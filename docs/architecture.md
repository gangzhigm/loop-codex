# Local Agent Loop 架构

## 数据边界

```text
Operator / Worker / Runner --------------------> data/loop-agent.sqlite3 <--- Dashboard Server ---> dashboard.html
Planner(read-only source sandbox) --受控 preflight stdin--^                         ^
                              |                                                   |
                 config/initialization.json                              runtime/health-state.json
                              ^                         ^
                              |                         |
                 Worker / 健康任务配置       Windows 任务计划程序

E:\code\根目录清单.md --实时解析项目路由--> loopctl / Dashboard Server

config/initialization.json --backend/service/secret_ref--> SecretStore ---> OS keyring
                                                               `-------> explicit process environment
```

SQLite 只包含任务、预检与执行一致性表。`tasks` 分开保存 Operator 原始业务事实和 Planner 补充：原始描述、业务验收、priority、环境、依赖、附件、scope hint 与预估等级不会被 Planner 覆盖；最终能力等级、精确 scope、锁模式、技术验收、证据和拆分建议属于预检结果。Schema 3.7.0 增加独立 `preflight_status`、`preflight_executions`、技术验收与证据子表；Worker `executions` 显式使用 `execution_kind=WORKER`，Planner 使用 `execution_kind=PLANNER`。Worker 的 `STALLED/TIMED_OUT` 与 scope `ACTIVE/QUARANTINED` 恢复语义保持不变。数据库不保存密钥、Authorization、源码内容、隐藏推理、运行环境部署配置、模型映射、自动化周期或 service state。

## Planner 预检边界

新任务固定以 `DRAFT/UNINSPECTED` 创建。独立 Planner 自动化默认每 5 分钟以 Terra/high 唤起，必须运行在 `read-only`、禁网、`approval_policy=never` 和默认拒绝工具边界中。它通过 `preflight-claim ... --runtime-environment codex_automation --sandbox read-only` 原子预留一个 DRAFT，在独立表中形成唯一活动 execution；不计入全局 8/平台 5 的 Worker 容量，也不获取业务 scope 写锁。claim 只返回 Operator 业务定义和公开客户端边界，不把队列历史或其他任务状态交给 Planner。

业务项目文件、配置、Git 状态和用户文件只允许静态读取。唯一状态写入是宿主按初始化配置精确允许的 `loopctl.py preflight-claim|heartbeat|ready|needs-review|fail`，结果必须经 UTF-8 stdin 提交；Planner 不能直接写 SQLite、报告文件或业务文件。`config/initialization.json`、`prompts/planner.md`、配置加载校验、回归测试与 `check-initialization.ps1` 共同验证该部署契约。真实 Codex 自动化的创建/切换仍由 Operator 完成，普通 Planner/Worker 无权修改自动化。

Planner READY 在一个事务中写入最终能力等级、精确 scope、`file|module|project` 锁模式、技术验收补充和检查证据，并形成 `DRAFT -> PENDING`。首次 L5/manual、信息不足、需求冲突、无法安全确定 scope 或需要人工拆分决定时，任务进入 `NEEDS_REVIEW`。用户明确批准 L5/manual 后，Operator 把对应 `APPROVED_PLANNER_ESCALATION` 标记写回业务定义；Planner 复检和控制面门禁同时通过才可 READY。结构化拆分建议不创建、取消或替换任何任务。

Planner heartbeat、lease 与 attempt deadline 独立。heartbeat stalled、lease expiry 或 attempt timeout 会把只读 execution 标为 `TIMED_OUT` 并自动释放预留，使任务回到 `DRAFT/UNINSPECTED`；这不需要 Worker 那种人工 scope 解隔离，因为 Planner 没有写锁。execution ID、task `row_version` 与任务上的 `preflight_execution_id` 共同 fencing，迟到结果不能覆盖新预检。Planner 是定时唤起、每轮一次预检的 Codex 自动化，不是常驻进程、Dispatcher 或 Supervisor。

## SecretStore 边界

`scripts/secret_store.py` 是 Dashboard、初始化命令、Supervisor/Runtime 和 Provider 唯一允许使用的密钥访问契约，统一提供 `set/get/status/verify/rotate/delete`。状态与审计结果只包含 backend、`secret_ref`、可用性、是否变化和 Asia/Shanghai 时间，不返回原值、掩码、后四位或可逆材料。`config/initialization.json` 只保存 `secret_management.backend/service/access_account` 与 Provider 的 `secret_ref` 等非敏感引用。

Dashboard 的 `/api/secrets` 是浏览器到 SecretStore 的本机受控层。GET 状态把内部 `secret_ref` 收敛为 provider_id、configured、backend、status、last_validated_at 等公开字段；写操作直接调用同一个 SecretStore，不创建 Loop 任务、不写 SQLite、不转交 Operator/Worker。设置、轮换和验证结果只把 Provider、操作、验证范围、公开状态与 Asia/Shanghai 时间作为事件写入 `runtime/health-state.json`，健康任务会继续保留这些非敏感事件。

`os_keyring` 在 Windows 通过 WinCred API 使用当前登录账户的 Credential Manager；macOS/Linux 使用系统 keyring 适配器，分别要求可用的 Keychain 与 Secret Service 后端。后端缺失、锁定、权限不足或账户与 `access_account` 不符时 fail closed，不会回退到 JSON、SQLite、日志或明文文件。`environment` 只有在配置显式选择时生效，把 `secret_ref` 解释为当前进程变量名；它不持久化，也不能由子进程初始化命令写回父进程。

轮换在进程级同引用锁内完成：候选值先写入同一安全后端的临时引用，回读并完成格式与可选连接验证后才替换主引用；提交验证失败时恢复旧值并清理候选引用。底层系统密钥库不提供跨进程事务，因此外部并发写入者仍必须由部署编排串行化；代码不会把这一限制伪装成分布式原子事务。外部 Secret Manager 目前只定义 `SecretBackendAdapter` 的能力检测与读写删除契约，未配置具体服务器适配器时明确报未实现。

## Codex CLI Runner 边界

`scripts/codex_cli_runner.py` 是 `runtime_environment=codex_cli` 的单次入口，不是 Supervisor 或调度器。每次启动必须显式取得 `capability_level` 与 `execution_policy`，生成唯一 execution ID，只调用一次 `claim --runtime-environment codex_cli --capability-level <level> --execution-policy <policy>`；旧 `--profile` 仅作显式兼容映射。`NO_TASK`、`SLOT_FULL` 或 `CONFLICT` 立即退出且不启动模型进程。`CLAIMED` 后只接受全部 scope 能解析到同一个登记项目的任务，并把该项目作为唯一 `--cd` 工作目录；多项目、外部、未登记或不安全 scope 以 `WAITING_HUMAN` finish。

`scripts/codex_cli_dispatcher.py` 是轻量单一调度器，不是 Supervisor。它只读加载队列，按既有 `blocker`、`critical`、`high`、`medium`、`low`、`created_at` 和 ID 顺序选择首个依赖已满足的 `codex_cli` 候选能力等级；无候选、全局容量满或平台容量满时不启动 Runner。它最多启动一次 Runner，并不自行 claim、重试其他等级或写入 SQLite。并发状态变化与 scope 冲突继续由 Runner 的原子 claim 裁决；Runner 返回 `NO_TASK`、`SLOT_FULL` 或 `CONFLICT` 后本轮结束。Dispatcher 日志仅记录时间、任务 ID、能力等级、结果、退出码或错误类型，绝不记录子进程输出、提示词、业务文件或认证信息。

Runner 用 UTF-8 stdin 向一个 `codex exec` 提交 `prompts/cli-worker.md` 和已领取任务的最小上下文，启用 `--json`、`--ephemeral`、明确模型、思考参数、沙箱、工作目录和临时 JSON Schema。`codex_cli.use_user_config=true`（默认）时 CLI 自行读取本机现有认证与 provider 配置，Runner 不读取、复制、输出或迁移其中内容；设为 `false` 才加入 `--ignore-user-config`。无论开关如何，Runner 显式传入的模型、推理档位、沙箱、`--cd` 与 output schema 保持任务边界，用户配置不能扩大 scope、工作目录或 sandbox。默认沙箱为 `workspace-write`，不使用 `--add-dir`、`--dangerously-bypass-approvals-and-sandbox` 或 `--dangerously-bypass-hook-trust`。CLI 子进程读取适用 `AGENTS.md`、检查 Git 改动并执行业务任务，但不得调用 Loop 队列协议；Runner 独占 claim、heartbeat 和 UTF-8 stdin finish。

stdout 与 stderr 由独立线程持续排空并只保留配置上限内的尾部内容，避免管道阻塞和无界内存；最终结果只从 JSONL 的已完成 `agent_message` 中解析，再复用现有 `SUCCEEDED`、`FAILED`、`WAITING_HUMAN` 契约校验。CLI 运行期间后台 heartbeat；总超时、心跳失败、中断或异常会终止进程树并以真实失败 finish。登录、账户或模型权限错误形成 `WAITING_HUMAN`，公开错误在写入任务前脱敏。认证仍由 Codex CLI 自身完成，Runner 不读取、复制或输出 Codex 用户配置、令牌和认证文件。

参数证据于 2026-08-03（Asia/Shanghai）核对：本机 `codex-cli 0.146.0` 的 `codex exec --help` 明确列出 stdin、`--json`、`--ephemeral`、`--ignore-user-config`、`--cd`、`--sandbox` 和 `--output-schema`，并把两个 bypass 参数标为危险。脱敏诊断确认：同一模型调用在附加 `--ignore-user-config` 时认证失败，保留用户配置时成功；未读取、复制、记录或输出任何凭据、认证文件路径或私有配置内容。官方参考入口为 [Non-interactive mode](https://developers.openai.com/codex/noninteractive) 与 [CLI reference](https://developers.openai.com/codex/cli/reference/)；当前环境访问官方 manual、页面和 Docs MCP 均为 HTTP 403，在线正文未能独立复核，此限制必须与已确认的本机事实区分。

## 自建 Agent 边界

`scripts/agent_runtime.py` 是不依赖 Codex 客户端自动化或 Codex CLI 的单次执行入口。它从初始化配置创建一次 SecretStore 并通过 `factory(config=..., secret_store=...)` 注入 Provider；Provider 工厂不接受该契约时启动失败。Provider 只负责将外部模型请求与响应转换为中立协议：请求包含任务上下文、消息、工具 schema 和最终结果 schema；响应只能是 `tool_calls` 或 `final`。Runtime 不识别 DeepSeek 专有字段，负责一次 claim、任务路由复核、适用 `AGENTS.md` 与既有 Git 状态采集、工具执行、定时 heartbeat、结果契约校验和 UTF-8 stdin finish。

模型上下文只使用领取任务的 `id`、`description`、`scope`、`acceptance` 和已满足依赖标记，不传入完整队列状态。日志只记录步骤、工具名和错误类型，不记录模型密钥、Authorization、文件全文、完整提示词或隐藏推理。Provider 失败只有继承 Runtime 受信任诊断契约时才能进入事件及 `FAILED`/`WAITING_HUMAN` 结果；固定字段为 `category`、`http_status`、`retryable`、`retry_exhausted`、`finish_reason`、`agent_attempt` 和 `model_step`。未知异常只降级为固定消息与异常类型，绝不透传异常文本。

对 `finish_reason=stop` 的 final，Provider 和 Runtime 共享 value-free `final_shape` 诊断：只含 content Unicode 字符长度、`invalid_json`/`parsed`/`unavailable` 状态、顶层类型、十个固定允许列表字段的存在性/类型和未知字段数量；不含字段值或未知字段名。Provider 在 JSON 解析失败或顶层非 object 时附加它，Runtime 在 required `summary` 等终态契约失败时以 `final_schema` 保留该 shape。Runtime 可发起最多一次不带工具的专用 final 修复请求；修复不重放工具、不启动新的 Agent attempt，也不包含被拒绝 final，仍失败时保留最新 shape 并补入 attempt/step 上下文。

工具层提供 UTF-8 文本读取、正则搜索、精确文本 patch/新文件创建和受限命令执行。所有路径在解析符号链接后必须位于领取 scope；绝对路径、`..`、`.env*`、`.reasonix`、版本控制元数据、常见密钥及凭据命名默认拒绝。命令不经过 shell，只允许固定形态的 `git status/diff` 和 `rg`，可执行文件必须从工作区外解析，并关闭本地 fsmonitor、外部 diff、textconv、子模块与 ripgrep 配置执行面。解释器、重定向、任意子进程和未知命令默认拒绝。

`delete`、`publish`、`git_commit`、`external_message`、`credential_access` 必须在当前任务文本中使用 `APPROVED_ACTIONS: action1,action2` 明确授权；缺少标记时 Runtime 直接以 `WAITING_HUMAN` finish。即使授权，未实现的高风险工具仍拒绝执行，避免把授权误当作实现。

## DeepSeek 提供方

`scripts/deepseek_provider.py` 将中立协议映射为 DeepSeek 的非流式 Chat Completions 请求，并把 `tool_calls` 或最终 JSON 转回中立响应。仅该 Provider 限制启动参数为 `runtime_environment=self_hosted_agent`、`provider_id=deepseek`，并根据初始化配置拒绝不支持的能力等级；因此 Codex 自动化和 Codex CLI 入口不能借此领取 DeepSeek 任务。密钥仅在已领取任务含有明确 `APPROVED_ACTIONS: credential_access` 时通过注入的 SecretStore 读取，在请求结束后释放本地引用；密钥不会进入命令行、环境快照、工具输出、日志或公开异常链。

适配器只重试 429、500、502、503、504、连接和请求超时，次数与指数退避上限来自配置；401/403、400、格式错误、空响应、截断和未知结束原因不重试。每个失败被映射为允许列表类别，不会携带上游文本：鉴权、限流、服务端、连接、请求超时、空或畸形响应、截断响应、无效工具调用、无效最终 JSON、本地协议或未知结束原因。`deepseek.max_retries` 只计算同一模型步骤内首次 HTTP 请求之后的额外请求；execution profile 的 `max_retries` 只计算首次完整 Agent attempt 之后的额外 attempt。Provider 请求耗尽后仅瞬态类别可进入后一层，并且任何已发生本地副作用的 attempt 都禁止重启；确定性和无法分类的错误不重启工具循环。Provider 与 Runtime 分别记录 `provider_request_retry`/`provider_request_retries_exhausted` 和 `agent_attempt_retry`。因为 Provider 在交还中立工具调用前完成请求重试，运行时不会因该层重试重复执行本地工具。返回的函数名和 JSON 参数仍在运行时按本地 allowlist 与精确参数结构校验，之后才进入 scope、敏感路径和人工批准检查。诊断字段不包含 API key、Authorization、请求/响应正文、工具参数值、业务文件内容或隐藏推理。

多轮工具消息按 `assistant(tool_calls) -> tool(tool_call_id)` 成对追加；重复只读调用保留各自 ID 与顺序。Provider 要求最终 HTTP 响应为 JSON object，Runtime 再校验协议 1.0 终态；`max_steps` 只计算模型轮次，Provider 内部请求重试不消耗模型轮次。达到 `max_steps`、attempt timeout 或终态契约失败会形成具体且脱敏的 `FAILED/WAITING_HUMAN`，不会降级成裸 `DeepSeekProviderError`。本地假 HTTP 服务已覆盖这些边界；此前两次真实失败没有可用于分类的原始响应证据，不能据此声明某个类别是其根因。

官方资料于 2026-08-03（Asia/Shanghai）核对：

- [Chat Completions API](https://api-docs.deepseek.com/api/create-chat-completion)：`POST /chat/completions`、模型、工具调用、结束原因及客户端必须校验函数参数。
- [Tool Calls](https://api-docs.deepseek.com/guides/tool_calls)：模型不执行函数，客户端必须提交工具结果并继续对话。
- [Error Codes](https://api-docs.deepseek.com/quick_start/error_codes)：401、429、500、503 的官方语义和建议。

当前官方 API 页面列出的模型名和能力会变化；实现的默认模型是核对时列出的 `deepseek-v4-flash`。真实凭据、真实 API 调用和费用均未获授权，故只以本地假 HTTP 服务验证。

SecretStore 平台依据于 2026-08-05（Asia/Shanghai）核对，以下官方入口均返回 HTTP 200：[Windows CredWrite](https://learn.microsoft.com/en-us/windows/win32/api/wincred/nf-wincred-credwritew)、[Apple Keychain Services](https://developer.apple.com/documentation/security/keychain-services)、[freedesktop Secret Service](https://specifications.freedesktop.org/secret-service/latest/)。这些来源确认平台 API/服务边界；具体桌面会话是否已解锁、Linux 是否安装可用 keyring 适配器仍由运行时能力检测裁决。

运行环境允许值、显示名称、入口参数、Worker 周期、健康任务周期、租约、并发数、Dashboard 地址和健康阈值只存在于 `config/initialization.json`。项目路由实时读取 `E:\code\根目录清单.md`。健康检查的当前状态和最近事件写入 `runtime/health-state.json`。浏览器不直接读取 SQLite，而是访问本机 HTTP 服务。

Dashboard 的任务写入面是 `POST /api/task-action`。归档请求只接受合法任务 ID、`action=archive` 和当前 `row_version`；恢复请求只接受固定的 task/execution ID、`action=recover`、`recovery_action=requeue|failed|wait`、当前 `row_version` 与明确安全确认。服务端不接受命令、路径或 SQL，而是用固定参数调用 `loopctl.py`。恢复事务再次校验 execution fencing，页面会分开展示任务 `WAITING_HUMAN`、execution `STALLED/TIMED_OUT`、scope `QUARANTINED` 和已释放活动容量。

Secret 写入面是 `POST /api/secrets`，只接受初始化配置中登记的 Provider 和 `set/rotate/verify/delete` 固定动作。Server 强制绑定 `127.0.0.1`，校验精确 Host、同源 Origin、CSRF token、JSON 类型、请求体上限、动作字段和一次性 UUID；拒绝 CORS 预检、跨站表单、DNS rebinding 和重复请求。连接验证及替换/删除需要前端人工确认和服务端确认字；浏览器不直接访问 Provider。密码值只存在于当前请求，输入不预填，提交后清空 DOM 和临时变量，不写 URL、localStorage、sessionStorage、IndexedDB、缓存或附件。Secret 路径日志只记录规范路径、方法与状态码。

这套边界只适用于本机同源管理。任何远程服务器管理都必须等待服务器 Secret 后端提供 HTTPS、认证、授权和审计；修改 Dashboard host、端口映射或反向代理不能替代这些控制，非 `127.0.0.1` 监听会被 Server 拒绝。

## 并发模型

每个 Worker 唤起后必须使用自身 `runtime_environment`、`capability_level` 和 `execution_policy` 调用一次 `claim`，三项都没有默认值。一个 `BEGIN IMMEDIATE` 事务完成过期恢复、全局与平台名额检查、同环境、Provider、能力等级和策略候选选择、scope 冲突检测、execution 创建、scope 加锁和任务转为 `RUNNING`。Codex 客户端自动化固定声明 `codex_automation`；Codex CLI 与自建 Agent 分别声明 `codex_cli` 与 `self_hosted_agent`。

系统默认最多允许 8 个活动 execution；`codex_automation`、`codex_cli` 和 `self_hosted_agent` 各有最多 5 个活动 execution。能力等级不再作为并发槽位；它与 priority、运行环境、Provider 和执行策略独立。任一限制达到时领取返回 `SLOT_FULL`，并通过 `limit_scope` 区分全局或平台。scope 默认归一到项目：

```text
rs/rs-mall4pc-pro/src/views/cart/index.vue -> project:rs/rs-mall4pc-pro
holding/frontend/src/App.tsx               -> project:holding
OSS:bucket/path/file.xlsx                  -> external:OSS:bucket/path/file.xlsx
```

同一项目任务默认互斥，运行环境不同也不会绕过 scope 锁。`claim` 在当前环境、Provider、能力等级和执行策略内按队列顺序扫描依赖就绪任务；`ACTIVE` 与 `QUARANTINED` 锁都构成冲突。冲突任务保持 `PENDING`，blocker、blocked scope 与 queue position 由读取时动态投影，claim 随后继续寻找其他 scope。只有所有依赖就绪候选都冲突时返回 `CONFLICT`；旧 `WAITING_CONFLICT` 仅在迁移时恢复为 PENDING 并保留审计历史。

## 状态机

```text
DRAFT/UNINSPECTED --Planner 预留--> DRAFT/INSPECTING
DRAFT/INSPECTING --READY--> PENDING/READY --Worker 领取--> RUNNING --> SUCCEEDED --人工复核--> CONFIRMED
        |                    |                                      |  |
        |                    +--需决定/失败--> NEEDS_REVIEW/FAILED  |  +--> FAILED
        |                                      |                    +-----> WAITING_HUMAN --人工重排--> PENDING
        +--Planner 超时恢复--> DRAFT/UNINSPECTED                    +--scope 冲突--> PENDING（动态 blocker）
