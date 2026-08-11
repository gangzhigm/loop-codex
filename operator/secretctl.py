"""Provider 密钥初始化与生命周期管理的 UTF-8 交互命令。

本文件只负责解析人工命令、确认高风险动作并调用统一 ``SecretStore``。支持查询、
校验、首次写入、轮换和删除；不会把密钥写入 SQLite、配置文件、日志或 JSON 输出。
启用 ``--connect`` 会真实请求 Provider，可能产生费用，因此必须再次输入 CONNECT。
"""

from __future__ import annotations

# 中文排查：这是人工管理 Provider 密钥的命令入口，解析参数后调用统一 SecretStore。
# 失败时按“配置加载 -> 存储后端可用性 -> 当前账户权限 -> 可选连接验证”的顺序检查。
# 命令输出禁止包含密钥、掩码或可逆信息，调试时也不能临时打印原值。

import argparse
import getpass
import json
import sys
from pathlib import Path
from typing import Any, Callable, Mapping, TextIO

SCRIPTS_ROOT = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from loop_agent.providers.deepseek import (
    DeepSeekProviderError,
    DeepSeekSettings,
    verify_deepseek_credential,
)
from loopdb import CONFIG_PATH, load_initialization_config
from loop_agent.secrets.store import (
    SecretOperationUnsupported,
    SecretStore,
    SecretStoreError,
    create_secret_store,
)


PROVIDERS = {"deepseek"}


def parser() -> argparse.ArgumentParser:
    """构建密钥管理命令行，并仅开放已注册的 Provider 与生命周期动作。"""
    root = argparse.ArgumentParser(description="Initialize Local Agent Loop provider secrets")
    root.add_argument("--config", default=str(CONFIG_PATH))
    commands = root.add_subparsers(dest="command", required=True)
    for name in ("status", "verify", "set", "rotate", "delete"):
        command = commands.add_parser(name)
        command.add_argument("provider", choices=sorted(PROVIDERS))
        if name in {"verify", "set", "rotate"}:
            command.add_argument(
                "--connect",
                action="store_true",
                help="Perform an explicitly confirmed provider connection check",
            )
    return root


def _secret_ref(config: Mapping[str, Any], provider: str) -> str:
    """从初始化配置解析 Provider 的密钥引用名，不读取密钥本身。"""
    value = config.get(provider)
    secret_ref = value.get("secret_ref") if isinstance(value, Mapping) else None
    if not isinstance(secret_ref, str) or not secret_ref:
        raise SecretStoreError("provider secret_ref is missing from initialization configuration")
    return secret_ref


def _confirm(input_fn: Callable[[str], str], prompt: str, expected: str) -> None:
    """要求用户完整输入指定确认词；任何其他输入都立即中止高风险操作。"""
    if input_fn(prompt).strip() != expected:
        raise SecretStoreError("operation was not confirmed")


def _read_secret(secret_input: Callable[[str], str]) -> str:
    """通过无回显输入读取两次 DeepSeek 密钥，并在内容不一致时拒绝继续。"""
    first = secret_input("DeepSeek API key: ")
    second = secret_input("Repeat DeepSeek API key: ")
    if first != second:
        raise SecretStoreError("secret entries did not match")
    return first


def _connection_verifier(
    config: Mapping[str, Any],
    connect: bool,
    input_fn: Callable[[str], str],
) -> Callable[[str], bool | None] | None:
    """按 ``--connect`` 构造真实连接校验器，并在联网前取得明确确认。"""
    if not connect:
        return None
    _confirm(
        input_fn,
        "Connection validation can make a billable provider request. Type CONNECT to continue: ",
        "CONNECT",
    )
    settings = DeepSeekSettings.from_config(config)
    return lambda candidate: verify_deepseek_credential(candidate, settings)


def execute(
    args: argparse.Namespace,
    *,
    config: Mapping[str, Any],
    store: SecretStore,
    input_fn: Callable[[str], str] = input,
    secret_input: Callable[[str], str] = getpass.getpass,
) -> dict[str, Any]:
    """执行一个密钥动作并返回不含敏感值的结构化结果。

    环境变量型存储只允许注入和校验，不能持久化 set、rotate 或 delete。写入和轮换
    可选择先做真实连接验证；删除和轮换必须分别输入固定确认词。
    """
    secret_ref = _secret_ref(config, args.provider)
    if args.command == "status":
        return {"outcome": "STATUS", "provider": args.provider, **store.status(secret_ref).as_dict()}
    if args.command in {"set", "rotate", "delete"} and not store.capabilities.persistent:
        raise SecretOperationUnsupported(
            "environment backend is injection-only; update the launching process environment instead"
        )
    if args.command == "verify":
        result = store.verify(secret_ref)
        if args.connect:
            verifier = _connection_verifier(config, True, input_fn)
            result = store.verify(secret_ref, verifier=verifier)
    elif args.command == "set":
        status = store.status(secret_ref)
        if status.state == "ready":
            raise SecretStoreError("secret_ref already exists; use rotate for replacement")
        if status.state != "missing":
            raise SecretStoreError("secret backend is not ready for initialization")
        candidate = _read_secret(secret_input)
        verifier = _connection_verifier(config, args.connect, input_fn)
        result = store.set(secret_ref, candidate, verifier=verifier)
    elif args.command == "rotate":
        _confirm(input_fn, "Type ROTATE to replace the existing secret: ", "ROTATE")
        candidate = _read_secret(secret_input)
        verifier = _connection_verifier(config, args.connect, input_fn)
        result = store.rotate(secret_ref, candidate, verifier=verifier)
    elif args.command == "delete":
        _confirm(input_fn, "Type DELETE to remove the secret: ", "DELETE")
        result = store.delete(secret_ref)
    else:
        raise SecretStoreError("unsupported secret operation")
    return {"outcome": "COMPLETED", "provider": args.provider, **result.as_dict()}


def main(
    argv: list[str] | None = None,
    *,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
) -> int:
    """加载配置与 SecretStore，统一输出 JSON，并将可预期错误映射为退出码 1。"""
    try:
        args = parser().parse_args(argv)
        config = load_initialization_config(Path(args.config))
        result = execute(args, config=config, store=create_secret_store(config))
    except (SecretStoreError, DeepSeekProviderError) as error:
        print(
            json.dumps(
                {"outcome": "ERROR", "error": str(error), "error_type": type(error).__name__},
                ensure_ascii=False,
            ),
            file=stderr,
        )
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2), file=stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
