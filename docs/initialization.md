# Local Agent Loop 初始化与自动化

## 1. 固定约束

- 根目录：`E:\code`
- Loop 目录：`E:\code\local-agent-loop`
- 数据库：`data/loop-agent.sqlite3`
- Schema：`3.3.0`
- 项目清单：`E:\code\根目录清单.md`
- Worker：五个普通档位各一条定时自动化，默认每 20 分钟，每轮最多领取一个匹配档位任务
- 运行环境：`codex_automation`、`codex_cli`、`deepseek`；五条定时自动化固定使用 `codex_automation`
- 并发：全局上限 6，并同时受各档位上限约束
- `exceptional`：无定时自动化，仅人工批准后一次性执行
- Windows 健康任务：默认每 30 分钟，连续失败阈值 3
- Dashboard：`127.0.0.1:4178`
- 冲突粒度：项目
- 时区：`Asia/Shanghai`
- 文本读写：UTF-8
- Codex CLI Runner：单次进程、单次 claim、单任务、单登记项目；默认总超时 3600 秒
- 自建 Agent：单次进程，默认最多 24 个模型步骤；模型/工具超时 120 秒；不安装 Windows 服务

`config/initialization.json` 是初始化和部署配置模板的唯一来源。运行环境列表、显示名称和执行入口参数只在该配置中维护；SQLite 仅在任务行保存所选 `runtime_environment`，并继续保存任务和执行一致性数据。项目清单实时读取；健康状态写入 `runtime/health-state.json`。

初始化必须检查五条普通档位 Codex Worker 自动化和一个 Windows 健康任务。Worker 按初始化配置中的模型、思考程度、错峰时间和入口提示创建或更新并启用；健康任务通过 `install_health_task.ps1` 注册或更新。不得创建 Health Codex 自动化或定时 `exceptional` 自动化。

| 档位 | 模型 | 思考程度 | 定时 | 并发上限 |
|---|---|---|---:|---:|
| `routine` | `gpt-5.6-luna` | `medium` | 每 20 分钟，偏移 0 分钟 | 2 |
| `standard` | `gpt-5.6-terra` | `medium` | 每 20 分钟，偏移 2 分钟 | 3 |
| `advanced` | `gpt-5.6-terra` | `high` | 每 20 分钟，偏移 4 分钟 | 2 |
| `deep` | `gpt-5.6-terra` | `xhigh` | 每 20 分钟，偏移 6 分钟 | 1 |
| `complex` | `gpt-5.6-sol` | `high` | 每 20 分钟，偏移 8 分钟 | 1 |
| `exceptional` | `gpt-5.6-sol` | `xhigh` | 不定时，人工批准后一次性执行 | 1 |

这些上限不能相加理解为总容量；全局最多仍为 6 个活动 execution。

## 2. 从旧 JSON 迁移

已有 Schema 3.0.0、3.1.0 或 3.2.0 SQLite 数据库先原位升级：

```powershell
py -3 .\scripts\loopctl.py migrate
```

该命令幂等执行 `3.0.0/3.1.0/3.2.0 -> 3.3.0` 迁移。所有现有任务的 `runtime_environment` 回填为 `codex_automation`；3.2.0 的 `execution_profile`、状态、结果、尝试次数、归档属性、行版本、任务子表和 execution 历史全部保留。3.0.0/3.1.0 的档位回填为 `standard`，从 3.0.0 升级时已有 `CONFIRMED` 仍按旧归档语义回填 `archived_at`。升级前必须暂停所有环境的领取入口、确认没有活动 execution 并保留数据库备份。

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
py -3 .\scripts\loopctl.py claim <automation-execution-id> --runtime-environment codex_automation --profile standard
py -3 .\scripts\loopctl.py claim <cli-execution-id> --runtime-environment codex_cli --profile standard
py -3 .\scripts\loopctl.py claim <deepseek-execution-id> --runtime-environment deepseek --profile standard
```

PowerShell 示例：

```powershell
$resultJson = $result | ConvertTo-Json -Depth 8 -Compress
$resultJson | py -3 E:\code\local-agent-loop\scripts\loopctl.py finish $executionId $taskId -
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

