# Local Agent Loop 初始化与自动化

## 1. 固定约束

- 根目录：`E:\code`
- Loop 目录：`E:\code\local-agent-loop`
- 数据库：`data/loop-agent.sqlite3`
- Schema：`3.0.0`
- 项目清单：`E:\code\根目录清单.md`
- Worker：默认每 10 分钟，最多 6 个活动 execution，每轮最多领取一个任务
- Windows 健康任务：默认每 10 分钟，连续失败阈值 3
- Dashboard：`127.0.0.1:4178`
- 冲突粒度：项目
- 时区：`Asia/Shanghai`
- 文本读写：UTF-8

`config/initialization.json` 是初始化和部署配置模板的唯一来源。SQLite 只保存任务和执行一致性数据；项目清单实时读取；健康状态写入 `runtime/health-state.json`。

初始化必须检查一个 Codex Worker 自动化和一个 Windows 健康任务。Worker 按提示词模板创建或更新并启用；健康任务通过 `install_health_task.ps1` 注册或更新。不得创建 Health Codex 自动化。

## 2. 从旧 JSON 迁移

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

- `prompts/operator.md`：任务管理对话的查重、增删改、附件和状态流程。
- `prompts/worker.md`：Worker 自动化的完整执行提示词。

Worker 默认每 10 分钟运行。真实 Worker 自动化的入口提示只允许要求读取并执行 `prompts/worker.md`；不得在自动化配置、初始化文档或其他文件维护第二份正文。

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

脚本读取 `config/initialization.json` 中的任务名称和 10 分钟周期，直接执行 `health_run.py`。重复运行安装脚本会更新同名任务，不会创建重复项。

## 5. 初始化顺序

1. 暂停 Worker，确认没有活动 execution。
2. 创建或迁移 `data/loop-agent.sqlite3`，运行数据库与回归测试。
3. 运行 `install_health_task.ps1 -StartNow`，检查 Windows 健康任务、`/healthz` 和 `/api/state`。
4. 读取初始化配置，检查 Worker 自动化：周期 10 分钟、提示词与 `prompts/worker.md` 一致。
5. 删除旧 Health Codex 自动化，确保健康检查只由 Windows 任务计划程序执行。
6. 对缺失的 Worker 创建，对已有 Worker 更新；不得创建重复项。
7. 启用 Worker。

如果数据库一致性、Dashboard 或并发测试失败，Worker 保持暂停。不得同时维护 JSON 和 SQLite 两套任务真源。
