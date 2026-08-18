"""Local Agent Loop 的共享命令行控制入口。

本文件刻意只保留三类工作：

1. 定义稳定的命令名和命令行参数；
2. 将子命令绑定到 Operator、Planner、Worker 或共享控制模块；
3. 把可公开的控制面异常统一转换为 UTF-8 JSON。

实际状态机不在这里：Operator 位于 ``operator/control.py``，Planner 的禁用兼容入口
位于 ``planner/control.py``，Worker 位于 ``worker/control.py``，
迁移和恢复等共享逻辑位于 ``loop_agent/control``。

手工排查顺序：

* 参数无法识别：检查 ``parser()`` 中对应命令的参数定义；
* 命令进入后状态不符合预期：检查 ``set_defaults(handler=...)`` 指向的角色函数；
* 返回 ERROR：从 ``main()`` 捕获的异常类型进入对应实现模块；
* SQLite 数据不一致：先运行 ``validate``，禁止直接改表掩盖根因。
"""

from __future__ import annotations

import argparse
import importlib.util
import sqlite3
import sys

sys.dont_write_bytecode = True

# 共享配置、路径和数据库门面。loopctl 只通过这些稳定名字访问数据层，
# 这样角色模块重组时，外部命令路径和调用参数不需要同步变化。
from loopdb import (
    BASE_DIR,
    CAPABILITY_LEVELS,
    CLAIM_RUNTIME_ENVIRONMENTS,
    CONFIG_PATH,
    DEFAULT_DB,
    LoopError,
    SCHEMA_USER_VERSION,
    connect,
    state_payload,
    validate_database,
)

# Planner 与 Worker 已迁至仓库根目录。追加而非前置根目录，避免根目录 operator/
# 与 Python 标准库 operator 发生名称遮蔽；Operator 仍使用下方的文件路径加载。
repository_path = str(BASE_DIR)
if repository_path not in sys.path:
    sys.path.append(repository_path)

# loopctl 自身只需要统一 JSON 输出；各角色直接导入其余输入与校验工具。
from loop_agent.control.io import output

# 不属于单一角色的控制能力：SQLite Schema 迁移和失联恢复。
from loop_agent.control.migration import command_migrate
from loop_agent.control.recovery import command_recover

# Planner 业务重建期间保留旧命令名，handler 会在访问数据库前明确拒绝执行。
from planner.control import (
    command_preflight_claim,
    command_preflight_fail,
    command_preflight_heartbeat,
    command_preflight_needs_review,
    command_preflight_ready,
)

# Worker 子命令负责原子领取、租约心跳、动态扩锁和最终回写。
from worker.control import (
    command_claim,
    command_extend_scope,
    command_finish,
    command_heartbeat,
)

# Operator 位于仓库根目录 ``operator/``，该目录名与 Python 标准库 operator 冲突。
# 因此按绝对文件路径加载，不能写成 ``from operator.control import ...``；对外保留
# 不变的 loopctl 子命令和 handler 名称。
operator_control_path = BASE_DIR / "operator" / "control.py"
operator_spec = importlib.util.spec_from_file_location(
    "local_agent_loop_operator_control", operator_control_path
)
if operator_spec is None or operator_spec.loader is None:
    raise RuntimeError("无法加载根目录 Operator 控制模块")
operator_control = importlib.util.module_from_spec(operator_spec)
sys.modules[operator_spec.name] = operator_control
operator_spec.loader.exec_module(operator_control)
command_archive = operator_control.command_archive
command_cancel = operator_control.command_cancel
command_confirm = operator_control.command_confirm
command_enqueue = operator_control.command_enqueue
command_migrate_assets_directory = operator_control.command_migrate_assets_directory
command_migrate_internal_runtime = operator_control.command_migrate_internal_runtime
command_requeue = operator_control.command_requeue
command_resolve_human = operator_control.command_resolve_human
command_unarchive = operator_control.command_unarchive
command_update = operator_control.command_update


def command_validate(args: argparse.Namespace) -> None:
    """只读验证数据库的 Schema、跨表关系、execution 和锁生命周期。

    返回码 0 表示 VALID，返回码 1 表示发现一致性错误。无论结果如何都在
    finally 中关闭连接，避免人工重复执行检查时积累数据库句柄。
    """
    database = connect(args.db)
    try:
        result = validate_database(database)
        output({"outcome": "VALID" if result["ok"] else "INVALID", **result}, 0 if result["ok"] else 1)
    finally:
        database.close()


