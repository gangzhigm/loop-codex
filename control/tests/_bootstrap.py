"""Shared import-path bootstrap for directly executed regression tests.

The production entry modules intentionally remain in ``control/``. Tests live
one directory lower, so direct execution would otherwise omit that directory
from ``sys.path``. Keeping this adjustment here prevents every test file from
copying a subtly different repository-root calculation.
"""

from __future__ import annotations

# 中文排查：测试位于 control/tests，直接执行时需把 control 根目录加入 sys.path。
# 如果所有测试都同时出现本地模块导入失败，先检查这里的 parents 层级和插入顺序。

import sys
from pathlib import Path


CONTROL_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = CONTROL_ROOT.parent

sys.dont_write_bytecode = True
control_path = str(CONTROL_ROOT)
if control_path not in sys.path:
    sys.path.insert(0, control_path)
repository_path = str(REPOSITORY_ROOT)
if repository_path not in sys.path:
    sys.path.append(repository_path)
