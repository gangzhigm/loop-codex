from __future__ import annotations

# 兼容入口：直接执行本文件时，运行拆分后的全部控制面回归模块。

import sys
import unittest
from pathlib import Path


sys.dont_write_bytecode = True


if __name__ == "__main__":
    directory = Path(__file__).resolve().parent
    suite = unittest.defaultTestLoader.discover(
        str(directory), pattern="test_loop_*.py"
    )
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    raise SystemExit(0 if result.wasSuccessful() else 1)
