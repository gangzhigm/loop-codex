# Local Agent Loop 初始化与自动化

## 1. 固定约束

- 根目录：`E:\code`
- Loop 目录：`E:\code\local-agent-loop`
- 数据库：`data/loop-agent.sqlite3`
- Schema：`3.5.0`
- 项目清单：`E:\code\根目录清单.md`
- Worker：L1 至 L5 各一条 automatic 定时自动化，默认每 20 分钟，每轮最多领取一个完全匹配的任务
- 运行环境：`codex_automation`、`codex_cli`、`self_hosted_agent`；五条定时自动化固定使用 `codex_automation`
- 并发：全局上限 8，并同时受各平台上限 5 约束；能力等级不形成并发池
- 人工执行：仅 `L5/manual`，无定时自动化，须人工批准一次性执行
- Windows 健康任务：默认每 30 分钟，连续失败阈值 3
- Dashboard：`127.0.0.1:4178`；Provider Secret API 只允许该本机同源入口
- 冲突粒度：项目
- 时区：`Asia/Shanghai`
- 文本读写：UTF-8
- Codex CLI Runner：单次进程、单次 claim、单任务、单登记项目；默认总超时 3600 秒
- 自建 Agent：单次进程，默认最多 24 个模型步骤；模型/工具超时 120 秒；不安装 Windows 服务
- SecretStore：默认 `os_keyring`，密钥不进入 SQLite、普通配置、日志、任务结果、命令行或持久化报告

`config/initialization.json` 是初始化和部署配置模板的唯一来源。运行环境列表、显示名称和执行入口参数只在该配置中维护；SQLite 仅在任务行保存所选 `runtime_environment`，并继续保存任务和执行一致性数据。项目清单实时读取；健康状态写入 `runtime/health-state.json`。

初始化必须检查五条普通档位 Codex Worker 自动化和一个 Windows 健康任务。Worker 按初始化配置中的模型、思考程度、错峰时间和入口提示创建或更新并启用；健康任务通过 `install_health_task.ps1` 注册或更新。不得创建 Health Codex 自动化或定时 `exceptional` 自动化。

| 能力 | 兼容档位 | 模型 | 思考程度 | 定时 |
|---|---|---|---|---:|
| `L1` | `routine` | `gpt-5.6-luna` | `medium` | 每 20 分钟，偏移 0 分钟 |
| `L2` | `standard` | `gpt-5.6-terra` | `medium` | 每 20 分钟，偏移 2 分钟 |
| `L3` | `advanced` | `gpt-5.6-terra` | `high` | 每 20 分钟，偏移 4 分钟 |
| `L4` | `deep` | `gpt-5.6-sol` | `high` | 每 20 分钟，偏移 6 分钟 |
| `L5` | `complex` | `gpt-5.6-sol` | `xhigh` | 每 20 分钟，偏移 8 分钟 |

所有等级共享全局 8 个活动 execution，并分别计入所属平台的 5 个活动 execution 上限。

## 2. 从旧 JSON 迁移

已有 Schema 3.0.0 至 3.4.0 SQLite 数据库先原位升级：

```powershell
py -3 .\scripts\loopctl.py migrate
```

该命令幂等迁移到 3.5.0。3.0.0 至 3.3.0 要求先暂停领取并确认没有活动 execution，再完成 L1-L5、规范运行环境和 execution 快照转换。3.4.0 到 3.5.0 可以保留活动 execution：其 `RUNNING` 状态与 ACTIVE scope lock 原样迁移，不会根据时间戳自动创建隔离或释放 scope。迁移后由下一次 `claim` 独立判断 heartbeat stalled、lease expiry 和 attempt timeout。所有路径保留状态、结果、尝试次数、历史、依赖、scope、归档和 row version，并执行外键与 SQLite 完整性检查。

仅在旧 JSON 系统仍存在时运行一次：

```powershell
py -3 .\scripts\loopctl.py init `
  --tasks .\TASKS.json `
  --inbox .\INBOX.json `
  --registry ..\根目录清单.md `
  --config .\config\initialization.json
```

