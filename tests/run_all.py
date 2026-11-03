# -*- coding: utf-8 -*-
"""不依赖 pytest 的测试运行器：`python tests/run_all.py`

（CI 里仍用 `pytest tests -q`；本文件用于本机没有 pytest 时跑同一批用例。）
"""
import sys
import time
import traceback
import importlib.util
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main():
    files = sorted(HERE.glob("test_*.py"))
    passed = failed = 0
    t0 = time.time()
    for path in files:
        spec = importlib.util.spec_from_file_location(path.stem, str(path))
        module = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(module)
        except Exception:
            failed += 1
            print("FAIL  导入失败: %s" % path.name)
            traceback.print_exc()
            continue
        for name in sorted(dir(module)):
            if not name.startswith("test_"):
                continue
            fn = getattr(module, name)
            if not callable(fn):
                continue
            try:
                fn()
            except Exception:
                failed += 1
                print("FAIL  %s::%s" % (path.stem, name))
                print("      " + traceback.format_exc().replace("\n", "\n      ").rstrip())
            else:
                passed += 1
                print("PASS  %s::%s" % (path.stem, name))
    print("-" * 60)
    print("通过 %d / 失败 %d（%.1fs）" % (passed, failed, time.time() - t0))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
