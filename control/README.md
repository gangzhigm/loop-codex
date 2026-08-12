# Control 代码导航

`control/` 根目录只保留跨角色共享的 `loopctl.py`、`loopdb.py` 和本导航。
Supervisor、Operator、Planner、Worker、Dispatcher、Runner 都位于仓库根目录对应角色目录；
可复用基础模块位于 `control/loop_agent/`。

所有文本输入输出使用 UTF-8。SQLite 写入必须经过 `loopctl.py` 暴露的控制面，
不能从 Dashboard、Provider 或业务脚本直接更新任务表。

## 稳定入口

| 文件 | 职责 | 不负责 |
| --- | --- | --- |
| `loopctl.py` | CLI 参数、命令分发、`validate/state` | 具体任务状态机实现 |
| `loopdb.py` | 导出当前数据库公共 API | SQL 业务实现 |
| `../operator/secretctl.py` | 系统密钥库的人工管理入口 | 任务数据库 |
| `../planner/control.py` | Planner 的只读静态预检状态机 | 业务实现与 scope 写锁 |
| `../worker/control.py` | Worker 的领取、心跳、扩锁和结束状态机 | 周期调度 |
| `../dispatcher/codex_cli_dispatcher.py` | 只读选择一个 Codex CLI 能力等级并启动一次 Runner | 原子 claim 与业务实现 |
| `../runner/codex_cli_runner.py` | 单任务 Codex CLI 进程、heartbeat、finish | 周期调度 |
| `../runner/agent_runtime.py` | Self-hosted Agent 启动和 Provider 工厂 | 工具循环具体实现 |
| `../supervisor/main.py` | Supervisor 统一入口：一次健康检查或启动常驻 Client 服务 | 任务调度与任务表写入 |
| `../client/dashboard_server.py` | 本机 HTTP 路由、Secret API、静态资源服务 | 任务状态直接写入 |
| `../supervisor/health_run.py` | Supervisor 探活与恢复 | AI 自动化与任务领取 |

## 目录分区

```text
control/
├─ deployment_checks/       当前真实部署、配置与前端文件的只读校验
├─ tests/                   Python 回归测试与测试路径引导
├─ loop_agent/              跨角色可复用内部实现
├─ loopctl.py               共享任务控制 CLI
└─ loopdb.py                共享数据库公共 API
```

## 内部模块

```text
loop_agent/
├─ paths.py                 固定仓库路径
├─ constants.py             状态、优先级、能力等级和 Schema 常量
├─ configuration.py         initialization.json 校验与执行路由解析
├─ serialization.py         UTF-8 JSON 与 Asia/Shanghai 时间
├─ errors.py                公共 LoopError
├─ control/
│  ├─ io.py                 CLI JSON/stdin 与 row_version 检查
│  ├─ migration.py          旧 JSON 导入和 SQLite Schema 升级命令
│  ├─ queue.py              依赖就绪和旧冲突状态兼容
│  └─ recovery.py           失联检测、锁隔离和安全恢复
├─ database/
│  ├─ sql.py                建表 SQL 字符串
│  ├─ schema.py             连接、DDL 和逐版本迁移
│  ├─ compatibility.py      旧 Schema 列能力探测
│  ├─ execution_settings.py 并发与 execution 参数读取
│  ├─ task_store.py         任务写入、依赖、子表与 API 投影
│  ├─ state.py              Dashboard 全量状态与派生 revision
│  └─ validation.py         跨表一致性和锁生命周期校验
├─ tasks/
│  ├─ normalization.py      纯数据形状、诊断和拆分建议校验
│  └─ scopes.py             项目清单、scope 规范化与冲突判定
├─ runtime/
│  ├─ contracts.py          Provider/工具协议常量
│  ├─ core.py               路由快照、运行参数、错误和脱敏日志
│  ├─ controller.py         loopctl 子进程适配和 heartbeat 守卫
│  ├─ sandbox.py            scope 路径策略与本地受限工具
│  ├─ protocol.py           模型响应、工具参数和终态结果校验
│  ├─ diagnostics.py        value-free Provider/final 诊断
│  └─ agent.py              单次 claim 到 finish 的模型编排
├─ providers/
│  └─ deepseek.py           DeepSeek API 到中立 Provider 协议的适配
├─ secrets/
│  └─ store.py              系统密钥库抽象与本地后端
```

## 依赖方向

内部模块应按以下方向依赖，避免循环导入：

```text
constants / paths / errors / serialization
                 ↓
configuration / tasks.normalization / tasks.scopes
                 ↓
database.schema / database.task_store / database.state / database.validation
                 ↓
operator / planner / worker / supervisor / dispatcher / runner
                 ↓
根目录共享入口
```