命令先把旧 JSON 存入 `backups/`，然后只导入任务、依赖、scope、验收项、附件、结果和历史。旧 settings、projects、change requests 和健康状态不进入新库。成功标准：

```powershell
py -3 .\scripts\loopctl.py validate
py -3 -B .\scripts\test_loop.py -v
node .\scripts\check-dashboard.mjs .\dashboard.html
```

必须核对任务及所有保留子表的迁移前后行数和内容。升级期间暂停 Worker；旧活动任务不得伪装为完成。

## 3. 提示词来源

所有 Loop 提示词集中在 `prompts/`：

- `prompts/operator.md`：任务管理对话的查重、增删改、附件、状态和独立归档流程。
- `prompts/worker.md`：Worker 自动化的完整执行提示词。
- `prompts/cli-worker.md`：Codex CLI 子进程的单任务 scope、安全边界和最终结果契约。

五条普通 Worker 默认每 20 分钟错峰运行。真实 Worker 自动化的入口提示只允许要求读取并执行 `prompts/worker.md`，固定提供 `runtime_environment=codex_automation` 并提供当前档位；不得在自动化配置、初始化文档或其他文件维护第二份正文。入口模板以 `config/initialization.json` 的 `automations.entry_prompt_template` 为准。

所有领取入口都必须显式声明运行环境，缺失或非法参数由 CLI 拒绝，不能通过默认值跨环境领取：

```powershell
py -3 .\scripts\loopctl.py claim <automation-execution-id> --runtime-environment codex_automation --capability-level L2 --execution-policy automatic
py -3 .\scripts\loopctl.py claim <cli-execution-id> --runtime-environment codex_cli --capability-level L2 --execution-policy automatic
py -3 .\scripts\loopctl.py claim <agent-execution-id> --runtime-environment self_hosted_agent --provider-id deepseek --capability-level L2 --execution-policy automatic
```

PowerShell 示例：

```powershell
$resultJson = $result | ConvertTo-Json -Depth 8 -Compress
$resultJson | py -3 E:\code\local-agent-loop\scripts\loopctl.py finish $executionId $taskId -
```

`claim` 返回 `CLAIMED` 才能执行业务任务；`NO_TASK`、`SLOT_FULL`、`CONFLICT` 和 `RECOVERY_REQUIRED` 都立即结束本轮。`RECOVERY_REQUIRED` 表示匹配路由的旧 Codex execution 已退出活动容量，但 scope 仍为 `QUARANTINED`。人工确认旧客户端会话已结束后执行：

```powershell
py -3 .\scripts\loopctl.py recover <execution-id> --human-confirmed-safe --action requeue
py -3 .\scripts\loopctl.py recover <execution-id> --human-confirmed-safe --action failed
py -3 .\scripts\loopctl.py recover <execution-id> --human-confirmed-safe --action wait
```

三个动作分别为释放隔离并重新排队、释放隔离并标记失败、继续等待且保留隔离。命令幂等并记录 actor、Asia/Shanghai 时间与原因；未确认时不得释放。同一入口也可由 Dashboard 安全恢复面板调用。受控 Codex CLI/self-hosted Runner 在确认旧进程树终止后改用 `--runner-confirmed-terminated`。

### SecretStore 初始化

`config/initialization.json` 只保存以下非敏感路由信息：

```json
{
  "secret_management": {
    "backend": "os_keyring",
    "service": "Local Agent Loop",
    "access_account": "Admin"
  },
  "deepseek": {
    "secret_ref": "DEEPSEEK_API_KEY"
  }
}
```

`access_account` 必须与 Dashboard、初始化命令和 self-hosted Agent/Supervisor 的实际运行账户一致。换成 Windows 服务账户、macOS launchd 用户或 Linux systemd 用户时，先在该账户会话中更新此值并重新初始化同一 `secret_ref`；账户不符、后端不可用、引用缺失或权限不足都会快速失败，不能通过读取别的账户配置或降级到明文文件绕过。

