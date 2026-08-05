"""Interactive UTF-8 secret initialization and lifecycle command."""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from pathlib import Path
from typing import Any, Callable, Mapping, TextIO

from deepseek_provider import DeepSeekProviderError, DeepSeekSettings, verify_deepseek_credential
from loopdb import CONFIG_PATH, load_initialization_config
from secret_store import (
    SecretOperationUnsupported,
    SecretStore,
    SecretStoreError,
    create_secret_store,
)


PROVIDERS = {"deepseek"}


def parser() -> argparse.ArgumentParser:
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
    value = config.get(provider)
    secret_ref = value.get("secret_ref") if isinstance(value, Mapping) else None
    if not isinstance(secret_ref, str) or not secret_ref:
        raise SecretStoreError("provider secret_ref is missing from initialization configuration")
    return secret_ref


def _confirm(input_fn: Callable[[str], str], prompt: str, expected: str) -> None:
    if input_fn(prompt).strip() != expected:
        raise SecretStoreError("operation was not confirmed")


def _read_secret(secret_input: Callable[[str], str]) -> str:
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
