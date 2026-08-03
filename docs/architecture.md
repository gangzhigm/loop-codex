# Local Agent Loop 架构

## 数据边界

```text
Operator / Worker / Self-hosted Agent ---> data/loop-agent.sqlite3 <--- Dashboard Server ---> dashboard.html
                              ^                         ^
                              |                         |
                 config/initialization.json    runtime/health-state.json
                              ^                         ^
                              |                         |
                 Worker / 健康任务配置       Windows 任务计划程序

E:\code\根目录清单.md --实时解析项目路由--> loopctl / Dashboard Server
```

SQLite 只包含任务及其执行一致性表：`tasks`（包括任务所选的 `runtime_environment`、`execution_profile` 和独立的 nullable `archived_at`）、7 张任务子表、`executions`、`scope_locks` 和 `task_conflicts`。它不保存运行环境目录及入口配置、模型映射、自动化周期、metadata、settings、projects、change requests、health events 或 service state。

## Codex CLI Runner 边界

`scripts/codex_cli_runner.py` 是 `runtime_environment=codex_cli` 的单次入口，不是 Supervisor 或调度器。每次启动必须显式取得 `execution_profile`，生成唯一 execution ID，只调用一次 `claim --runtime-environment codex_cli --profile <profile>`；`NO_TASK`、`SLOT_FULL` 或 `CONFLICT` 立即退出且不启动模型进程。`CLAIMED` 后只接受全部 scope 能解析到同一个登记项目的任务，并把该项目作为唯一 `--cd` 工作目录；多项目、外部、未登记或不安全 scope 以 `WAITING_HUMAN` finish。

`scripts/codex_cli_dispatcher.py` 是轻量单一调度器，不是 Supervisor。它只读加载队列，按既有 `blocker`、`critical`、`high`、`medium`、`low`、`created_at` 和 ID 顺序选择首个依赖已满足的 `codex_cli` 候选档位；无候选、全局容量满或该档位容量满时不启动 Runner。它最多启动一次 Runner，并不自行 claim、重试其他档位或写入 SQLite。并发状态变化与 scope 冲突继续由 Runner 的原子 claim 裁决；Runner 返回 `NO_TASK`、`SLOT_FULL` 或 `CONFLICT` 后本轮结束。Dispatcher 日志仅记录时间、任务 ID、档位、结果、退出码或错误类型，绝不记录子进程输出、提示词、业务文件或认证信息。

Runner 用 UTF-8 stdin 向一个 `codex exec` 提交 `prompts/cli-worker.md` 和已领取任务的最小上下文，启用 `--json`、`--ephemeral`、明确模型、思考参数、沙箱、工作目录和临时 JSON Schema。`codex_cli.use_user_config=true`（默认）时 CLI 自行读取本机现有认证与 provider 配置，Runner 不读取、复制、输出或迁移其中内容；设为 `false` 才加入 `--ignore-user-config`。无论开关如何，Runner 显式传入的模型、推理档位、沙箱、`--cd` 与 output schema 保持任务边界，用户配置不能扩大 scope、工作目录或 sandbox。默认沙箱为 `workspace-write`，不使用 `--add-dir`、`--dangerously-bypass-approvals-and-sandbox` 或 `--dangerously-bypass-hook-trust`。CLI 子进程读取适用 `AGENTS.md`、检查 Git 改动并执行业务任务，但不得调用 Loop 队列协议；Runner 独占 claim、heartbeat 和 UTF-8 stdin finish。

stdout 与 stderr 由独立线程持续排空并只保留配置上限内的尾部内容，避免管道阻塞和无界内存；最终结果只从 JSONL 的已完成 `agent_message` 中解析，再复用现有 `SUCCEEDED`、`FAILED`、`WAITING_HUMAN` 契约校验。CLI 运行期间后台 heartbeat；总超时、心跳失败、中断或异常会终止进程树并以真实失败 finish。登录、账户或模型权限错误形成 `WAITING_HUMAN`，公开错误在写入任务前脱敏。认证仍由 Codex CLI 自身完成，Runner 不读取、复制或输出 Codex 用户配置、令牌和认证文件。

