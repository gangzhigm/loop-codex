# Local Agent Loop

`E:\code\local-agent-loop` 是 `E:\code` 下跨项目任务的并发执行中心。Operator 管理任务，Codex Worker 执行任务，Windows 健康任务维护 Dashboard Server。

## 固定配置

- 任务数据库：`E:\code\local-agent-loop\data\loop-agent.sqlite3`（Schema 3.5.0）
- 初始化配置：`config/initialization.json`
- 项目清单：`E:\code\根目录清单.md`
- Worker：L1 至 L5 五条 automatic 定时自动化各自每 20 分钟唤起一次；`L5/manual` 仅人工批准后一次性执行
- 运行环境：`codex_automation`、`codex_cli`、`self_hosted_agent`；当前定时自动化固定为 `codex_automation`
- Codex CLI Runner：`scripts/codex_cli_runner.py` 每次只领取并执行一个 `codex_cli` 任务，不包含调度循环或服务常驻
- 自建 Agent：`scripts/agent_runtime.py` 提供与具体模型无关的单任务工具循环；真实模型适配由外部 Provider 工厂注入
- SecretStore：默认 `os_keyring`；Windows 原生使用 Credential Manager，macOS/Linux 通过系统 keyring 适配器访问 Keychain/Secret Service；显式 `environment` 仅用于进程级临时注入
- 并发：全局最多 8 个活动 execution，并同时受每个平台最多 5 个活动 execution 约束
- Windows 健康任务：默认每 30 分钟运行一次，连续 3 次恢复失败告警
- scope 冲突：默认按项目加锁
- Dashboard：`http://127.0.0.1:4178`；Provider 密钥管理仅限该本机同源入口
- 时区：`Asia/Shanghai`
- 文本编码：UTF-8

SQLite 只保存任务及其执行一致性数据：任务内容和历史、每项任务所选运行环境、能力等级、Provider、执行策略、execution、租约、scope 锁与任务冲突。运行环境目录及入口配置、自动化周期、并发参数、SecretStore 后端和非敏感 `secret_ref`、服务部署配置只在 `config/initialization.json`；密钥值只在所选系统密钥库或显式进程环境中；项目清单实时读取；服务健康状态只在 `runtime/health-state.json`。

## 角色

- Operator：人工主对话，只添加、修改、取消、重排和确认任务。
- Worker：L1、L2、L3、L4、L5 五条 automatic 自动化每次显式使用 `codex_automation`，按自身能力等级原子领取一个任务，在当前自动化任务中执行并回写结果。Codex CLI 和自建 Agent 分别只领取匹配的 `codex_cli` 与 `self_hosted_agent` 任务。
- Codex CLI Runner：显式接收能力等级并生成唯一 execution ID，只 claim 一次；领取后由单个 ephemeral `codex exec` 处理一个登记项目，Runner 管理 heartbeat、attempt timeout、进程树、结构化结果和 finish。
- 自建 Agent Runtime：以显式运行环境、Provider、能力等级和执行策略启动，只领取一次并处理一个任务；Provider 负责把模型 API 标准化为中立响应，Runtime 负责队列协议、上下文、受限工具、心跳和结果校验。
- Windows 健康任务：由任务计划程序直接运行 `health_run.py`，检查并按需恢复 Dashboard Server，不调用模型。
- Dashboard Server：读取任务库、初始化配置和运行时健康 JSON，提供监控接口、受控任务归档和本机 SecretStore 管理层。

## 常用命令

```powershell
py -3 .\scripts\loopctl.py validate
py -3 .\scripts\loopctl.py state
py -3 .\scripts\loopctl.py migrate
py -3 .\scripts\loopctl.py enqueue .\new-task.json
py -3 .\scripts\loopctl.py update INIT-001 .\task-patch.json
py -3 .\scripts\loopctl.py requeue TASK-ID --reason "人工确认或重新打开后排队"
py -3 .\scripts\loopctl.py cancel TASK-ID --reason "不再需要"
py -3 .\scripts\loopctl.py confirm TASK-ID --reason "人工复核通过"
py -3 .\scripts\loopctl.py archive TASK-ID --reason "终态任务不再参与当前视图"
py -3 .\scripts\loopctl.py unarchive TASK-ID --reason "重新放回当前视图"
py -3 .\scripts\loopctl.py recover EXECUTION-ID --human-confirmed-safe --action requeue
py -3 .\scripts\loopctl.py resolve-human TASK-ID --response "人工确认内容"
```

