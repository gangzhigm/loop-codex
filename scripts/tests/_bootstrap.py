"""Shared import-path bootstrap for directly executed regression tests.

The production entry modules intentionally remain in ``scripts/``. Tests live
one directory lower, so direct execution would otherwise omit that directory
from ``sys.path``. Keeping this adjustment here prevents every test file from
copying a subtly different repository-root calculation.
"""

from __future__ import annotations

# 中文排查：测试位于 scripts/tests，直接执行时需把 scripts 根目录加入 sys.path。
# 如果所有测试都同时出现本地模块导入失败，先检查这里的 parents 层级和插入顺序。

import sys
from pathlib import Path


SCRIPTS_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = SCRIPTS_ROOT.parent

sys.dont_write_bytecode = True
scripts_path = str(SCRIPTS_ROOT)
if scripts_path not in sys.path:
    sys.path.insert(0, scripts_path)
repository_path = str(REPOSITORY_ROOT)
if repository_path not in sys.path:
    sys.path.append(repository_path)