`os_keyring` 的平台行为：

- Windows：本实现直接调用 WinCred，把通用凭据保存到当前账户的 Credential Manager。
- macOS：需要 Python `keyring` 能发现可用的 Keychain 后端；其他实现即使有非零优先级也会被拒绝。
- Linux 桌面：需要 Python `keyring` 能发现已解锁的 Secret Service/libsecret 后端；无桌面会话、chainer 或明文替代后端都保持不可用。

2026-08-05（Asia/Shanghai）已核对并成功访问官方入口：[Windows CredWrite](https://learn.microsoft.com/en-us/windows/win32/api/wincred/nf-wincred-credwritew)、[Apple Keychain Services](https://developer.apple.com/documentation/security/keychain-services)、[freedesktop Secret Service](https://specifications.freedesktop.org/secret-service/latest/)。这些资料确认存储接口，不保证当前机器的密钥库已解锁；实际能力以 `secretctl.py status` 为准。

DeepSeek 初始化命令不接受明文参数，`set` 和 `rotate` 只用隐藏终端输入并要求重复输入：

```powershell
py -3 .\scripts\secretctl.py status deepseek
py -3 .\scripts\secretctl.py set deepseek
py -3 .\scripts\secretctl.py verify deepseek
py -3 .\scripts\secretctl.py rotate deepseek
py -3 .\scripts\secretctl.py delete deepseek
```

`rotate` 必须输入 `ROTATE`，候选值先写入同一安全后端的临时引用并回读验证，成功后才替换主引用；失败恢复旧值。`delete` 必须输入 `DELETE`。`set/verify/rotate --connect` 会提示一次连接校验可能产生 Provider 调用，只有输入 `CONNECT` 才发送；默认校验只检查安全后端回读和格式，不联网。

状态和操作结果只输出 backend、`secret_ref`、状态、是否变化与带 Asia/Shanghai 时区的时间，不输出原值、掩码、后四位或 Authorization。项目和数据库备份不包含系统密钥库内容；系统重装、密钥库丢失或运行账户迁移后，用新账户重新运行 `set`。第一版没有具体外部 Secret Manager 实现，只有稳定 adapter 契约；选择未知 backend 会明确失败。

`environment` 只用于一次性冒烟或受控部署，必须显式把 `secret_management.backend` 改为 `environment`，并在启动 self-hosted Agent 的同一进程环境中注入与 `deepseek.secret_ref` 同名的变量。它不持久化；`secretctl` 拒绝 `set/rotate/delete`，因为子进程无法可靠修改父进程环境。禁止把注入命令写入仓库、日志或任务结果。

### DeepSeek 安全诊断

排查 self-hosted DeepSeek 执行失败时，只使用 Runtime 输出的受信任诊断字段：`category`、`http_status`、`retryable`、`retry_exhausted`、`finish_reason`、`agent_attempt` 和 `model_step`。`authentication`（含 401/403）和本地配置/批准问题需要人工处理；`rate_limited`、`server_error`、`connection` 和 `request_timeout` 仅在 Provider 配置允许的次数内重试，耗尽后显示 `retry_exhausted=true`。`empty_or_malformed_response`、`truncated_response`、`invalid_tool_call`、`invalid_final_json`、`local_protocol` 与 `unsupported_finish_reason` 不重试。不要依据异常原文、请求/响应正文、Authorization、工具参数或业务文件内容排查，因为这些值不进入事件、任务结果或 SQLite。

`final_schema` 同样属于确定性的受信任诊断类别，不会触发请求或完整 Agent attempt 重试。

`finish_reason=stop` 的 final 失败可带 `final_shape`：只含 content Unicode 字符长度、解析状态、顶层类型、`status`、`summary`、`verification`、`completed`、`error`、`question`、`options`、`result`、`message`、`output` 的存在性和标准类型，以及未知顶层字段数量/是否存在。它不保存字段值、未知字段名、内容摘要、哈希、前后缀或可逆表示。缺少 required `summary` 等终态协议错误以 `final_schema` 报告，附带 shape、`agent_attempt` 和 `model_step`；只通过本地合成 fake HTTP 响应排查，禁止为此读取 SecretStore 或调用真实 API。

配置中的两个同名字段不能混用：顶层 `deepseek.max_retries=2` 是单个模型步骤的 HTTP 请求重试数，含首次请求时最多调用 3 次；`execution_profiles.self_hosted_agent.providers.deepseek.capabilities.<level>.max_retries=2` 是完整 Agent attempt 的重试数，含首次 attempt 时最多执行 3 个 attempt。完整 attempt 只在瞬态 Provider 请求已耗尽，或明确请求/attempt 超时，且本地副作用计数为 0 时重启。持续瞬态故障的理论上限是 3 x 3 = 9 次 Provider 请求；400、无效工具参数、无效 final JSON、截断、`max_steps` 与未知错误均只执行当前 attempt。日志用 `provider_request_retry`、`provider_request_retries_exhausted` 与 `agent_attempt_retry` 区分两层。

每轮响应为 `tool_calls` 时，Runtime 执行受限工具并追加与调用 ID 匹配的 `tool_results`；下一次 Provider 请求映射为按序的 assistant/tool 消息。重复只读调用允许继续收敛，写入等本地副作用一旦发生则禁止完整 attempt 自动重启。最终响应使用 JSON object 模式，并由 Runtime 校验协议 1.0 终态；`max_steps` 计算模型轮次而不是 HTTP 请求次数。当前本地证据没有包含此前两次真实失败的原始响应形态，只能确认安全分类与重试控制流，不能确认那两次失败的具体根因。

### Dashboard Secret 管理

`dashboard.secret_api` 只保存非敏感服务边界：

```json
{
  "dashboard": {
    "host": "127.0.0.1",
    "port": 4178,
    "secret_api": {
      "enabled": true,
      "max_body_bytes": 16384,
      "replay_cache_size": 1024
    }
  }
}
```

Dashboard Server 有 Secret API 时固定要求 `host=127.0.0.1`；`--host 0.0.0.0`、局域网地址或其他监听值会直接拒绝启动。浏览器设置抽屉只通过同源 `/api/secrets` 发送一次性密码输入，状态响应不包含现有密钥、掩码、后四位、可逆密文或内部 `secret_ref`。Server 校验 Host、Origin、CSRF token、`application/json`、请求体大小、Provider 白名单和一次性请求 ID，并拒绝 CORS 与重复请求。

配置和替换调用与 CLI、Runtime 相同的 SecretStore。替换和删除必须显式确认；连接验证会提前提示可能产生一次 Provider 调用，只有确认后才由 Server 发起，前端不会直接连接 DeepSeek。提交后 password 控件、表单和临时请求变量立即清空，页面不使用 URL、localStorage、sessionStorage、IndexedDB、缓存或任务附件恢复输入。最近验证元数据只作为非敏感事件进入 `runtime/health-state.json`，不进入 SQLite。

Dashboard、后续 Supervisor 与 SecretStore 初始化必须使用同一 `access_account`。账户不一致时只显示存储不可用和切换运行账户的建议，不得要求重新输入并静默复制第二份密钥。当前实现只支持本机同源管理；远程管理必须等待服务器 Secret 后端完成 HTTPS、认证、授权和审计，不能靠修改监听地址、端口转发或反向代理直接开放。

不读取真实凭据的回归验证：

```powershell
py -3 -B .\scripts\test_dashboard_server.py -v
py -3 -B .\scripts\test_secret_store.py -v
node .\scripts\check-dashboard.mjs .\dashboard.html
```

### Codex CLI Runner 启停

`codex_cli` 配置只保存非敏感运行参数：可执行文件名、提示词路径、允许档位、档位到模型与思考参数的映射、`use_user_config`、沙箱、总超时、终止宽限和 stdout/stderr 上限。`use_user_config=true` 是默认兼容模式，CLI 可自行使用本机已有认证与 provider 配置；Runner 不读取、复制、输出或迁移这些内容。设置为 `false` 才额外传入 `--ignore-user-config`。不得在配置中加入登录信息、令牌、认证文件路径或复制用户 Codex 配置。

单次运行：

```powershell
py -3 .\scripts\codex_cli_runner.py --profile standard
```

### Codex CLI 单一调度器

`codex_cli.dispatcher` 是调度周期、Windows 任务名称、支持档位、固定工作目录、运行账户要求、超时和日志路径的唯一来源。默认每 15 分钟运行一次，固定为当前 Windows 用户和隐藏后台执行。Dispatcher 每次只读检查并最多启动一个 Runner；它不实现第二套 claim、不会重试第二个档位，也不是完整 Supervisor。

手动运行：

```powershell
py -3 .\scripts\codex_cli_dispatcher.py
```

安装脚本必须先 dry-run，且只在单独获得 Windows 计划任务部署批准后才允许不带 dry-run 执行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\install_codex_cli_task.ps1 -DryRun
```

不带 `-DryRun` 的脚本会幂等创建或更新同名任务；停止或回滚需要人工禁用或删除该任务并恢复 Dispatcher 相关变更。本阶段不提供常驻进程、自动重试、跨轮恢复或多 Runner 协调。

`--profile` 必须显式提供。Runner 自行生成 `codex-cli-<profile>-<GUID>`，只调用一次 `claim --runtime-environment codex_cli --profile <profile>`；非 `CLAIMED` 结果立即退出。领取后只允许一个登记项目，使用一个 ephemeral `codex exec`，由 CLI 自身完成认证。正常停止是单次任务自然结束；人工中断或总超时会终止 CLI 进程树，并尽力以 `FAILED` finish。当前不创建 Supervisor、Windows 服务、计划任务、PID 文件或持久化 report。

Runner 使用本机 `codex-cli 0.146.0` 的 `codex exec --help` 已确认 stdin、JSONL、ephemeral、工作目录、沙箱和输出 Schema 参数。脱敏诊断确认：附加 `--ignore-user-config` 的同模型调用认证失败，而保留用户配置的同模型调用成功；诊断没有读取、记录或输出凭据、认证文件路径或私有配置内容。升级 Codex CLI 后，先核对 [官方 non-interactive 文档](https://developers.openai.com/codex/noninteractive)、[CLI reference](https://developers.openai.com/codex/cli/reference/) 与新版本本机帮助，再更新配置或参数。2026-08-03 当前执行环境对上述官方正文返回 HTTP 403，因此在线正文尚未独立复核，不能把该项写成已验证。

只运行本地假 CLI 验证，不会调用真实 Codex 模型：

```powershell
py -3 -B .\scripts\test_codex_cli_runner.py -v
```

### 自建 Agent 启停

先提供一个 Python Provider 工厂。工厂返回的对象实现：

```python
def complete(request: dict, timeout_seconds: float) -> dict:
    ...
```

Provider 工厂必须接受 `config` 和统一的 `secret_store` 关键字参数。Provider 通过该接口完成鉴权，不得另建密钥存储或直接读取持久化凭据；也不得把密钥、Authorization 或隐藏推理放入返回对象或日志。Runtime 的标准响应仅有两种：`{"type":"tool_calls","calls":[...]}` 和 `{"type":"final","result":{...}}`。

一次运行示例：

```powershell
py -3 .\scripts\agent_runtime.py `
  --runtime-environment self_hosted_agent `
  --provider-id deepseek `
  --capability-level L2 `
  --execution-policy automatic `
  --execution-id deepseek-worker-<GUID> `
  --provider your_provider_package:create_provider
```

`--runtime-environment`、`--provider-id`、`--capability-level`、`--execution-policy automatic`、`--execution-id` 和 `--provider` 都必须显式给出。进程只 claim 一次；`NO_TASK`、`SLOT_FULL`、`CONFLICT` 立即退出，`CLAIMED` 只处理该任务。受控进程超时或中断时，Runner 先确认进程树已终止，再通过安全恢复处置 execution；不会把 Codex 客户端的人工确认要求错误扩展到可控平台。当前不创建 Windows 服务、不保存 PID、不自动重启。

DeepSeek Provider 使用 `scripts/deepseek_provider.py`，由 Runtime 注入统一 SecretStore，并仅在已领取任务明确包含 `APPROVED_ACTIONS: credential_access` 后按 `deepseek.secret_ref` 读取密钥。Provider 实例不缓存密钥；配置、日志、SQLite、任务结果、命令行和环境快照均不得保存该值。

```powershell
py -3 .\scripts\agent_runtime.py `
  --runtime-environment self_hosted_agent `
  --provider-id deepseek `
  --capability-level L2 `
  --execution-policy automatic `
  --execution-id deepseek-worker-<GUID> `
  --provider deepseek_provider:create_provider
```

DeepSeek Provider 只接受 `self_hosted_agent/deepseek` 及配置允许的能力等级；401/403、400、格式错误、空响应或截断会直接失败，429、5xx、连接和请求超时才会按 Provider 请求配置限次退避。Provider 请求耗尽后的完整 Agent attempt 是否重试，再由 execution profile、瞬态分类和本地副作用计数共同裁决。正常停止为单次运行结束；人工终止时由心跳/租约回收。回滚时停止 DeepSeek 入口并恢复本次 Provider、配置和文档变更，禁止通过直接写 SQLite 处理运行中的任务。真实 API 调用与任何凭据读取需要任务中的明确 `credential_access` 批准，未批准时只运行本地假服务测试。

自建 Agent 参数来自 `self_hosted_agent`：`max_steps`、模型与工具超时、单文件字节上限和工具输出字符上限。调整后先运行：

```powershell
py -3 -B .\scripts\test_agent_runtime.py -v
py -3 -B .\scripts\test_deepseek_provider.py -v
py -3 -B .\scripts\test_secret_store.py -v
py -3 -B .\scripts\test_loop.py -v
py -3 .\scripts\loopctl.py validate
```

## 4. Windows 健康任务

健康检查不使用 Codex 自动化。以当前 Windows 用户注册计划任务，并立即运行一次：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\install_health_task.ps1 -StartNow
```

脚本读取 `config/initialization.json` 中的任务名称和 30 分钟周期，直接执行 `health_run.py`。重复运行安装脚本会更新同名任务，不会创建重复项。

## 5. 初始化顺序

1. 暂停旧 Worker，确认没有活动 execution，备份 `data/loop-agent.sqlite3`。
2. 使用 `loopctl.py migrate` 迁移数据库到 Schema 3.5.0；3.4.0 的活动 execution 与 ACTIVE scope lock 原样保留，不在迁移时推断隔离，随后运行数据库与回归测试。
3. 运行 `install_health_task.ps1 -StartNow`，检查 Windows 健康任务、`/healthz` 和 `/api/state`。
4. 读取初始化配置，逐一检查 `routine`、`standard`、`advanced`、`deep`、`complex` 五条 Worker 自动化的 ID、模型、思考程度、20 分钟周期、错峰、`codex_automation` 路由和入口提示。
5. 删除旧 Health Codex 自动化，确保健康检查只由 Windows 任务计划程序执行。
6. 对缺失的普通 Worker 创建，对已有 Worker 更新；不得创建定时 `exceptional` 或重复项。
7. 删除旧单 Worker 自动化，启用五条普通 Worker，并逐一复核状态。

如果数据库一致性、Dashboard 或并发测试失败，Worker 保持暂停。普通 Worker 返回 `NO_TASK` 或 `RECOVERY_REQUIRED` 时只结束当前轮次，不自动暂停；后者必须先由 Operator 完成人工安全恢复。不得同时维护 JSON 和 SQLite 两套任务真源。
