"""项目长期运行入口共享的 Windows 进程、运行文件和健康管理方法。"""

from __future__ import annotations

import sys
from pathlib import Path


# 公共模块可被独立导入，因此不能依赖 main.py 预先设置控制面搜索路径。
_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_CONTROL_ROOT = _REPOSITORY_ROOT / "control"
if str(_CONTROL_ROOT) not in sys.path:
    sys.path.insert(0, str(_CONTROL_ROOT))
