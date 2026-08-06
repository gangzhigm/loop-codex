# Local Agent Loop 操作约束

1. 所有文本文件按 UTF-8 读写，时间统一为带时区的 Asia/Shanghai ISO 8601。
2. `data/loop-agent.sqlite3` 只保存任务、Planner 预检及执行一致性数据，是任务的唯一事实来源。禁止重建运行时 `TASKS.json` 或 `INBOX.json`。
3. 运行环境列表及入口配置、模型映射、自动化周期、任务执行参数、项目默认优先级和服务部署参数只读取 `config/initialization.json`，不得保存到 SQLite；SQLite 只在任务行保存所选 `runtime_environment`、`capability_level`、Provider 与 `execution_policy`。
4. 项目路由实时读取 `E:\code\根目录清单.md`，不得缓存到 SQLite。
5. 健康状态只写入 `runtime/health-state.json`，不得保存到 SQLite。
6. 人工主对话是 Operator，必须遵循 `prompts/operator.md`；只管理任务及 Planner/Worker 自动化启停，不执行任务业务内容。新任务只写为 `DRAFT/UNINSPECTED`；最终 scope、能力等级、锁模式和技术验收只能由独立 Planner 预检提交。人工执行策略只允许 L5，且须由人工明确批准的一次性 Codex 执行，不形成第六个能力等级。
7. Planner 自动化的权威提示词是 `prompts/planner.md`；固定使用 `codex_automation`、Terra/high、5 分钟周期和初始化配置登记的 read-only/禁网/默认拒绝边界。每次只预留一个 DRAFT，只能通过宿主受控的 `loopctl.py preflight-*` stdin 通道写预检状态，不实现任务、不直接写 SQLite、不创建子任务或其他 Agent，也不管理自动化。首次建议 L5、manual、拆分、需求冲突或无法确定 scope 时必须进入 NEEDS_REVIEW；L5/manual 只有在 Operator 记录用户明确批准后才能复检 READY。
8. Worker 自动化的权威提示词是 `prompts/worker.md`；Codex 客户端自动化每次必须显式带 `runtime_environment=codex_automation` 和自身 `capability_level`（L1 至 L5）只调用一次 `claim`，且只能领取 `PENDING/READY` 任务。领取后必须核对 scope 锁凭证；修改新范围前必须由当前 execution 原子 `extend-scope` 成功。它只处理领取结果中的一个任务，不创建、继续或等待其他任务，也不读取或修改 Codex 自动化状态。旧 `execution_profile` 只作过渡期入口兼容，映射关系由提示词和初始化配置明确规定。
9. Worker 用 stdin 把结构化结果交给 `finish`；Planner 同样只用 stdin 提交预检结果；正常流程不得创建持久化 reports。
10. 健康检查由 Windows 任务计划程序直接运行 `health_run.py`，不得通过 Codex 自动化运行，也不领取任务。
11. 不物理删除任务；使用 `cancel` 保留审计历史。
12. 不直接写 SQLite 表；任务状态变更必须通过 `scripts/loopctl.py`。
13. 不读取或输出 `.env`、凭据、密钥、`$CODEX_HOME` 和 `.reasonix`。
14. 保留用户已有改动，遵守目标项目的 `AGENTS.md`，只修改任务 scope。
15. 自动执行不能生成 `CONFIRMED`；它只能由人工复核后的 `confirm` 命令产生。
16. 所有运行环境共享最多 8 个活动 execution，并同时受各平台 5 个活动 execution 上限约束；运行环境、优先级、能力等级、Provider、执行策略、依赖和 scope 锁彼此独立。
17. Codex 客户端 execution 的 heartbeat stalled、租约过期和 attempt timeout 独立判定；停滞或超时后 execution 必须离开活动容量，任务进入 `WAITING_HUMAN`，scope 进入 `QUARANTINED`。只有人工确认旧会话已结束后才能用 `recover` 释放隔离；迟到 heartbeat/finish 必须拒绝。
18. Planner 使用独立 `preflight_executions`，不占 Worker 容量、不获取业务 scope 写锁。Planner 只能读取并提交预检补充，不得覆盖 Operator 的 priority、业务描述、业务验收、运行环境、依赖或附件；超时预留自动回到 `DRAFT/UNINSPECTED`，迟到结果必须拒绝。
19. Planner 的拆分结果只是结构化建议；未经 Operator 取得人工决定，不得自动创建子任务、取消原任务或绕过预检进入 `PENDING`。本阶段不实现常驻 Dispatcher 或 Supervisor。
