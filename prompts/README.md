# Prompt Sources

此目录是 Local Agent Loop 提示词的唯一维护位置：

- `operator.md`：人工任务管理主对话的查重、拆分、依赖、优先级、执行档位、状态、独立归档和 Worker 启停边界。
- `planner.md`：独立 Planner 自动化的一次 DRAFT 静态预检、强制只读边界和结构化写回契约。
- `worker.md`：Codex Worker 自动化的档位领取与完整执行提示词。
- `cli-worker.md`：Codex CLI Runner 已领取单任务后的业务执行与结构化结果提示词。

`operator.md` 由任务管理对话遵循。Planner 自动化入口读取 `planner.md`，登记的普通 Codex Worker 入口读取 `worker.md`；入口不得复制提示词正文。模型、推理参数、自动化周期、入口数量、偏移量、并发上限和旧入口兼容映射只从 `config/initialization.json` 读取，本目录不维护这些部署值的副本。Windows 健康任务直接运行确定性脚本，不使用模型提示词。

Planner 每次只调用一次 `preflight-claim`，只读检查一个 DRAFT，并通过受控 stdin 通道提交 READY/NEEDS_REVIEW/FAILED；它不能实现任务、创建子任务或管理自动化。登记的普通 Worker 自动化共享 `worker.md`，入口必须显式提供与初始化配置相符的运行环境、能力等级和执行策略，不得复制正文。旧 `execution_profile` 只按初始化配置中的兼容映射解析。Codex CLI Runner 使用 `cli-worker.md`，由 Runner 负责一次 READY claim、heartbeat、scope 锁和 finish；CLI 子进程不得管理队列，新增范围由 Runner/宿主先原子扩锁。自建 Agent 使用同一 READY 与 scope 锁契约。修改环境、能力等级、结构或路径时必须同步更新初始化配置、文档和真实执行入口。

真实 Planner 的创建和普通 Worker 的入口切换仅能由 Operator 在生产维护窗口完成并复核。Planner 与 Worker 都不读取、暂停、启用、删除或创建自动化；AI 会话在 claim 前已经启动这一产品差异不改变统一逻辑队列的主动拉取语义。
