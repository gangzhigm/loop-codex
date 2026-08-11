"""Local Agent Loop 的共享命令行控制入口。

本文件刻意只保留三类工作：

1. 定义稳定的命令名和命令行参数；
2. 将子命令绑定到 Operator、Planner、Worker 或共享控制模块；
3. 把可公开的控制面异常统一转换为 UTF-8 JSON。

实际状态机不在这里：Operator 位于 ``roles/operator/control.py``，Planner
位于 ``roles/planner/control.py``，Worker 位于 ``roles/worker/control.py``，
迁移和恢复等共享逻辑位于 ``loop_agent/control``。

手工排查顺序：

* 参数无法识别：检查 ``parser()`` 中对应命令的参数定义；
* 命令进入后状态不符合预期：检查 ``set_defaults(handler=...)`` 指向的角色函数；
* 返回 ERROR：从 ``main()`` 捕获的异常类型进入对应实现模块；
* SQLite 数据不一致：先运行 ``validate``，禁止直接改表掩盖根因。
"""

from __future__ import annotations

import argparse
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
    EXECUTION_PROFILES,
    LoopError,
    connect,
    state_payload,
    validate_database,
)

# 控制面通用工具：JSON/stdin 读取、统一输出、UTF-8 完整性和乐观并发检查。
# 这些名字继续从 loopctl 导出，是为了兼容已有测试和人工诊断脚本。
from loop_agent.control.io import (
    output,
    read_json,
    read_json_source,
    read_preflight_report,
    require_expected_row_version,
    validate_preflight_text_integrity,
)

# 不属于单一角色的控制能力：旧数据迁移、失联恢复和依赖队列兼容。
from loop_agent.control.migration import command_migrate, command_migrate_legacy
from loop_agent.control.recovery import (
    command_recover,
    recovery_required_records,
    stalled_executions,
    transition_recovery_states,
)
from loop_agent.control.queue import dependencies_ready, requeue_resolved_conflicts

# Planner 子命令只处理独立静态预检 execution，不占用 Worker execution 容量。
from roles.planner.control import (
    command_preflight_claim,
    command_preflight_fail,
    command_preflight_heartbeat,
    command_preflight_needs_review,
    command_preflight_ready,
)

# Worker 子命令负责原子领取、租约心跳、动态扩锁和最终回写。
from roles.worker.control import (
    claim_target,
    command_claim,
    command_extend_scope,
    command_finish,
    command_heartbeat,
    describe_conflicting_task,
    scope_lock_credential,
    task_scopes_and_conflicts,
)

# Operator 子命令只管理任务定义、人工状态和归档，不实现业务任务。
from roles.operator.control import (
    command_archive,
    command_cancel,
    command_confirm,
    command_enqueue,
    command_requeue,
    command_resolve_human,
    command_unarchive,
    command_update,
)


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
    # migrate-legacy 处理旧 JSON 导入；migrate 只升级已有 SQLite Schema。
    # 两者都不是空环境初始化，因此 legacy 导入要求显式输入路径和备份位置。
    legacy_migrate = commands.add_parser(
        "migrate-legacy",
        help="将旧 TASKS.json 和 INBOX.json 导入 SQLite；不是空系统初始化入口",
    )
    legacy_migrate.add_argument("--tasks", required=True, help="旧 TASKS.json 的 UTF-8 路径")
    legacy_migrate.add_argument("--inbox", required=True, help="旧 INBOX.json 的 UTF-8 路径")
    legacy_migrate.add_argument("--registry", default=str(BASE_DIR.parent / "根目录清单.md"))
    legacy_migrate.add_argument("--config", default=str(CONFIG_PATH))
    legacy_migrate.add_argument("--backup-dir", default=str(BASE_DIR / "backups"))
    legacy_migrate.add_argument("--force", action="store_true")
    legacy_migrate.set_defaults(handler=command_migrate_legacy)

    migrate = commands.add_parser("migrate")
    migrate.set_defaults(handler=command_migrate)

    validate = commands.add_parser("validate")
    validate.set_defaults(handler=command_validate)
    state = commands.add_parser("state")
    state.set_defaults(handler=command_state)

    # 二、Planner 静态预检协议。
    # claim 强制 codex_automation/read-only，避免 Planner 获得业务写权限。
    preflight_claim = commands.add_parser("preflight-claim")
    preflight_claim.add_argument("execution_id")
    preflight_claim.add_argument(
        "--runtime-environment", required=True, choices=("codex_automation",)
    )
    preflight_claim.add_argument("--sandbox", required=True, choices=("read-only",))
    preflight_claim.set_defaults(handler=command_preflight_claim)
    preflight_heartbeat = commands.add_parser("preflight-heartbeat")
    preflight_heartbeat.add_argument("execution_id")
    preflight_heartbeat.add_argument("task_id")
    preflight_heartbeat.add_argument("--expected-row-version", type=int)
    preflight_heartbeat.set_defaults(handler=command_preflight_heartbeat)

    # 三个预检结束命令共享相同的 execution/task/report 参数。
    # report 默认使用 '-'，即从 UTF-8 stdin 读取，防止大段 JSON 和业务文本出现在命令行。
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
    # --profile 仅用于旧调用兼容；新入口应显式传 --capability-level。
    claim = commands.add_parser("claim")
    claim.add_argument("execution_id")
    claim_level = claim.add_mutually_exclusive_group(required=True)
    claim_level.add_argument("--profile", choices=EXECUTION_PROFILES)
    claim_level.add_argument("--capability-level", choices=CAPABILITY_LEVELS)
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
    # 恢复必须由受控 Runner 确认进程树终止，或由人工确认旧客户端会话安全结束；
    # 两种确认互斥，防止调用方用模糊的布尔组合绕过隔离锁。
    recover = commands.add_parser("recover")
    recover.add_argument("execution_id")
    recover.add_argument("--action", choices=("requeue", "failed", "wait"))
    recover.add_argument("--expected-row-version", type=int)
    recovery_confirmation = recover.add_mutually_exclusive_group(required=True)
    recovery_confirmation.add_argument("--runner-confirmed-terminated", action="store_true")
    recovery_confirmation.add_argument("--human-confirmed-safe", action="store_true")
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

    # 归档和取消归档保留原终态。unarchive 不要求 row_version 是为了兼容既有人工入口；
    # 真正的状态与终态限制仍由 Operator handler 校验。
    archive = commands.add_parser("archive")
    archive.add_argument("task_id")
    archive.add_argument("--reason")
    archive.add_argument("--expected-row-version", type=int)
    archive.set_defaults(handler=command_archive)
    unarchive = commands.add_parser("unarchive")
    unarchive.add_argument("task_id")
    unarchive.add_argument("--reason")
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
    """解析一次命令并执行一次 handler；loopctl 本身不是常驻调度循环。"""
    args = parser().parse_args()
    try:
        args.handler(args)
    except (LoopError, sqlite3.Error, OSError, ValueError) as error:
        # 这里只公开经过约束的业务、SQLite、文件和输入错误。
        # 编程错误不在此捕获，使 traceback 能在开发测试中直接暴露真实缺陷。
        output({"outcome": "ERROR", "message": str(error)}, 1)


if __name__ == "__main__":
    main()