`loopdb.py` 是运行入口使用的数据库公共 API。数据层内部模块应直接导入实际
职责模块。当前少量控制面和运行时模块仍从 `loopdb.py` 导入，是为了分阶段保持公共
契约；后续可以机械替换，但不能改变外部导入路径。

## 排障顺序

任务无法创建或更新：

1. 检查 `../operator/control.py` 的输入字段和 row version 门禁。
2. 检查 `tasks/normalization.py` 与 `tasks/scopes.py` 的纯校验错误。
3. 检查 `database/task_store.py` 的任务与子表写入。
4. 运行 `python control/loopctl.py validate` 查看跨表错误。

Planner 无法把 DRAFT 变为 PENDING：

1. 检查 `../planner/control.py` 的 execution fencing 和 UTF-8 stdin 契约。
2. 检查最终 capability、scope、lock mode、技术验收和 evidence 是否齐全。
3. 检查 L5/manual 是否具有 Operator 明确批准标记。
4. 运行 Planner 专项测试或标准 unittest discovery。

Worker 无法领取任务：

1. 检查任务是否为 `PENDING/READY` 且路由四元组匹配。
2. 检查 `database/task_store.py` 投影出的依赖状态和 scope queue position。
3. 检查 `../worker/control.py` 的全局/平台容量和动态冲突列表。
4. 检查 `control/recovery.py` 是否保留了 `QUARANTINED` 锁。

Self-hosted Agent 异常：

1. `runtime/core.py`：配置和 route snapshot 是否有效。
2. `runtime/controller.py`：claim/heartbeat/finish 是否返回合法 JSON。
3. `runtime/sandbox.py`：路径、敏感文件、命令白名单是否拒绝请求。
4. `runtime/protocol.py`：Provider envelope、工具参数或 final schema 是否不合法。
5. `runtime/agent.py`：attempt deadline、重试抑制、final repair 和 finish 顺序。
6. `runtime/diagnostics.py`：只依据 value-free 类别排查，不读取原始敏感响应。

Dashboard 异常：

1. `/healthz` 只证明 HTTP 服务存活。
2. `/api/state` 读取 `database/state.py` 的全量投影。
3. Client 的归档/恢复走 `client/service/tasks.py -> loopctl.py`，不得直接写表。
4. Client 的运维配置走 `client/service/operations.py`，与任务 SQLite 分离。
5. Secret API 只允许本机同源请求，失败时检查 Host、Origin、CSRF 和一次性 UUID。

Supervisor 异常：

1. `runtime/supervisor.pid` 标识当前 `main.py serve` 进程。
2. `runtime/supervisor-heartbeat.json` 证明主监控循环仍在按周期推进。
3. 外部 `health` 只负责恢复 Supervisor 主进程，组件状态由 `main.py serve` 负责。

## 回归测试

控制面回归通过标准 unittest discovery 发现各 `test_loop_*.py` 模块。
控制面测试按职责拆分：

| 文件 | 职责 |
| --- | --- |
| `_loop_support.py` | UTF-8 路径、临时数据库、任务构造、CLI 调用和历史 Schema fixture；不定义独立测试场景 |
| `test_loop_configuration.py` | 初始化配置、状态投影和执行路由 |
| `test_loop_migrations.py` | 旧 JSON、SQLite Schema 和混合锁迁移 |
| `test_loop_planner.py` | Planner 预检、契约、诊断和迟到写回 fencing |
| `test_loop_claiming.py` | 领取、容量、优先级、依赖和 scope 锁 |
| `test_loop_recovery.py` | 心跳、租约、超时、隔离和人工恢复 |
| `test_loop_lifecycle.py` | 人工确认、阻塞答复、归档和重新排队 |

```powershell
$env:PYTHONUTF8 = '1'
python -m unittest discover -s control/tests -p "test_loop_*.py"
python control/tests/test_dashboard_server.py
python control/tests/test_agent_runtime.py
python control/tests/test_deepseek_provider.py
python control/tests/test_codex_cli_runner.py
python control/tests/test_codex_cli_dispatcher.py
python control/tests/test_secret_store.py
python control/tests/test_instruction_authority.py
powershell -NoProfile -ExecutionPolicy Bypass -File control/deployment_checks/check-initialization.ps1 -SkipCodexCliCheck
node control/deployment_checks/check-dashboard.mjs
python control/loopctl.py validate
```

运行入口保持稳定；检查、安装和测试路径按用途分区。移动后必须同步更新本文档、
根 README、测试内路径断言和安装器的仓库根目录计算。
