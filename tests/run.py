"""Run every test with no test framework installed: `python tests/run.py`.

The package is stdlib-only, so its self-check must be too. CI uses pytest over
the same files; this is what Setup.cmd and setup.sh run on a clean machine.
"""
import importlib.util
import sys
import traceback
from pathlib import Path

here = Path(__file__).resolve().parent
passed = failed = 0
for path in sorted(here.glob("test_*.py")):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for name in sorted(n for n in dir(mod) if n.startswith("test_")):
        try:
            getattr(mod, name)()
            passed += 1
        except Exception:
            failed += 1
            print(f"FAIL {path.name}::{name}")
            traceback.print_exc()
print(f"{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
