# Local Agent Loop

`E:\code\local-agent-loop` 是 `E:\code` 下跨项目任务的并发执行中心。Operator 管理任务，Codex Worker 执行任务，Windows 健康任务维护 Dashboard Server。

## 固定配置

- 任务数据库：`E:\code\local-agent-loop\data\loop-agent.sqlite3`（Schema 3.3.0）
- 初始化配置：`config/initialization.json`
- 项目清单：`E:\code\根目录清单.md`
- Worker：五个普通档位各有一条定时自动化，默认每 20 分钟唤起一次；`exceptional` 仅人工批准后一次性执行
- 运行环境：`codex_automation`、`codex_cli`、`deepseek`；当前定时自动化固定为 `codex_automation`
- Codex CLI Runner：`scripts/codex_cli_runner.py` 每次只领取并执行一个 `codex_cli` 任务，不包含调度循环或服务常驻
- 自建 Agent：`scripts/agent_runtime.py` 提供与具体模型无关的单任务工具循环；真实模型适配由外部 Provider 工厂注入
- 并发：全局最多 6 个活动 execution，并同时受各档位上限约束
- Windows 健康任务：默认每 30 分钟运行一次，连续 3 次恢复失败告警
- scope 冲突：默认按项目加锁
- Dashboard：`http://127.0.0.1:4178`
- 时区：`Asia/Shanghai`
- 文本编码：UTF-8

SQLite 只保存任务及其执行一致性数据：任务内容和历史、每项任务所选运行环境与执行档位、execution、租约、scope 锁与任务冲突。运行环境目录及入口配置、自动化周期、并发参数和服务部署配置只在 `config/initialization.json`；项目清单实时读取；服务健康状态只在 `runtime/health-state.json`。

## 角色

- Operator：人工主对话，只添加、修改、取消、重排和确认任务。
- Worker：`routine`、`standard`、`advanced`、`deep`、`complex` 五条自动化每次显式使用 `codex_automation`，按自身档位原子领取一个任务，在当前自动化任务中执行并回写结果。Codex CLI 和 DeepSeek 入口分别只领取 `codex_cli` 与 `deepseek`。
- Codex CLI Runner：显式接收档位并生成唯一 execution ID，只 claim 一次；领取后由单个 ephemeral `codex exec` 处理一个登记项目，Runner 管理 heartbeat、超时、进程树、结构化结果和 finish。
- 自建 Agent Runtime：以显式运行环境和档位启动，只领取一次并处理一个任务；Provider 负责把模型 API 标准化为中立响应，Runtime 负责队列协议、上下文、受限工具、心跳和结果校验。
- Windows 健康任务：由任务计划程序直接运行 `health_run.py`，检查并按需恢复 Dashboard Server，不调用模型。
- Dashboard Server：读取任务库、初始化配置和运行时健康 JSON，提供监控接口和页面。

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
```

`cancel` 保留历史，不物理删除任务。`requeue` 可重新排队草稿、等待、失败或成功任务；`confirm` 只接受 `SUCCEEDED`，形成 `SUCCEEDED -> CONFIRMED` 的人工复核链路。归档是独立的 nullable `archived_at` 属性，`archive/unarchive` 不改变状态、结果或尝试次数，且重复执行不会重复写历史。已归档任务需要先取消归档，才能修改、取消或重新排队。

Dashboard 的“已结束”分段为未归档终态任务提供归档按钮。本地 `POST /api/task-action` 只接受 `task_id`、固定的 `archive` 动作和当前 `row_version`；服务端以固定参数调用 `loopctl.py`。`SUCCEEDED` 会先人工确认再归档，其他可归档终态直接归档；旧版本、非法状态和重复请求会返回冲突并要求页面刷新。

Schema 3.0.0、3.1.0 或 3.2.0 数据库升级到 3.3.0 时运行 `loopctl.py migrate`。迁移会保留任务、子表和 execution 历史，并把所有既有任务的 `runtime_environment` 回填为 `codex_automation`；3.2.0 的 `execution_profile`、状态、结果、归档属性和行版本原样保留，3.0.0/3.1.0 的档位回填为 `standard`。从 3.0.0 升级时仍按旧版语义仅为已有 `CONFIRMED` 任务回填 `archived_at`。

Worker 协议：

```powershell
py -3 .\scripts\loopctl.py claim <execution-id> --runtime-environment codex_automation --profile standard
py -3 .\scripts\loopctl.py heartbeat <execution-id> <task-id>
$resultJson | py -3 .\scripts\loopctl.py finish <execution-id> <task-id> -
```

`finish` 默认从 stdin 读取 UTF-8 JSON，也兼容显式 JSON 文件路径。正常流程不持久化中间 report。`claim` 强制显式提供 `runtime_environment` 和 `execution_profile`，只扫描两个字段同时匹配的任务；它会把冲突候选转为 `WAITING_CONFLICT` 后继续寻找同环境同档位其他 scope 的任务，并回收心跳超时或租约过期的 execution。全局和档位并发上限、依赖与 scope 锁跨运行环境共同生效。它可能返回 `CLAIMED`、`NO_TASK`、`SLOT_FULL` 或 `CONFLICT`；除 `CLAIMED` 外均立即结束。

Codex CLI 单次入口：

```powershell
py -3 .\scripts\codex_cli_runner.py --profile standard
```

Codex CLI 单一调度入口：

```powershell
py -3 .\scripts\codex_cli_dispatcher.py
```

Dispatcher 只读检查 `codex_cli` 的 `PENDING` 且依赖已满足任务，沿用既有优先级、创建时间和 ID 顺序选出一个档位，并最多启动一次 Runner。原子领取、容量、scope 冲突和最终状态仍由 Runner 的单次 `claim` 裁决；`NO_TASK`、`SLOT_FULL` 或 `CONFLICT` 不会触发第二个档位。`codex_cli.dispatcher` 是周期、任务名称、当前用户身份、工作目录、超时和日志边界的唯一来源。

Windows 安装脚本仅在人工批准部署后运行；本轮不会注册或修改任务计划。先用 dry-run 核对命令：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\install_codex_cli_task.ps1 -DryRun
```