`cancel` 保留历史，不物理删除任务。`requeue` 可重新排队草稿、等待、失败或成功任务；当 Worker 已完成全部实现和验证、`WAITING_HUMAN` 只缺最后一个人工事实时，`resolve-human` 要求非空 Worker verification、无活动 execution 和明确人工答复，可直接形成 `WAITING_HUMAN -> SUCCEEDED`，避免无意义重跑。刚被误重排但尚未再次领取的任务也可受控纠正，并必须显式提供完成摘要。`confirm` 只接受 `SUCCEEDED`，形成 `SUCCEEDED -> CONFIRMED` 的人工复核链路。归档是独立的 nullable `archived_at` 属性，`archive/unarchive` 不改变状态、结果或尝试次数，且重复执行不会重复写历史。已归档任务需要先取消归档，才能修改、取消或重新排队。

Dashboard 的“已结束”分段为未归档终态任务提供归档按钮；`WAITING_HUMAN` 隔离任务的详情提供重新排队、标记失败和继续等待入口。本地 `POST /api/task-action` 只接受固定的 `archive/recover` 结构并以固定参数调用 `loopctl.py`。归档与恢复都使用乐观 `row_version`；恢复还校验 execution ID、`STALLED/TIMED_OUT` 与 `QUARANTINED`，并要求明确确认旧 Codex 会话结束。

设置抽屉通过本机同源 `/api/secrets` 管理 Provider 密钥。状态响应只包含 Provider、是否已配置、后端、公开状态和最近验证时间等非敏感元数据；不返回密钥、掩码、后四位、可逆密文或 `secret_ref`。写操作要求精确的 `Host`/`Origin`、CSRF token、`application/json`、限长请求体和一次性 UUID，并拒绝 CORS、DNS rebinding、跨站表单与重复请求。密码控件不预填，提交即清空，不使用 URL、Web Storage、IndexedDB、缓存或任务附件。

Dashboard Server 固定绑定 `127.0.0.1`，命令行改为 `0.0.0.0` 或其他地址会拒绝启动。远程 Secret 管理必须等待服务器 Secret 后端同时提供 HTTPS、认证、授权和审计，不能通过修改 Dashboard 监听地址直接开放。

Schema 3.0.0 至 3.4.0 数据库升级到 3.5.0 时运行 `loopctl.py migrate`。3.0.0 至 3.3.0 仍要求没有活动 execution，并完成 L1-L5、规范运行环境和执行配置快照迁移。3.4.0 到 3.5.0 会原样保留活动 execution 与 ACTIVE scope 锁，只增加 execution 终止/恢复字段和 scope 隔离状态；迁移不会根据时间自动猜测真实旧会话已结束，也不会创建隔离。迁移后由后续 `claim` 按独立计时条件推进状态。

Worker 协议：

```powershell
py -3 .\scripts\loopctl.py claim <execution-id> --runtime-environment codex_automation --capability-level L2 --execution-policy automatic
py -3 .\scripts\loopctl.py heartbeat <execution-id> <task-id>
$resultJson | py -3 .\scripts\loopctl.py finish <execution-id> <task-id> -
```

`finish` 默认从 stdin 读取 UTF-8 JSON，也兼容显式 JSON 文件路径。正常流程不持久化中间 report。`claim` 强制显式提供 `runtime_environment`、`capability_level` 和 `execution_policy`，只扫描三个字段同时匹配的任务；它会把冲突候选转为 `WAITING_CONFLICT` 后继续寻找其他可运行 scope。全局 8、平台 5、依赖与 scope 锁跨运行环境共同生效。它可能返回 `CLAIMED`、`NO_TASK`、`SLOT_FULL`、`CONFLICT` 或 `RECOVERY_REQUIRED`；除 `CLAIMED` 外均立即结束。

