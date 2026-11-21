"""
Line recorder that runs in EVERY Python process of a test run (Step 3).

Python automatically imports a module named `sitecustomize` at startup if it
is on the path. tools/build_linemap.py puts this folder on PYTHONPATH and sets
LINECOV_DIR; child processes inherit both. That matters because some sqlglot
tests (test_optimizer.py, test_executor.py) do their real work in child
processes (ProcessPoolExecutor with "spawn"). Recording only the main process
would miss those lines, and then we could skip a test that fails.

Each process writes LINECOV_DIR/<pid>.json when it exits:
  {"lines": {"sqlglot/x.py": [line, ...]}, "opened": ["tests/fixtures/...", ...]}

What is recorded:
  * lines inside functions/methods/lambdas of sqlglot/ (code objects with the
    CO_OPTIMIZED flag), via sys.monitoring (built into Python 3.12+). We return
    DISABLE after the first hit of each line, so each line costs only once.
  * non-Python files opened under the sqlglot checkout (fixtures), via an
    audit hook.
"""

import os

_DIR = os.environ.get("LINECOV_DIR")

if _DIR:
    import atexit
    import inspect
    import json
    import sys

    _ROOT = os.path.realpath(os.environ.get("LINECOV_ROOT", os.getcwd()))
    _SQLGLOT = os.path.join(_ROOT, "sqlglot") + os.sep
    _lines = {}
    _opened = set()
    _mon = sys.monitoring
    _TOOL = _mon.COVERAGE_ID

    def _on_line(code, line):
        if code.co_flags & inspect.CO_OPTIMIZED and code.co_filename.startswith(_SQLGLOT):
            _lines.setdefault(code.co_filename, set()).add(line)
        return _mon.DISABLE

    def _audit(event, args):
        if event == "open" and isinstance(args[0], (str, bytes, os.PathLike)):
            _opened.add(os.fsdecode(args[0]))

    def _rel(path):
        full = os.path.realpath(os.path.join(_ROOT, path))
        if not full.startswith(_ROOT + os.sep):
            return None
        return os.path.relpath(full, _ROOT).replace(os.sep, "/")

    def _write():
        _mon.set_events(_TOOL, 0)
        lines = {}
        for filename, nums in _lines.items():
            rel = _rel(filename)
            if rel:
                lines[rel] = sorted(nums)
        opened = set()
        for path in _opened:
            rel = _rel(path)
            if rel and not rel.endswith((".py", ".pyc")) and "__pycache__" not in rel \
                    and os.path.isfile(os.path.join(_ROOT, rel)):
                opened.add(rel)
        with open(os.path.join(_DIR, f"{os.getpid()}.json"), "w") as f:
            json.dump({"lines": lines, "opened": sorted(opened)}, f)

    _mon.use_tool_id(_TOOL, "blast-radius")
    _mon.register_callback(_TOOL, _mon.events.LINE, _on_line)
    _mon.set_events(_TOOL, _mon.events.LINE)
    sys.addaudithook(_audit)
    atexit.register(_write)
