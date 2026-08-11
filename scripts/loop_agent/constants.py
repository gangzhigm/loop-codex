"""稳定的状态、执行路由和 Schema 常量。

这些值是配置校验、SQLite 约束、Planner 预检、Worker 领取和 Dashboard 共同依赖的契约。
修改时必须同步调整 Schema、提示词、迁移和回归测试。
"""

# 中文排查：这里的状态、优先级、能力等级和 Schema 版本是跨模块公共契约。
# 修改任一值时必须同步检查 SQL、迁移、配置校验、提示词和回归测试，不能只改常量。

SCHEMA_VERSION = "3.7.0"
SCHEMA_USER_VERSION = 30700
PREFLIGHT_SCHEMA_USER_VERSION = 30600
DIAGNOSTIC_SCHEMA_USER_VERSION = 30500
RECOVERY_SCHEMA_USER_VERSION = 30400
PROFILE_ROUTING_SCHEMA_USER_VERSION = 30300
ROUTING_SCHEMA_USER_VERSION = 30200
ARCHIVE_SCHEMA_USER_VERSION = 30100
LEGACY_SCHEMA_USER_VERSION = 30000

FINAL_EXECUTION_STATUSES = {"SUCCEEDED", "FAILED", "WAITING_HUMAN"}
DEPENDENCY_COMPLETE_STATUSES = {"SUCCEEDED", "CONFIRMED"}
ARCHIVABLE_STATUSES = {"CONFIRMED", "FAILED", "CANCELLED"}
PRIORITIES = ("blocker", "critical", "high", "medium", "low")
EXECUTION_PROFILES = ("routine", "standard", "advanced", "deep", "complex", "exceptional")
CAPABILITY_LEVELS = ("L1", "L2", "L3", "L4", "L5")
PREFLIGHT_STATUSES = ("UNINSPECTED", "INSPECTING", "READY", "FAILED")
LOCK_MODES = ("file", "module", "project")
EXECUTION_POLICIES = ("automatic", "manual")
CANONICAL_RUNTIME_ENVIRONMENTS = ("codex_automation", "codex_cli", "self_hosted_agent")
LEGACY_RUNTIME_ENVIRONMENTS = ("codex_automation", "codex_cli", "deepseek")

# 在所有旧启动器都改为 ``self_hosted_agent`` 并显式提供 Provider ID 之前，
# ``deepseek`` 继续作为兼容输入别名接受。
RUNTIME_ENVIRONMENTS = CANONICAL_RUNTIME_ENVIRONMENTS + ("deepseek",)
CLAIM_RUNTIME_ENVIRONMENTS = RUNTIME_ENVIRONMENTS
LEGACY_PROFILE_TO_CAPABILITY = {
    "routine": "L1",
    "standard": "L2",
    "advanced": "L3",
    "deep": "L4",
    "complex": "L5",
    "exceptional": "L5",
}

FORBIDDEN_SCOPE_ROOTS = {"$CODEX_HOME", ".reasonix", ".env"}