Codex 客户端的 heartbeat stalled、renewable lease expiry 与 attempt timeout 独立计算。任一存活性条件触发后，任务进入 `WAITING_HUMAN`，execution 转为非活动 `STALLED`；attempt timeout 到达后进一步转为 `TIMED_OUT`。这会释放全局和平台活动容量，但 scope lock 转为 `QUARANTINED`，继续阻止同项目写入。只有人工确认旧客户端会话已结束后，才能用 `recover --human-confirmed-safe --action requeue|failed|wait` 处置；迟到 heartbeat/finish 会被 execution fencing 拒绝。受控 Runner 平台改用 `--runner-confirmed-terminated`。

复现缺陷的时间线为：`2026-08-05T12:12:46.282+08:00` 首次观测旧 execution 心跳停滞但租约和 attempt 尚未超时；`2026-08-05T13:39:32.463+08:00` 三项条件均到达后，它仍错误保持 `RUNNING` 并占用容量与 scope。修复后的预期是首次停滞即变为 `STALLED + WAITING_HUMAN + QUARANTINED` 并释放容量，attempt 到期再记录 `TIMED_OUT`，隔离继续保留直至人工安全恢复。旧客户端实际是否已经结束仍无法从这些时间信号确认。

Codex CLI 单次入口：

```powershell
py -3 .\scripts\codex_cli_runner.py --capability-level L2 --execution-policy automatic
```

Codex CLI 单一调度入口：

```powershell
py -3 .\scripts\codex_cli_dispatcher.py
```

Dispatcher 只读检查 `codex_cli` 的 `PENDING` 且依赖已满足任务，沿用既有优先级、创建时间和 ID 顺序选出一个能力等级，并最多启动一次 Runner。原子领取、全局 8、平台 5、scope 冲突和最终状态仍由 Runner 的单次 `claim` 裁决；`NO_TASK`、`SLOT_FULL` 或 `CONFLICT` 不会触发第二个等级。`codex_cli.dispatcher` 是周期、任务名称、当前用户身份、工作目录、超时和日志边界的唯一来源。

Windows 安装脚本仅在人工批准部署后运行；本轮不会注册或修改任务计划。先用 dry-run 核对命令：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\install_codex_cli_task.ps1 -DryRun
```

授权部署后运行同一脚本（不带 `-DryRun`）。停止或回滚时禁用或删除该任务计划，并恢复本次 Dispatcher、安装脚本与配置变更；这仍不是完整 Supervisor，未提供常驻重试、跨轮恢复或多 Runner 编排。

Runner 从 `codex_cli` 配置读取能力等级对应的模型与思考参数、`use_user_config`、`workspace-write` 沙箱、attempt timeout 和输出上限。默认 `use_user_config=true`，使 CLI 自行使用本机现有认证与 provider 配置；Runner 不读取、复制或迁移这些私有配置。它仍显式传入 stdin、`--json`、`--ephemeral`、模型、推理参数、`--sandbox`、`--cd` 与临时输出 Schema，这些任务边界不由用户配置扩大。设置 `use_user_config=false` 才额外传入 `--ignore-user-config`。不使用 `--add-dir` 或任何危险绕过参数。初版只接受全部 scope 能解析到同一个登记项目的任务，多项目、外部或不安全 scope 以 `WAITING_HUMAN` finish。

截至 2026-08-03（Asia/Shanghai），本机 `codex-cli 0.146.0` 的 `codex exec --help` 已确认上述参数。已完成脱敏诊断：附加 `--ignore-user-config` 的同模型调用认证失败，保留用户配置的同模型调用成功；诊断未读取、记录或输出任何凭据、认证文件路径或私有配置内容。官方入口为 [Non-interactive mode](https://developers.openai.com/codex/noninteractive) 和 [CLI reference](https://developers.openai.com/codex/cli/reference/)；当前执行环境访问官方 manual、页面与 Docs MCP 均返回 HTTP 403，故未把无法取得的在线正文写成已核验事实。CLI 升级后须同时复核本机帮助和官方参考。

自建 Agent 单次入口：

```powershell
py -3 .\scripts\agent_runtime.py `
  --runtime-environment self_hosted_agent `
  --provider-id deepseek `
  --capability-level L2 `
  --execution-policy automatic `
  --execution-id deepseek-worker-<GUID> `
  --provider your_provider_package:create_provider