NEEDS_REVIEW --Operator 补充/重排--> DRAFT/UNINSPECTED

RUNNING --受控 Runner 确认终止并选择重试--> PENDING
RUNNING --受控 Runner 确认终止并选择失败--> FAILED
非 RUNNING 状态 --人工取消--> CANCELLED
```

Codex 客户端存活性分支独立于普通业务完成分支：

```text
task RUNNING + execution RUNNING + scope ACTIVE
  -- heartbeat stalled 或 lease expiry --> task WAITING_HUMAN + execution STALLED + scope QUARANTINED
  -- attempt timeout -------------------> task WAITING_HUMAN + execution TIMED_OUT + scope QUARANTINED
STALLED -- 后续 attempt timeout --------> TIMED_OUT（容量保持释放，scope 继续隔离）
人工确认旧会话结束 -- requeue/failed --> 释放旧隔离；wait --> 保持隔离
```

依赖只有在上游为 `SUCCEEDED` 或 `CONFIRMED` 时满足。普通 Worker claim 只接受 `PENDING/READY` 且最终等级、scope、锁模式、技术验收和证据完整的任务。自动执行结果只允许 `SUCCEEDED`、`FAILED`、`WAITING_HUMAN`；`CONFIRMED` 只能人工产生。

归档不属于状态机。`archived_at IS NULL` 表示未归档，带 Asia/Shanghai 时区的 ISO 8601 时间表示已归档。`CONFIRMED` 只表示人工复核通过，不会隐式写入 `archived_at`。人工 `archive/unarchive` 只修改该属性，并以原状态到同一原状态的管理事件记录 actor、时间和 reason；允许归档的终态为 `CONFIRMED`、`FAILED` 和 `CANCELLED`。

## 顺序与租约

候选在各自运行环境、Provider、能力等级和执行策略内按 `blocker`、`critical`、`high`、`medium`、`low`，再按 `created_at` 和 id 排序。仅 `PENDING/READY`、最终执行契约完整且依赖完成的任务可领取。DRAFT、INSPECTING、FAILED preflight 与 NEEDS_REVIEW 均不会进入 Worker 候选。运行环境、优先级、能力等级、Provider 和执行策略相互独立，高优先级不会自动升高能力等级。`NO_TASK`、`SLOT_FULL`、`CONFLICT` 或 `RECOVERY_REQUIRED` 都应立即结束。

独立 Planner 由一条 `codex_automation` 定时自动化每 5 分钟唤起，只扫描 DRAFT；五个 L1-L5 automatic Worker 默认每 20 分钟按 0、2、4、6、8 分钟错峰运行，只扫描 PENDING/READY。`L5/manual` 没有定时自动化，只能由 Operator 人工批准一次性执行。`codex_cli` 与 `self_hosted_agent` 由各自 Runner 显式领取，不由客户端自动化兜底。真实 Planner 的创建和五条 Worker 的切换只能由 Operator 在维护窗口完成并复核；Planner/Worker 不读取或修改自动化状态。

默认 heartbeat stalled 阈值为 300 秒、可续租 lease 为 3600 秒；attempt timeout 由 execution 快照独立定义，续心跳不会重置 attempt 计时。每次 `claim` 在容量判断前推进三项计时状态：Codex execution 停滞或超时后转为非活动 `STALLED/TIMED_OUT`，任务转为 `WAITING_HUMAN`，活动容量立即释放；scope lock 则转为 `QUARANTINED`，租约到期也不自动删除。heartbeat stalled 只表示旧客户端会话存活性未知，不能归类为实现/模型失败或能力等级不足。

人工确认旧 Codex 会话已结束后，`recover --human-confirmed-safe --action requeue|failed|wait` 在一个事务中处置 execution、task、scope lock、冲突任务和历史。`requeue/failed` 仅按旧 execution ID 删除其隔离，`wait` 保持隔离；row version 与 execution ID 共同 fencing，已 `STALLED/TIMED_OUT` 的 execution 无法 heartbeat/finish，也不能删除后续新 attempt 的 ACTIVE 锁。受控 Runner 使用 `--runner-confirmed-terminated`，因为它能确认旧进程树退出，不需要套用 Codex 客户端的人工存活性判断。

自建 Agent 在整个模型和工具循环期间使用后台心跳，并在各工具调用前后主动续租。模型/工具超时、心跳失败、进程中断、最大步骤耗尽或无效结构化结果会形成真实 `FAILED`；缺少高风险批准会形成 `WAITING_HUMAN`。只有 `finish` 返回 `FINISHED` 才视为状态更新成功。

Codex CLI Runner 同样只有在 `finish` 返回 `FINISHED` 后才视为任务状态已更新；它不轮询或领取第二项，也不创建持久化 report、`CONFIRMED` 或 `archived_at`。正常退出后不保留 CLI 会话，超时和中断路径会回收进程树；若操作系统拒绝终止或 `finish` 自身不可用，Runner 只能报告真实运行错误，后续领取仍由既有心跳/租约机制回收 execution 与 scope 锁。

Schema 3.0.0 至 3.6.0 到 3.7.0 的迁移使用受控的 `loopctl.py migrate`。3.0.0 至 3.3.0 要求没有活动 Worker execution；3.4.0 至 3.6.0 允许原样保留活动 Worker execution 与 ACTIVE scope lock。旧 DRAFT 转为 NEEDS_REVIEW 并记录迁移原因；旧 PENDING 及具备 scope 的既有任务标记为 READY，历史终态缺少 scope 时保留终态但不伪称 READY。迁移保留任务事实、execution、历史、结果、依赖、scope、附件、归档和 row version，并执行外键与 quick check；不会自动创建 Planner execution、隔离或未来调度服务。

## 安全边界

scope 必须相对 `E:\code` 并匹配项目清单中最长的项目路径。绝对路径、`..`、`$CODEX_HOME`、`.reasonix`、`.env` 和未登记项目会被拒绝。Planner 只能静态读取项目并通过受控 preflight 通道提交范围；Worker 还必须读取目标项目适用的 `AGENTS.md`、检查 Git 工作树、核对 claim 返回的锁凭证，并只处理已领取 scope。需要新增范围时，当前 execution 必须先原子 `extend-scope`，冲突或拒绝时不得写入。

删除 Git 已跟踪、execution 前已存在或归属不明的文件，以及发布、Git 提交、外部消息和凭据访问，需要明确人工授权；授权应体现在当前任务内容或 Operator 的明确指令中。Codex 自动化与 Codex CLI 可自行清理本次 execution 在任务登记项目内新生成且仍未被 Git 跟踪的普通临时文件，但必须有执行前状态、具体生成命令和时间证据，只能逐个使用精确路径，且不得删除目录、符号链接/重解析点、源码、配置、凭据或用户数据。通配符、递归删除、跨项目清理和归属不明的文件仍必须进入 `WAITING_HUMAN`。自建 Agent Runtime 未实现删除工具，即使满足条件也不会获得隐式删除能力。

## 服务健康

Dashboard Server 默认绑定 `127.0.0.1:4178`：

- `/`：监控页。
- `/api/state`：合并任务库、运行环境与入口配置、项目清单和运行时健康状态；任务与活动 execution 都返回 `runtime_environment`。
- `/api/task-action`：接受固定结构的 POST 归档或安全恢复动作，校验 task/execution fencing 与 `row_version` 后复用 `loopctl.py` 状态机。
- `/api/secrets`：GET 返回非敏感 Provider Secret 状态，POST 通过同源安全门禁调用统一 SecretStore。
- `/healthz`：Schema、任务数和活动 execution 健康信息。

Windows 任务计划程序默认每 30 分钟直接运行一次 `health_run.py`，不调用 Codex 模型。服务正常时写入 `HEALTHY`；不可用时尝试恢复；连续达到阈值后写入 `NEEDS_ATTENTION`，但仍继续尝试自愈。Windows 恢复流程会核对端口监听 PID 的命令行，只停止本项目 Dashboard，并禁止多个 Dashboard 共享监听端口。健康状态保存在 `runtime/health-state.json`，独立短时锁避免健康任务重入。
