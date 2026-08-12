"""所有 Local Agent Loop 组件共同使用的标准仓库路径。

集中计算路径可以防止不同入口推导出不同的根目录。配置可以选择任务工作区，但运行时
不能移动 Loop 系统文件本身。
"""

# 中文排查：仓库根目录、数据库、Schema 和初始化配置的标准路径只允许在这里推导。
# 路径异常先检查本文件的 parents 层级；不要在各入口复制另一套相对路径计算。

from pathlib import Path


BASE_DIR = Path(__file__).resolve().parents[2]
DEFAULT_DB = BASE_DIR / "data" / "loop-agent.sqlite3"
SCHEMA_PATH = BASE_DIR / "schemas" / "loop-agent.sql"
CONFIG_PATH = BASE_DIR / "config" / "initialization.json"