```

Provider 工厂必须接受 `config` 与 `secret_store` 关键字参数，返回实现 `complete(request, timeout_seconds)` 的对象，并把任何模型 API 响应转换为协议版本 `1.0` 的 `tool_calls` 或 `final` 对象。Runtime 创建统一 SecretStore 并注入 Provider；Provider 不得另建密钥存储或直接读取持久化凭据。`scripts/deepseek_provider.py` 提供 DeepSeek Chat Completions 适配器，采用标准库 HTTP、非流式响应、限次退避重试和本地工具参数校验；模型调用本身不执行工具，重试只发生在工具结果尚未返回运行时之前。

DeepSeek Provider 的公开失败诊断是允许列表式结构：`category`、`http_status`、`retryable`、`retry_exhausted`、`finish_reason`、`agent_attempt` 和 `model_step`。类别仅包括鉴权、限流、服务端、连接、请求超时、空或畸形响应、截断响应、无效工具调用、无效最终 JSON、本地协议和未知结束原因。401/403 和本地配置/批准问题进入 `WAITING_HUMAN`；429、5xx、连接或请求超时会受 Provider 配置约束重试，并在耗尽后标记 `retry_exhausted=true`。Runtime 只接受该受信任诊断契约；未知异常只公开固定前缀和异常类型。诊断、事件和最终结果绝不包含 API key、Authorization、请求或响应正文、完整提示词、工具参数值、业务文件内容或隐藏推理。

两个 `max_retries` 属于不同边界：`deepseek.max_retries=2` 表示每个模型步骤首次 HTTP 请求失败后最多再请求 2 次；`execution_profiles.self_hosted_agent.providers.deepseek.capabilities.<level>.max_retries=2` 表示一次完整 Agent attempt 失败后最多再启动 2 个干净 attempt。只有 Provider 请求预算已耗尽的瞬态类别，或明确的请求/attempt 超时，才能在没有本地副作用时进入后一层；400、无效工具调用、无效 final JSON、截断、`max_steps` 和未知错误不重启完整工具循环。持续 5xx 的最坏边界因此是每个 attempt 3 次请求、最多 3 个 attempt，共 9 次请求。日志分别使用 `provider_request_retry`、`provider_request_retries_exhausted` 和 `agent_attempt_retry`，不会把两层重试混为一谈。

Runtime 在每轮工具调用后保留对应的 assistant `tool_calls`，Provider 再把每个 runtime `tool_result` 映射为带匹配 `tool_call_id` 的 `tool` 消息；重复只读调用继续追加成有序消息链。最终轮要求 JSON object，并由 Runtime 按协议 1.0 的 `SUCCEEDED/FAILED/WAITING_HUMAN` 契约再次校验。`max_steps` 计算模型轮次，不计算 Provider 内部 HTTP 重试。现有安全诊断与本地假 HTTP 服务可以确认这些控制流和分类，但没有保存此前两次真实失败的原始 Provider 响应形态，因此无法确认它们分别属于哪个具体类别；本轮未读取真实凭据，也未调用真实 Provider。

DeepSeek 的非敏感端点、模型、超时、重试和 `secret_ref` 位于 `config/initialization.json`。密钥由统一 SecretStore 按引用读取；缺失、账户不符、权限不足或后端不可用会快速失败且不打印其值。先用隐藏输入初始化并查看不含原值或掩码的状态：

```powershell
py -3 .\scripts\secretctl.py status deepseek
py -3 .\scripts\secretctl.py set deepseek
py -3 .\scripts\secretctl.py verify deepseek
py -3 .\scripts\secretctl.py rotate deepseek
py -3 .\scripts\secretctl.py delete deepseek
```

`set`、`rotate` 从隐藏终端输入读取且要求重复输入；`rotate` 与 `delete` 还要求人工确认。添加 `--connect` 会再次提示可能产生一次 Provider 调用，只有输入 `CONNECT` 才联网。临时冒烟需要在配置中显式选择 `secret_management.backend=environment`，并由启动进程注入与 `deepseek.secret_ref` 同名的环境变量；该后端不持久化，`secretctl` 不会假装把子进程环境写回父进程。

也可从 Dashboard 设置抽屉执行配置、替换、本地验证、显式确认的连接验证和删除。浏览器只把一次性输入交给同源 Dashboard Server；Server 复用同一个 SecretStore，不创建任务、不写 SQLite，也不把密钥交给 Operator 或 Worker。最近验证状态以非敏感事件写入 `runtime/health-state.json`；运行账户与 `secret_management.access_account` 不一致时界面只报告存储不可用及修复建议，不会静默复制第二份密钥。

```powershell
py -3 .\scripts\agent_runtime.py `
  --runtime-environment self_hosted_agent `
  --provider-id deepseek `
  --capability-level L2 `
  --execution-policy automatic `
  --execution-id deepseek-worker-<GUID> `
  --provider deepseek_provider:create_provider
