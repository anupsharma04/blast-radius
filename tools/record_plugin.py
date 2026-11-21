"""
pytest plugin that records what ONE test file depends on.

Used only by tools/build_map.py (never at submission runtime). It is loaded
with `pytest -p record_plugin`, and writes a JSON file (path taken from the
RECORD_OUT environment variable) containing:

  - "deps":     every file inside the sqlglot checkout that this pytest run
                used: Python modules that got imported (including ones
                imported lazily while tests ran), and data files that were
                opened (e.g. tests/fixtures/optimizer/simplify.sql).
  - "outcomes": test id -> "passed" / "failed" / "skipped", so we can check
                that our environment behaves like the scorer's.

Why record at runtime instead of reading `import` lines? sqlglot loads
dialects lazily (e.g. read="snowflake" imports the snowflake modules only
when a test runs). Reading import lines would miss that and could make us
skip a test that fails, which costs 300 s.
"""

import json
import os
import sys

# The sqlglot checkout we are running in (build_map.py sets cwd to it).
ROOT = os.path.realpath(os.getcwd())

# Files opened during the run, collected by the audit hook below.
_opened = set()


def _audit_hook(event, args):
    """Python calls this for every audited action. We only care about
    "open" events (raised by open(), io.open_code(), os.open(), ...)."""
    if event != "open":
        return
    path = args[0]
    # Some opens are by file descriptor (an int); those are not paths.
    if isinstance(path, (str, bytes, os.PathLike)):
        _opened.add(os.fsdecode(path))


# Register as early as possible (when pytest loads this plugin), so we also
# see files opened while pytest imports the test module.
sys.addaudithook(_audit_hook)

_outcomes = {}


def _relative_to_root(path):
    """Turn an absolute path into a repo-relative one like 'sqlglot/x.py'.
    Returns None for paths outside the sqlglot checkout."""
    full = os.path.realpath(os.path.join(ROOT, path))
    if not full.startswith(ROOT + os.sep):
        return None
    rel = os.path.relpath(full, ROOT).replace(os.sep, "/")
    # Compiled bytecode caches are not source files a patch could change.
    if "__pycache__" in rel or rel.endswith(".pyc"):
        return None
    return rel


def pytest_runtest_logreport(report):
    """Remember each test's result. A test is 'failed' if any phase
    (setup / call / teardown) failed."""
    if report.failed:
        _outcomes[report.nodeid] = "failed"
    elif report.when == "call" and report.nodeid not in _outcomes:
        _outcomes[report.nodeid] = "skipped" if report.skipped else "passed"
    elif report.skipped and report.nodeid not in _outcomes:
        _outcomes[report.nodeid] = "skipped"


def pytest_sessionfinish(session):
    deps = set()

    # 1) Every module Python has loaded so far, wherever it was imported.
    for module in list(sys.modules.values()):
        file = getattr(module, "__file__", None)
        if file:
            rel = _relative_to_root(file)
            if rel:
                deps.add(rel)

    # 2) Every file opened (fixtures, data files, source files).
    for path in _opened:
        rel = _relative_to_root(path)
        if rel and os.path.isfile(os.path.join(ROOT, rel)):
            deps.add(rel)

    with open(os.environ["RECORD_OUT"], "w") as f:
        json.dump({"deps": sorted(deps), "outcomes": _outcomes}, f)
