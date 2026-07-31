# Local Agent Loop 初始化与自动化

## 1. 固定约束

- 根目录：`E:\code`
- Loop 目录：`E:\code\local-agent-loop`
- 数据库：`data/loop-agent.sqlite3`
- Schema：`3.2.0`
- 项目清单：`E:\code\根目录清单.md`
- Worker：五个普通档位各一条定时自动化，默认每 20 分钟，每轮最多领取一个匹配档位任务
- 并发：全局上限 6，并同时受各档位上限约束
- `exceptional`：无定时自动化，仅人工批准后一次性执行
- Windows 健康任务：默认每 30 分钟，连续失败阈值 3
- Dashboard：`127.0.0.1:4178`
- 冲突粒度：项目
- 时区：`Asia/Shanghai`
- 文本读写：UTF-8

`config/initialization.json` 是初始化和部署配置模板的唯一来源。SQLite 只保存任务和执行一致性数据；项目清单实时读取；健康状态写入 `runtime/health-state.json`。

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

已有 Schema 3.0.0 或 3.1.0 SQLite 数据库先原位升级：

```powershell
py -3 .\scripts\loopctl.py migrate
```

该命令幂等执行 `3.0.0/3.1.0 -> 3.2.0` 迁移。所有现有任务的 `execution_profile` 先回填为 `standard`；需要人工介入的既有任务再由 Operator 按已确认清单重新分类。从 3.0.0 升级时，已有 `CONFIRMED` 任务按旧归档语义回填 `archived_at`，其他状态不归档。任务状态、结果、尝试次数和既有历史均保留。升级前必须暂停旧 Worker、确认没有活动 execution 并保留数据库备份。

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

五条普通 Worker 默认每 20 分钟错峰运行。真实 Worker 自动化的入口提示只允许要求读取并执行 `prompts/worker.md` 并提供当前档位；不得在自动化配置、初始化文档或其他文件维护第二份正文。入口模板以 `config/initialization.json` 的 `automations.entry_prompt_template` 为准。

PowerShell 示例：

```powershell
$resultJson = $result | ConvertTo-Json -Depth 8 -Compress
$resultJson | py -3 E:\code\local-agent-loop\scripts\loopctl.py finish $executionId $taskId -
```

## 4. Windows 健康任务

健康检查不使用 Codex 自动化。以当前 Windows 用户注册计划任务，并立即运行一次：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\install_health_task.ps1 -StartNow
```

脚本读取 `config/initialization.json` 中的任务名称和 30 分钟周期，直接执行 `health_run.py`。重复运行安装脚本会更新同名任务，不会创建重复项。

## 5. 初始化顺序

1. 暂停旧 Worker，确认没有活动 execution，备份 `data/loop-agent.sqlite3`。
2. 使用 `loopctl.py migrate` 迁移数据库到 Schema 3.2.0；现有任务默认回填 `standard`，随后按已确认清单重分类需人工任务，并运行数据库与回归测试。
3. 运行 `install_health_task.ps1 -StartNow`，检查 Windows 健康任务、`/healthz` 和 `/api/state`。
4. 读取初始化配置，逐一检查 `routine`、`standard`、`advanced`、`deep`、`complex` 五条 Worker 自动化的 ID、模型、思考程度、20 分钟周期、错峰和入口提示。
5. 删除旧 Health Codex 自动化，确保健康检查只由 Windows 任务计划程序执行。
6. 对缺失的普通 Worker 创建，对已有 Worker 更新；不得创建定时 `exceptional` 或重复项。
7. 删除旧单 Worker 自动化，启用五条普通 Worker，并逐一复核状态。

如果数据库一致性、Dashboard 或并发测试失败，Worker 保持暂停。普通 Worker 返回 `NO_TASK` 时只结束当前轮次，不自动暂停；Operator 发布或重新排队任务时只需恢复被人工暂停的对应档位自动化。不得同时维护 JSON 和 SQLite 两套任务真源。