```

停止方式是让单次运行自然结束；需要人工停止时终止该进程。受控 Runner 确认进程树退出后可执行安全恢复；Codex 客户端则必须人工确认旧会话结束。备份不包含密钥；密钥库丢失或迁移账户后必须重新运行 `secretctl.py set deepseek`。不要修改任务数据库或把密钥写入配置。未获得任务内 `credential_access` 批准前，Provider 不读取密钥；本轮也没有写入真实系统凭据或调用真实模型。

## 文件

- `data/loop-agent.sqlite3`：唯一任务事实源。
- `schemas/loop-agent.sql`：Schema 3.5.0。
- `config/initialization.json`：执行、自动化与服务配置。
- `prompts/operator.md`：任务管理主对话提示词和查重、状态、独立归档流程。
- `prompts/worker.md`：Codex Worker 自动化的权威提示词。
- `prompts/cli-worker.md`：Codex CLI 子进程的单任务业务执行与结果契约提示词。
- `scripts/loopdb.py`：任务库访问与状态投影。
- `scripts/loopctl.py`：任务管理与 Worker 事务协议。
- `scripts/codex_cli_runner.py`：Codex CLI 单任务 claim、heartbeat、进程管理、结果校验和 finish 入口。
- `scripts/agent_runtime.py`：通用自建 Agent 的单次运行入口、Provider 协议与受限工具层。
- `scripts/deepseek_provider.py`：DeepSeek Chat Completions 到中立 Provider 协议的适配器。
- `scripts/secret_store.py`：统一 SecretStore 契约、系统密钥库与显式环境后端。
- `scripts/secretctl.py`：隐藏输入、状态、校验、轮换和删除命令。
- `scripts/dashboard_server.py`：本地 HTTP 状态服务、受限归档接口与同源 Secret API。
- `scripts/health_run.py`：Dashboard 健康检查和恢复。
- `scripts/install_health_task.ps1`：按初始化配置注册或更新 Windows 健康任务。
- `scripts/test_loop.py`：并发、冲突、租约和确认回归测试。
- `scripts/test_codex_cli_runner.py`：假 Codex CLI 的 JSONL、超时、脱敏、scope 和真实 finish 回归测试。
- `scripts/test_agent_runtime.py`：假 Provider、工具边界、心跳和真实 finish 回归测试。
- `scripts/test_deepseek_provider.py`：本地假 HTTP 服务的 DeepSeek 工具循环、重试和脱敏边界回归测试。
- `dashboard.html`：监控页面模板。
- `runtime/`：PID、日志、健康状态和短时锁；不是任务事实源。
- `backups/`：迁移前快照和旧产物，仅用于审计恢复。

详细规则见 `docs/architecture.md`；初始化、Worker 提示词和健康任务安装见 `docs/initialization.md`。
