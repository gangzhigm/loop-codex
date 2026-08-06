# Prompt Sources

此目录是 Local Agent Loop 提示词的唯一维护位置：

- `operator.md`：人工任务管理主对话的查重、拆分、依赖、优先级、执行档位、状态、独立归档和 Worker 启停边界。
- `planner.md`：独立 Planner 自动化的一次 DRAFT 静态预检、强制只读边界和结构化写回契约。
- `worker.md`：Codex Worker 自动化的档位领取与完整执行提示词。
- `cli-worker.md`：Codex CLI Runner 已领取单任务后的业务执行与结构化结果提示词。

`operator.md` 由任务管理对话遵循。Planner 自动化的入口只读取 `planner.md`，固定使用 Terra/high、5 分钟周期、`codex_automation` 和 read-only 边界；五条 Worker 入口只读取 `worker.md`。入口不得复制正文。Windows 健康任务直接运行确定性脚本，不使用模型提示词。

Planner 每次只调用一次 `preflight-claim`，只读检查一个 DRAFT，并通过受控 stdin 通道提交 READY/NEEDS_REVIEW/FAILED；它不能实现任务、创建子任务或管理自动化。五条普通 Worker 自动化共享 `worker.md`，规范入口固定提供 `runtime_environment=codex_automation`、`capability_level=L1..L5` 和 `execution_policy=automatic`，不得复制正文。过渡期仍接受明确提供的旧 `execution_profile`：`routine -> L1`、`standard -> L2`、`advanced -> L3`、`deep -> L4`、`complex -> L5`；`exceptional` 仅表示 `L5/manual` 的人工一次性执行。Codex CLI Runner 使用 `cli-worker.md`，由 Runner 负责一次 READY claim、heartbeat、scope 锁和 finish；CLI 子进程不得管理队列，新增范围由 Runner/宿主先原子扩锁。自建 Agent 使用同一 READY 与 scope 锁契约。各入口调用 claim 时必须显式声明匹配运行环境；修改环境、能力等级、结构或路径时必须同步更新初始化配置、文档和真实执行入口。

真实 Planner 的创建和五条 Worker 的入口切换仅能由 Operator 在生产维护窗口完成并复核。Planner 与 Worker 都不读取、暂停、启用、删除或创建自动化；AI 会话在 claim 前已经启动这一产品差异不改变统一逻辑队列的主动拉取语义。