def command_state(args: argparse.Namespace) -> None:
    """只读输出 Dashboard 使用的完整状态投影，不修改 revision 或任务。"""
    database = connect(args.db)
    try:
        output(state_payload(database))
    finally:
        database.close()


def parser() -> argparse.ArgumentParser:
    """构建稳定 CLI 契约，并把每个子命令绑定到唯一 handler。

    ``set_defaults(handler=...)`` 是排查命令去向的索引。新增命令时必须同时
    定义参数、绑定 handler，并由对应角色测试覆盖，不能在 main 中写分支判断。
    """
    root = argparse.ArgumentParser(description="Concurrent SQLite Local Agent Loop controller")
    root.add_argument("--db", default=str(DEFAULT_DB))
    commands = root.add_subparsers(dest="command", required=True)

    # 一、迁移与只读检查。
    # migrate 只升级已有 SQLite Schema，并保留数据库中的历史任务数据。
    migrate = commands.add_parser("migrate")
    migrate.set_defaults(handler=command_migrate)

    validate = commands.add_parser("validate")
    validate.set_defaults(handler=command_validate)
    state = commands.add_parser("state")
    state.set_defaults(handler=command_state)

    # 二、Planner 预留命令。参数契约暂时保留，所有 handler 当前均拒绝业务执行。
    preflight_claim = commands.add_parser("preflight-claim")
    preflight_claim.add_argument("execution_id")
    preflight_claim.add_argument(
        "--runtime-environment", required=True, choices=CLAIM_RUNTIME_ENVIRONMENTS
    )
    preflight_claim.add_argument("--sandbox", required=True, choices=("read-only",))
    preflight_claim.set_defaults(handler=command_preflight_claim)
    preflight_heartbeat = commands.add_parser("preflight-heartbeat")
    preflight_heartbeat.add_argument("execution_id")
    preflight_heartbeat.add_argument("task_id")
    preflight_heartbeat.add_argument("--expected-row-version", type=int)
    preflight_heartbeat.set_defaults(handler=command_preflight_heartbeat)

    # 三个旧结束命令继续解析原参数，便于调用方得到稳定的禁用错误。
    for name, handler in (
        ("preflight-ready", command_preflight_ready),
        ("preflight-needs-review", command_preflight_needs_review),
        ("preflight-fail", command_preflight_fail),
    ):
        preflight_finish = commands.add_parser(name)
        preflight_finish.add_argument("execution_id")
        preflight_finish.add_argument("task_id")
        preflight_finish.add_argument(
            "report", nargs="?", default="-", help="UTF-8 JSON 文件；省略或使用 - 时从 stdin 读取"
        )
        preflight_finish.add_argument("--expected-row-version", type=int)
        preflight_finish.set_defaults(handler=handler)

    # 三、Worker execution 协议。
    claim = commands.add_parser("claim")
    claim.add_argument("execution_id")
    claim.add_argument("--capability-level", required=True, choices=CAPABILITY_LEVELS)
    claim.add_argument("--runtime-environment", required=True, choices=CLAIM_RUNTIME_ENVIRONMENTS)
    claim.add_argument("--provider-id")
    claim.add_argument("--execution-policy", choices=("automatic", "manual"))
    claim.set_defaults(handler=command_claim)
    heartbeat = commands.add_parser("heartbeat")
    heartbeat.add_argument("execution_id")
    heartbeat.add_argument("task_id")
    heartbeat.set_defaults(handler=command_heartbeat)

    # Worker 发现新文件范围时，必须先通过 extend-scope 获得新的锁凭证，
    # 然后才能编辑；expected-row-version 用于拒绝基于旧页面状态的扩锁请求。
    extend_scope = commands.add_parser("extend-scope")
    extend_scope.add_argument("execution_id")
    extend_scope.add_argument("task_id")
    extend_scope.add_argument(
        "report", nargs="?", default="-", help="UTF-8 JSON 文件；省略或使用 - 时从 stdin 读取"
    )
    extend_scope.add_argument("--expected-row-version", type=int)
    extend_scope.set_defaults(handler=command_extend_scope)

    # 四、失联恢复。
    # 当前恢复只接受受控 Runner 对自己进程树的终止确认。
    recover = commands.add_parser("recover")
    recover.add_argument("execution_id")
    recover.add_argument("--action", choices=("requeue", "failed", "wait"))
    recover.add_argument("--expected-row-version", type=int)
    recover.add_argument("--runner-confirmed-terminated", action="store_true", required=True)
    recover.set_defaults(handler=command_recover)
    finish = commands.add_parser("finish")
    finish.add_argument("execution_id")
    finish.add_argument("task_id")
    finish.add_argument("report", nargs="?", default="-", help="UTF-8 JSON 文件；省略或使用 - 时从 stdin 读取")
    finish.set_defaults(handler=command_finish)

    # 五、Operator 人工任务管理。
    # confirm 只确认 SUCCEEDED；archive 是独立 archived_at 属性，不改变任务状态。
    confirm = commands.add_parser("confirm")
    confirm.add_argument("task_id")
    confirm.add_argument("--reason")
    confirm.add_argument("--expected-row-version", type=int)
    confirm.set_defaults(handler=command_confirm)
    resolve_human = commands.add_parser("resolve-human")
    resolve_human.add_argument("task_id")
    resolve_human.add_argument("--response", required=True)
    resolve_human.add_argument("--summary")
    resolve_human.add_argument("--reason")
    resolve_human.add_argument("--expected-row-version", type=int)
    resolve_human.set_defaults(handler=command_resolve_human)

    # 归档和取消归档都要求当前 row_version，避免覆盖较新的人工操作。
    archive = commands.add_parser("archive")
    archive.add_argument("task_id")
    archive.add_argument("--reason")
    archive.add_argument("--expected-row-version", type=int)
    archive.set_defaults(handler=command_archive)
    unarchive = commands.add_parser("unarchive")
    unarchive.add_argument("task_id")
    unarchive.add_argument("--reason")
    unarchive.add_argument("--expected-row-version", type=int, required=True)
    unarchive.set_defaults(handler=command_unarchive)

    # enqueue/update 的任务定义通过 UTF-8 JSON 文件传入。
    # enqueue 固定创建 DRAFT；update 会清除旧 Planner 补充并重新进入预检。
    enqueue = commands.add_parser("enqueue")
    enqueue.add_argument("file")
    enqueue.set_defaults(handler=command_enqueue)
    update = commands.add_parser("update")
    update.add_argument("task_id")
    update.add_argument("file")
    update.add_argument("--expected-row-version", type=int)
    update.set_defaults(handler=command_update)

    # 一次性收敛旧 Codex 路由；只改执行目标，不重置任务状态或历史结果。
    migrate_internal_runtime = commands.add_parser("migrate-internal-runtime")
    migrate_internal_runtime.add_argument("--reason")
    migrate_internal_runtime.set_defaults(handler=command_migrate_internal_runtime)

    # 把根级任务附件路径迁移到 data，不改变任务状态、结果或归档属性。
    migrate_assets_directory = commands.add_parser("migrate-assets-directory")
    migrate_assets_directory.add_argument("--reason")
    migrate_assets_directory.set_defaults(handler=command_migrate_assets_directory)

    # requeue 根据当前状态回到 DRAFT 或 PENDING；cancel 只做逻辑取消并保留历史。
    requeue = commands.add_parser("requeue")
    requeue.add_argument("task_id")
    requeue.add_argument("--reason")
    requeue.add_argument("--expected-row-version", type=int)
    requeue.set_defaults(handler=command_requeue)
    cancel = commands.add_parser("cancel")
    cancel.add_argument("task_id")
    cancel.add_argument("--reason")
    cancel.set_defaults(handler=command_cancel)
    return root


def main() -> None:
    """解析一次命令并执行 handler；除迁移和只读检查外都要求当前 Schema。"""
    args = parser().parse_args()
    try:
        if args.command not in {"migrate", "validate", "state"}:
            database = connect(args.db)
            try:
                current = int(database.execute("PRAGMA user_version").fetchone()[0])
            finally:
                database.close()
            if current != SCHEMA_USER_VERSION:
                raise LoopError(
                    f"数据库 Schema 不是当前版本: user_version={current}；"
                    "请先运行 loopctl.py migrate"
                )
        args.handler(args)
    except (LoopError, sqlite3.Error, OSError, ValueError) as error:
        # 这里只公开经过约束的业务、SQLite、文件和输入错误。
        # 编程错误不在此捕获，使 traceback 能在开发测试中直接暴露真实缺陷。
        output({"outcome": "ERROR", "message": str(error)}, 1)


if __name__ == "__main__":
    main()