Provider 必须自行完成真实模型 API 的鉴权、请求和响应标准化，但不得把密钥、Authorization 或隐藏推理放入返回对象或日志。Runtime 的标准响应仅有两种：`{"type":"tool_calls","calls":[...]}` 和 `{"type":"final","result":{...}}`。

一次运行示例：

```powershell
py -3 .\scripts\agent_runtime.py `
  --runtime-environment deepseek `
  --profile standard `
  --execution-id deepseek-worker-<GUID> `
  --provider your_provider_package:create_provider
```

`--runtime-environment`、`--profile`、`--execution-id` 和 `--provider` 都必须显式给出。进程只 claim 一次；`NO_TASK`、`SLOT_FULL`、`CONFLICT` 立即退出，`CLAIMED` 只处理该任务。正常停止由单次执行自然退出；人工中断会尽力以 `FAILED` finish。强制结束导致无法 finish 时，后续任意领取会按现有心跳/租约规则回收 execution。当前不创建 Windows 服务、不保存 PID、不自动重启。

DeepSeek Provider 使用 `scripts/deepseek_provider.py`，启动时从 `deepseek.api_key_environment_variable` 指定的外部环境变量读取密钥；配置、日志、SQLite 和任务结果均不得保存该值。

```powershell
$env:DEEPSEEK_API_KEY = "由外部安全注入提供"
py -3 .\scripts\agent_runtime.py `
  --runtime-environment deepseek `
  --profile standard `
  --execution-id deepseek-worker-<GUID> `
  --provider deepseek_provider:create_provider
```

DeepSeek Provider 只接受 `deepseek` 环境及 `deepseek.supported_execution_profiles` 中的档位；401/403、格式错误、空响应或截断会直接失败，429、5xx 和连接错误才会按配置限次退避。正常停止为单次运行结束；人工终止时由心跳/租约回收。回滚时停止 DeepSeek 入口并恢复本次 Provider、配置和文档变更，禁止通过直接写 SQLite 处理运行中的任务。真实 API 调用与任何凭据读取需要任务中的明确 `credential_access` 批准，未批准时只运行本地假服务测试。

自建 Agent 参数来自 `self_hosted_agent`：`max_steps`、模型与工具超时、单文件字节上限和工具输出字符上限。调整后先运行：

```powershell
py -3 -B .\scripts\test_agent_runtime.py -v
py -3 -B .\scripts\test_deepseek_provider.py -v
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
2. 使用 `loopctl.py migrate` 迁移数据库到 Schema 3.3.0；现有任务运行环境回填 `codex_automation`，旧 Schema 3.0.0/3.1.0 任务档位回填 `standard`，随后运行数据库与回归测试。
3. 运行 `install_health_task.ps1 -StartNow`，检查 Windows 健康任务、`/healthz` 和 `/api/state`。
4. 读取初始化配置，逐一检查 `routine`、`standard`、`advanced`、`deep`、`complex` 五条 Worker 自动化的 ID、模型、思考程度、20 分钟周期、错峰、`codex_automation` 路由和入口提示。
5. 删除旧 Health Codex 自动化，确保健康检查只由 Windows 任务计划程序执行。
6. 对缺失的普通 Worker 创建，对已有 Worker 更新；不得创建定时 `exceptional` 或重复项。
7. 删除旧单 Worker 自动化，启用五条普通 Worker，并逐一复核状态。

如果数据库一致性、Dashboard 或并发测试失败，Worker 保持暂停。普通 Worker 返回 `NO_TASK` 时只结束当前轮次，不自动暂停；Operator 发布或重新排队任务时只需恢复被人工暂停的对应档位自动化。不得同时维护 JSON 和 SQLite 两套任务真源。
