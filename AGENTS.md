# Local Agent Loop 全局约束

> 本文件只定义所有角色共同遵守的不可绕过边界，不保存某个角色的具体工作步骤。
> 排查权限或职责冲突时，先读本文件确认全局上限，再进入对应角色目录的提示词，
> 最后沿 `control/loopctl.py` 绑定的 handler 检查控制代码；下层规则不得扩大这里的权限。

## 全局边界

1. 所有文本文件按 UTF-8 读写；时间统一使用带时区的 Asia/Shanghai ISO 8601。
2. 保留用户已有改动，遵守目标项目及 scope 路径适用的全部 `AGENTS.md`，只修改当前任务 scope。扩大范围前必须先通过当前执行入口取得有效 scope 锁。
3. 禁止读取或输出 `.env`、凭据、密钥、`$CODEX_HOME` 和 `.reasonix`。
4. 不直接写 SQLite 表。任务状态变更必须通过 `control/loopctl.py`；不物理删除任务，使用 `cancel` 保留审计历史；自动执行不得生成 `CONFIRMED`。
5. 不得绕过入口声明的运行环境、执行身份、能力等级、执行策略、状态机、scope 锁、租约、超时或隔离边界。入口事实缺失、冲突或凭证无效时停止并按对应角色协议报告。
6. 正常流程通过 UTF-8 stdin 提交 Planner 或 Worker 结构化结果，不创建持久化 report。

## 权威数据边界

- `data/loop-agent.sqlite3`：任务、Planner 预检及执行一致性数据的唯一事实来源；禁止重建运行时 `TASKS.json` 或 `INBOX.json`。
- `data/assets/`：任务原始附件和派生附件的统一存储目录；SQLite 只记录相对路径、摘要和用途，不嵌入文件内容。
- `data/backups/`：数据库迁移审计与灾难恢复快照目录；正常运行不得从备份读取任务。
- `data/runtime/`：PID、heartbeat、锁、日志和健康状态目录；内容可重建，不是任务事实源。
- `config/initialization.json`：内部运行环境、入口、模型映射、Scheduler 周期、执行参数、默认优先级和部署参数的唯一配置来源；不得把这些配置复制到 SQLite。
- `E:\code\根目录清单.md`：项目路由的实时来源，不得缓存到 SQLite。
- `data/runtime/health-state.json`：健康状态的唯一写入位置，不得把健康状态保存到 SQLite。

## 角色入口

先确认当前入口赋予的角色，再完整遵循对应权威提示词；不得把一个角色的权限扩展到另一个角色。

- 人工任务管理 Operator：`operator/operator.md`。只管理任务，不实现任务业务内容或管理外部客户端自动化。
- 独立静态预检 Planner：`planner/planner.md`。只做预检，不实现任务、创建子任务或管理 Scheduler。
- 通用单任务 Worker 协议：`worker/worker.md`。每次只领取并处理一个与入口身份匹配的 READY 任务。
- Self-hosted Agent Worker：宿主循环、队列领取、heartbeat、工具边界和 finish 由 `runner/agent_runtime.py` 负责，Provider 只实现中立模型协议适配。
- 健康检查：Windows 任务计划程序运行 `supervisor/run.ps1`，检查并在必要时恢复 `supervisor/main.py serve`；不领取任务。

详细状态、恢复、拆分、归档和执行规则属于对应角色提示词及控制代码；本文件只保存跨角色且稳定的共同边界。`README.md` 仅供人工导航，不是角色执行事实源。