授权部署后运行同一脚本（不带 `-DryRun`）。停止或回滚时禁用或删除该任务计划，并恢复本次 Dispatcher、安装脚本与配置变更；这仍不是完整 Supervisor，未提供常驻重试、跨轮恢复或多 Runner 编排。

Runner 从 `codex_cli` 配置读取可执行文件、档位模型与思考参数、`use_user_config`、`workspace-write` 沙箱、总超时和输出上限。默认 `use_user_config=true`，使 CLI 自行使用本机现有认证与 provider 配置；Runner 不读取、复制或迁移这些私有配置。它仍显式传入 stdin、`--json`、`--ephemeral`、模型、推理档位、`--sandbox`、`--cd` 与临时输出 Schema，这些任务边界不由用户配置扩大。设置 `use_user_config=false` 才额外传入 `--ignore-user-config`。不使用 `--add-dir` 或任何危险绕过参数。初版只接受全部 scope 能解析到同一个登记项目的任务，多项目、外部或不安全 scope 以 `WAITING_HUMAN` finish。

截至 2026-08-03（Asia/Shanghai），本机 `codex-cli 0.146.0` 的 `codex exec --help` 已确认上述参数。已完成脱敏诊断：附加 `--ignore-user-config` 的同模型调用认证失败，保留用户配置的同模型调用成功；诊断未读取、记录或输出任何凭据、认证文件路径或私有配置内容。官方入口为 [Non-interactive mode](https://developers.openai.com/codex/noninteractive) 和 [CLI reference](https://developers.openai.com/codex/cli/reference/)；当前执行环境访问官方 manual、页面与 Docs MCP 均返回 HTTP 403，故未把无法取得的在线正文写成已核验事实。CLI 升级后须同时复核本机帮助和官方参考。

自建 Agent 单次入口：

```powershell
py -3 .\scripts\agent_runtime.py `
  --runtime-environment deepseek `
  --profile standard `
  --execution-id deepseek-worker-<GUID> `
  --provider your_provider_package:create_provider
```

Provider 工厂返回实现 `complete(request, timeout_seconds)` 的对象，并把任何模型 API 响应转换为协议版本 `1.0` 的 `tool_calls` 或 `final` 对象。Runtime 不读取模型密钥，不接受 DeepSeek 专有字段，也不会轮询或领取第二项。`scripts/deepseek_provider.py` 提供 DeepSeek Chat Completions 适配器，采用标准库 HTTP、非流式响应、限次退避重试和本地工具参数校验；模型调用本身不执行工具，重试只发生在工具结果尚未返回运行时之前。

DeepSeek 的非敏感端点、模型、超时、重试和支持档位位于 `config/initialization.json` 的 `deepseek` 节。密钥只从该节指定的外部环境变量注入；缺失会快速失败且不打印其值。启动时 Provider 仅接受 `deepseek` 环境及配置允许的档位：

```powershell
$env:DEEPSEEK_API_KEY = "由安全注入系统提供"
py -3 .\scripts\agent_runtime.py `
  --runtime-environment deepseek `
  --profile standard `
  --execution-id deepseek-worker-<GUID> `
  --provider deepseek_provider:create_provider
```

停止方式是让单次运行自然结束；需要人工停止时终止该进程，未能提交 `finish` 的 execution 将由现有心跳/租约机制回收。回滚只需停止 DeepSeek 入口并恢复本次变更；不要修改任务数据库或把密钥写入配置。未获得 `credential_access` 和可能产生费用调用的明确批准前，真实 DeepSeek 链路仍未经验证。

## 文件

- `data/loop-agent.sqlite3`：唯一任务事实源。
- `schemas/loop-agent.sql`：Schema 3.3.0。
- `config/initialization.json`：执行、自动化与服务配置。
- `prompts/operator.md`：任务管理主对话提示词和查重、状态、独立归档流程。
- `prompts/worker.md`：Codex Worker 自动化的权威提示词。
- `prompts/cli-worker.md`：Codex CLI 子进程的单任务业务执行与结果契约提示词。
- `scripts/loopdb.py`：任务库访问与状态投影。
- `scripts/loopctl.py`：任务管理与 Worker 事务协议。
- `scripts/codex_cli_runner.py`：Codex CLI 单任务 claim、heartbeat、进程管理、结果校验和 finish 入口。
- `scripts/agent_runtime.py`：通用自建 Agent 的单次运行入口、Provider 协议与受限工具层。
- `scripts/deepseek_provider.py`：DeepSeek Chat Completions 到中立 Provider 协议的适配器。
- `scripts/dashboard_server.py`：本地 HTTP 状态服务与受限归档接口。
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