参数证据于 2026-08-03（Asia/Shanghai）核对：本机 `codex-cli 0.146.0` 的 `codex exec --help` 明确列出 stdin、`--json`、`--ephemeral`、`--ignore-user-config`、`--cd`、`--sandbox` 和 `--output-schema`，并把两个 bypass 参数标为危险。脱敏诊断确认：同一模型调用在附加 `--ignore-user-config` 时认证失败，保留用户配置时成功；未读取、复制、记录或输出任何凭据、认证文件路径或私有配置内容。官方参考入口为 [Non-interactive mode](https://developers.openai.com/codex/noninteractive) 与 [CLI reference](https://developers.openai.com/codex/cli/reference/)；当前环境访问官方 manual、页面和 Docs MCP 均为 HTTP 403，在线正文未能独立复核，此限制必须与已确认的本机事实区分。

## 自建 Agent 边界

`scripts/agent_runtime.py` 是不依赖 Codex 客户端自动化或 Codex CLI 的单次执行入口。Provider 只负责将外部模型请求与响应转换为中立协议：请求包含任务上下文、消息、工具 schema 和最终结果 schema；响应只能是 `tool_calls` 或 `final`。Runtime 不识别 DeepSeek 专有字段，负责一次 claim、任务路由复核、适用 `AGENTS.md` 与既有 Git 状态采集、工具执行、定时 heartbeat、结果契约校验和 UTF-8 stdin finish。

模型上下文只使用领取任务的 `id`、`description`、`scope`、`acceptance` 和已满足依赖标记，不传入完整队列状态。日志只记录步骤、工具名和错误类型，不记录模型密钥、Authorization、文件全文、完整提示词或隐藏推理。

工具层提供 UTF-8 文本读取、正则搜索、精确文本 patch/新文件创建和受限命令执行。所有路径在解析符号链接后必须位于领取 scope；绝对路径、`..`、`.env*`、`.reasonix`、版本控制元数据、常见密钥及凭据命名默认拒绝。命令不经过 shell，只允许固定形态的 `git status/diff` 和 `rg`，可执行文件必须从工作区外解析，并关闭本地 fsmonitor、外部 diff、textconv、子模块与 ripgrep 配置执行面。解释器、重定向、任意子进程和未知命令默认拒绝。

`delete`、`publish`、`git_commit`、`external_message`、`credential_access` 必须在当前任务文本中使用 `APPROVED_ACTIONS: action1,action2` 明确授权；缺少标记时 Runtime 直接以 `WAITING_HUMAN` finish。即使授权，未实现的高风险工具仍拒绝执行，避免把授权误当作实现。

## DeepSeek 提供方

`scripts/deepseek_provider.py` 将中立协议映射为 DeepSeek 的非流式 Chat Completions 请求，并把 `tool_calls` 或最终 JSON 转回中立响应。仅该 Provider 限制启动参数为 `runtime_environment=deepseek`，并根据初始化配置拒绝不支持的档位；因此 Codex 自动化和 Codex CLI 入口不能借此领取 DeepSeek 任务。密钥仅在已领取任务含有明确 `APPROVED_ACTIONS: credential_access` 时从 `deepseek.api_key_environment_variable` 指定的外部环境变量读取，缺失时失败，且日志与异常不包含值。

适配器只重试 429、500、502、503、504 和连接错误，次数与指数退避上限来自配置；401/403、格式错误、空响应、截断和未知结束原因不重试。因为 Provider 在交还中立工具调用前完成重试，运行时不会因重试重复执行已产生副作用的本地工具。返回的函数名和 JSON 参数仍在运行时按本地 allowlist 与精确参数结构校验，之后才进入 scope、敏感路径和人工批准检查。

官方资料于 2026-08-03（Asia/Shanghai）核对：

- [Chat Completions API](https://api-docs.deepseek.com/api/create-chat-completion)：`POST /chat/completions`、模型、工具调用、结束原因及客户端必须校验函数参数。
- [Tool Calls](https://api-docs.deepseek.com/guides/tool_calls)：模型不执行函数，客户端必须提交工具结果并继续对话。
- [Error Codes](https://api-docs.deepseek.com/quick_start/error_codes)：401、429、500、503 的官方语义和建议。

当前官方 API 页面列出的模型名和能力会变化；实现的默认模型是核对时列出的 `deepseek-v4-flash`。真实凭据、真实 API 调用和费用均未获授权，故只以本地假 HTTP 服务验证。

运行环境允许值、显示名称、入口参数、Worker 周期、健康任务周期、租约、并发数、Dashboard 地址和健康阈值只存在于 `config/initialization.json`。项目路由实时读取 `E:\code\根目录清单.md`。健康检查的当前状态和最近事件写入 `runtime/health-state.json`。浏览器不直接读取 SQLite，而是访问本机 HTTP 服务。

Dashboard 的写入面仅包含 `POST /api/task-action` 归档动作。请求必须且只能包含合法任务 ID、`action=archive` 和任务当前 `row_version`；服务端不接受命令、路径或 SQL，而是用固定参数调用 `loopctl.py confirm/archive`。乐观版本校验拒绝旧页面和并发状态变化；`SUCCEEDED` 严格先记录 `SUCCEEDED -> CONFIRMED` 人工确认，再写独立 `archived_at`，其他允许终态保持原状态直接归档。

## 并发模型

每个 Worker 唤起后必须使用自身 `runtime_environment` 和 `execution_profile` 调用一次 `claim`，两项都没有默认值。一个 `BEGIN IMMEDIATE` 事务完成过期恢复、全局与档位名额检查、同环境同档位候选选择、scope 冲突检测、execution 创建、scope 加锁和任务转为 `RUNNING`。Codex 客户端自动化固定声明 `codex_automation`，Codex CLI 与 DeepSeek 入口分别声明 `codex_cli` 和 `deepseek`。

系统默认最多允许 6 个活动 execution；各档位上限分别为 `routine=2`、`standard=3`、`advanced=2`、`deep=1`、`complex=1`、`exceptional=1`。全局与档位计数跨运行环境共同计算，档位上限之和不代表全局容量。任一限制达到时领取返回 `SLOT_FULL`，并通过 `limit_scope` 区分全局或档位。scope 默认归一到项目：

```text
rs/rs-mall4pc-pro/src/views/cart/index.vue -> project:rs/rs-mall4pc-pro
holding/frontend/src/App.tsx               -> project:holding
OSS:bucket/path/file.xlsx                  -> external:OSS:bucket/path/file.xlsx
```

同一项目任务默认互斥，运行环境不同也不会绕过 scope 锁。`claim` 在当前环境与档位内按队列顺序扫描依赖就绪任务；冲突任务保存 blocker 信息并进入 `WAITING_CONFLICT`，随后继续寻找同环境同档位其他 scope 可执行的任务。只有所有匹配且依赖就绪的候选都冲突时，本轮 Worker 才返回 `CONFLICT`。阻塞 execution 完成、心跳超时或租约过期后，任务自动回到 `PENDING`。

## 状态机

```text
DRAFT --人工重排--> PENDING --领取--> RUNNING --> SUCCEEDED --人工复核--> CONFIRMED
                       |                  |  |
                       |                  |  +--> FAILED
                       |                  +-----> WAITING_HUMAN --人工重排--> PENDING
                       +--冲突--> WAITING_CONFLICT --冲突解除--> PENDING

RUNNING --租约过期且仍可重试--> PENDING
RUNNING --租约过期且达到上限--> FAILED
非 RUNNING 状态 --人工取消--> CANCELLED
```

依赖只有在上游为 `SUCCEEDED` 或 `CONFIRMED` 时满足。自动执行结果只允许 `SUCCEEDED`、`FAILED`、`WAITING_HUMAN`；`CONFIRMED` 只能人工产生。

归档不属于状态机。`archived_at IS NULL` 表示未归档，带 Asia/Shanghai 时区的 ISO 8601 时间表示已归档。`CONFIRMED` 只表示人工复核通过，不会隐式写入 `archived_at`。人工 `archive/unarchive` 只修改该属性，并以原状态到同一原状态的管理事件记录 actor、时间和 reason；允许归档的终态为 `CONFIRMED`、`FAILED` 和 `CANCELLED`。

## 顺序与租约

候选在各自运行环境与执行档位内按 `blocker`、`critical`、`high`、`medium`、`low`，再按 `created_at` 和 id 排序。仅 `PENDING` 且依赖完成的任务可领取。运行环境、优先级和执行档位相互独立，高优先级不会自动升高模型档位。`NO_TASK`、`SLOT_FULL` 或 `CONFLICT` 都应立即结束。

五个普通档位各由一条 `codex_automation` Codex 定时自动化驱动，默认每 20 分钟错峰运行；`exceptional` 没有定时自动化，只能由 Operator 在人工明确批准后创建一次性执行。`codex_cli` 与 `deepseek` 由各自 Runner 显式领取，不由这些自动化兜底。Codex 自动化不能可靠地暂停或恢复自身或其他自动化，因此本阶段不做无任务自动暂停；`NO_TASK` 只结束当前轮次，Worker 不读取或修改自动化状态。

默认心跳超时 300 秒、租约 3600 秒。Worker 在阅读完成后、编辑前以及长命令前后调用 `heartbeat`。后续 `claim` 会回收心跳超时或租约过期的 execution，释放其 scope 锁，并按最大尝试次数重排或失败。正常结束将 UTF-8 JSON 通过 stdin 交给 `finish`；`finish` 在同一事务中保存结果、释放 scope 锁并重新排队已解除冲突的任务，不生成 report 文件，也不自动归档任务。

自建 Agent 在整个模型和工具循环期间使用后台心跳，并在各工具调用前后主动续租。模型/工具超时、心跳失败、进程中断、最大步骤耗尽或无效结构化结果会形成真实 `FAILED`；缺少高风险批准会形成 `WAITING_HUMAN`。只有 `finish` 返回 `FINISHED` 才视为状态更新成功。

Codex CLI Runner 同样只有在 `finish` 返回 `FINISHED` 后才视为任务状态已更新；它不轮询或领取第二项，也不创建持久化 report、`CONFIRMED` 或 `archived_at`。正常退出后不保留 CLI 会话，超时和中断路径会回收进程树；若操作系统拒绝终止或 `finish` 自身不可用，Runner 只能报告真实运行错误，后续领取仍由既有心跳/租约机制回收 execution 与 scope 锁。

Schema 3.0.0、3.1.0 或 3.2.0 到 3.3.0 的迁移使用受控的 `loopctl.py migrate`。迁移要求没有活动 execution，并重建任务表加入非空 `runtime_environment`；全部既有任务回填 `codex_automation`。3.2.0 的 `execution_profile`、状态、结果、归档属性、行版本、任务子表与 execution 历史原样保留；3.0.0/3.1.0 的档位按旧迁移规则回填 `standard`。从 3.0.0 升级时仍仅为已有 `CONFIRMED` 任务按旧语义回填归档时间。迁移结束后执行外键与完整性检查，再更新为 Schema 3.3.0；重复运行返回已是当前版本。

## 安全边界

scope 必须相对 `E:\code` 并匹配项目清单中最长的项目路径。绝对路径、`..`、`$CODEX_HOME`、`.reasonix`、`.env` 和未登记项目会被拒绝。Worker 还必须读取目标项目适用的 `AGENTS.md`、检查 Git 工作树、保留既有改动并只处理已领取 scope。

删除、发布、Git 提交、外部消息和凭据访问需要明确人工授权；授权应体现在当前任务内容或 Operator 的明确指令中。缺少授权时以 `WAITING_HUMAN` 完成本轮。

## 服务健康

Dashboard Server 默认绑定 `127.0.0.1:4178`：

- `/`：监控页。
- `/api/state`：合并任务库、运行环境与入口配置、项目清单和运行时健康状态；任务与活动 execution 都返回 `runtime_environment`。
- `/api/task-action`：仅接受 POST 归档动作，校验任务 ID 与 `row_version` 后复用 `loopctl.py` 状态机。
- `/healthz`：Schema、任务数和活动 execution 健康信息。

Windows 任务计划程序默认每 30 分钟直接运行一次 `health_run.py`，不调用 Codex 模型。服务正常时写入 `HEALTHY`；不可用时尝试恢复；连续达到阈值后写入 `NEEDS_ATTENTION`，但仍继续尝试自愈。Windows 恢复流程会核对端口监听 PID 的命令行，只停止本项目 Dashboard，并禁止多个 Dashboard 共享监听端口。健康状态保存在 `runtime/health-state.json`，独立短时锁避免健康任务重入。
