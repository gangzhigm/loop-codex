"""Local Agent Loop 数据层的公共 API。

这个文件没有自己的 SQL、状态迁移或任务写入逻辑。它只把分散在
``loop_agent`` 内的当前数据库、配置和序列化 API 集中导出，供各运行入口使用。

手工排查时按导入分组进入权威实现：

* 配置或执行路由错误：``loop_agent/configuration.py``；
* 状态、优先级或版本常量错误：``loop_agent/constants.py``；
* 连接、建表和迁移错误：``loop_agent/database/schema.py``；
* 任务字段、依赖、scope 或投影错误：``loop_agent/database/task_store.py``；
* Dashboard 数量或 revision 错误：``loop_agent/database/state.py``；
* validate 报错：``loop_agent/database/validation.py``；
* 输入形状或结果诊断错误：``loop_agent/tasks/normalization.py``；
* 项目路由、锁键或冲突错误：``loop_agent/tasks/scopes.py``。

如果某个名字无法从 loopdb 导入，先确认它是否仍在下面的对应导入组；如果名字
存在但行为错误，应修改权威实现模块，禁止把业务逻辑重新写回这个门面。
"""

from __future__ import annotations

# 一、配置与执行路由。
# 新任务路由由 runtime_environment、provider_id、capability_level 和
# execution_policy 共同决定。
from loop_agent.configuration import (
    load_initialization_config,
    normalize_execution_target,
    resolve_execution_profile,
)

# 二、跨模块公共常量。
# 这些名字同时被 Schema 约束、Planner、Worker、Dashboard 和测试使用。
# 常量有误应修改 constants.py，并同步迁移/配置/测试，不能在调用方覆盖。
from loop_agent.constants import (
    ARCHIVABLE_STATUSES,
    ARCHIVE_SCHEMA_USER_VERSION,
    CAPABILITY_LEVELS,
    CANONICAL_RUNTIME_ENVIRONMENTS,
    CLAIM_RUNTIME_ENVIRONMENTS,
    DEPENDENCY_COMPLETE_STATUSES,
    DIAGNOSTIC_SCHEMA_USER_VERSION,
    EXECUTION_POLICIES,
    FINAL_EXECUTION_STATUSES,
    FORBIDDEN_SCOPE_ROOTS,
    LEGACY_SCHEMA_USER_VERSION,
    LOCK_MODES,
    PREFLIGHT_SCHEMA_USER_VERSION,
    PREFLIGHT_STATUSES,
    PRIORITIES,
    PROFILE_ROUTING_SCHEMA_USER_VERSION,
    RECOVERY_SCHEMA_USER_VERSION,
    ROUTING_SCHEMA_USER_VERSION,
    RUNTIME_ENVIRONMENTS,
    SCHEMA_USER_VERSION,
    SCHEMA_VERSION,
)

# 三、旧 Schema 能力探测。
# 迁移和兼容投影用它们判断某列是否存在；函数只读 sqlite metadata。
from loop_agent.database.compatibility import (
    uses_capability_schema,
    uses_preflight_schema,
    uses_recovery_schema,
    uses_result_diagnostic_schema,
)

# 四、执行容量和超时参数。
# 值来自 initialization.json，不来自任务数据库；数量异常时进入 execution_settings.py。
from loop_agent.database.execution_settings import (
    execution_setting,
    global_parallel_limit,
    platform_parallel_limit,
)

# 五、数据库连接、事务和 Schema 生命周期。
# connect 只建立连接；initialize_schema 创建当前结构；migrate_schema 升级旧结构。
# transaction/commit/rollback 由角色状态机显式控制，不能依赖隐式自动提交。
from loop_agent.database.schema import (
    commit,
    connect,
    initialize_schema,
    migrate_schema,
    rollback,
    schema_version,
    transaction,
    uses_hybrid_scope_schema,
)

# 六、显式 DDL 文本。
# 仅供建表、迁移和测试对照，业务代码不应直接执行这些片段修改真实任务。
from loop_agent.database.sql import (
    EXECUTIONS_TABLE_SQL,
    PREFLIGHT_EXECUTIONS_TABLE_SQL,
    SCOPE_LOCKS_TABLE_SQL,
    TASKS_TABLE_SQL,
)

# 七、Dashboard 全量状态和派生 revision。
# state_payload 是只读聚合出口；bump_revision/current_revision 管理派生修订号。
from loop_agent.database.state import (
    bump_revision,
    current_revision,
    state_payload,
)

# 八、任务存储与统一任务投影。
# insert_task/set_task_dependencies/replace_ordered_text 由外层事务包围；
# task_dict/all_tasks 负责把主表和子表拼成 Dashboard、Planner、Worker 共用结构。
from loop_agent.database.task_store import (
    all_tasks,
    dependency_cycle_path,
    insert_task,
    replace_ordered_text,
    scope_queue_position,
    set_task_dependencies,
    task_children,
    task_dict,
    task_exists,
)

# 九、全库一致性验证。
# ALLOWED_TABLES 定义受支持表集合；validate_database 只读检查，不自动修复。
from loop_agent.database.validation import ALLOWED_TABLES, validate_database

# 十、公共异常。LoopError 表示已经校验、可以安全向本机 Operator 展示的错误。
from loop_agent.errors import LoopError

# 十一、仓库内标准路径。各入口必须复用这些值，避免相对路径层级不一致。
from loop_agent.paths import BASE_DIR, CONFIG_PATH, DEFAULT_DB, SCHEMA_PATH

# 十二、统一时间和 JSON。
# now_shanghai/expires_at 保证租约和历史使用同一时区；json_load 提供受控默认值。
from loop_agent.serialization import (
    expires_at,
    json_dump,
    json_load,
    now_shanghai,
)

# 十三、纯数据规范化。
# 这些函数不访问 SQLite，用于拒绝畸形 Planner/Worker 结果和不安全诊断字段。
from loop_agent.tasks.normalization import (
    RESULT_DIAGNOSTIC_CATEGORIES,
    RESULT_DIAGNOSTIC_FIELD_NAMES,
    RESULT_DIAGNOSTIC_FINISH_REASONS,
    RESULT_DIAGNOSTIC_PARSE_STATES,
    RESULT_DIAGNOSTIC_TYPE_TAGS,
    TRANSIENT_RESULT_DIAGNOSTIC_CATEGORIES,
    load_result_diagnostic,
    normalize_result_diagnostic,
    normalize_split_suggestions,
    normalize_string_list,
)

# 十四、项目清单、scope 和冲突规则。
# resolve_scope_key 生成锁键；scope_keys_conflict 比较锁冲突；configured_projects
# 每次读取实时项目清单，不把路由缓存进 SQLite。
from loop_agent.tasks.scopes import (
    configured_projects,
    normalize_scope,
    parse_project_registry,
    parse_scope_key,
    resolve_scope_key,
    scope_conflicts_for_keys,
    scope_keys_conflict,
)


# 数据公共 API 是上面所有不以下划线开头的导入名。
__all__ = [name for name in globals() if not name.startswith("_")]
