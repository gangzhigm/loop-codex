"""Canonical repository paths used by every Local Agent Loop component.

Keeping path calculation here prevents individual entry points from deriving
different roots. Configuration may select a task workspace, but it may not
move the Loop system files themselves at runtime.
"""

# 中文排查：仓库根目录、数据库、Schema 和初始化配置的标准路径只允许在这里推导。
# 路径异常先检查本文件的 parents 层级；不要在各入口复制另一套相对路径计算。

from pathlib import Path


BASE_DIR = Path(__file__).resolve().parents[2]
DEFAULT_DB = BASE_DIR / "data" / "loop-agent.sqlite3"
SCHEMA_PATH = BASE_DIR / "schemas" / "loop-agent.sql"
CONFIG_PATH = BASE_DIR / "config" / "initialization.json"
