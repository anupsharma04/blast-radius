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
    # (paths below are compared after os.path.realpath)
    _SQLGLOT = os.path.join(_ROOT, "sqlglot") + os.sep
    _lines = {}
    _opened = set()
    _mon = sys.monitoring
    _TOOL = _mon.COVERAGE_ID

    def _on_line(code, line):
        if code.co_flags & inspect.CO_OPTIMIZED and code.co_filename.startswith(_SQLGLOT):
            _lines.setdefault(code.co_filename, set()).add(line)
        return _mon.DISABLE

    # "Source observers": sqlglot .py files whose TEXT or CODE OBJECTS a test
    # looked at (not just ran). Only these tests can notice a comment edit or
    # a shift in line numbers.
    _observed = set()
    _IGNORED_CODE_READERS = {("_signature_from_function", "inspect.py"),
                             ("overload", "typing.py")}

    def _opened_by_import():
        # True if the open comes from Python's import machinery.
        f = sys._getframe(2)
        while f is not None:
            if f.f_code.co_filename.startswith("<frozen importlib"):
                return True
            f = f.f_back
        return False

    def _audit(event, args):
        if event == "open" and isinstance(args[0], (str, bytes, os.PathLike)):
            path = os.fsdecode(args[0])
            _opened.add(path)
            if path.endswith(".py") and os.path.realpath(path).startswith(_SQLGLOT) \
                    and not _opened_by_import():
                _observed.add(os.path.realpath(path))       # read as text
        elif event == "object.__getattr__" and args[1] == "__code__":
            # Someone inspected a function's code object (line numbers live
            # there). Record which module the function belongs to.
            # Two standard-library readers are ignored because what they read
            # can't change with a comment/line shift in any way tests see:
            #  - inspect's signature code (sqlglot's optimizer calls
            #    inspect.getfullargspec): reads only parameter names/kinds;
            #  - typing.overload: keys a registry by line number, but that
            #    registry is only read by typing.get_overloads(), which
            #    neither sqlglot nor its tests use.
            caller = sys._getframe(1).f_code
            if (caller.co_name, os.path.basename(caller.co_filename)) in _IGNORED_CODE_READERS:
                return
            mod = sys.modules.get(getattr(args[0], "__module__", None) or "")
            file = getattr(mod, "__file__", None)
            if file and os.path.realpath(file).startswith(_SQLGLOT):
                _observed.add(os.path.realpath(file))

    def _rel(path):
        full = os.path.realpath(os.path.join(_ROOT, path))
        if not full.startswith(_ROOT + os.sep):
            return None
        return os.path.relpath(full, _ROOT).replace(os.sep, "/")

    def _write():
        _mon.set_events(_TOOL, 0)
        _mon.set_events(_ATTR_TOOL, 0)
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
        observed = sorted(r for r in map(_rel, _observed) if r)
        # "wrapped": constants we could watch in this process. "clobbered":
        # ones that something replaced before we saw a read (we can't vouch
        # for their readers, so the map marks them untrackable).
        wrapped = sorted({r.key for r in _wrapped})
        clobbered = sorted({r.key for r in _wrapped
                            if not r.restored and r.cls.__dict__.get(r.name) is not r})
        with open(os.path.join(_DIR, f"{os.getpid()}.json"), "w") as f:
            json.dump({"lines": lines, "opened": sorted(opened), "observed": observed,
                       "attrs": sorted(_attrs), "wrapped": wrapped,
                       "clobbered": clobbered}, f)

    def _on_start(code, offset):
        # LINE events only fire when execution MOVES to a different line, so
        # a one-line lambda (`lambda self, e: ...`) never produces one: it
        # starts and finishes on the same line. PY_START fires whenever a
        # code object starts running; record the line it starts on.
        if code.co_flags & inspect.CO_OPTIMIZED and code.co_filename.startswith(_SQLGLOT):
            for _, _, line in code.co_lines():
                if line is not None:
                    _lines.setdefault(code.co_filename, set()).add(line)
                    break
        return _mon.DISABLE

    # ------------------------------------------------------------------
    # Class-constant reads (see valuemap.py at the repo root).
    # When a sqlglot module finishes importing, every plain-data class
    # attribute (TSQL.WEEK_OFFSET, Parser.FUNCTIONS, ...) is replaced by a
    # _Rec descriptor. Reading it returns the SAME value; the first read
    # that is not part of importing sqlglot records "(class, attribute)"
    # and puts the original value back (so later reads cost nothing).
    # ------------------------------------------------------------------
    import importlib.util

    _spec = importlib.util.spec_from_file_location(
        "_blast_valuemap",
        os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__)))), "valuemap.py"))
    _vm = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_vm)
    _attrs = set()
    _wrapped = []            # every _Rec we installed
    _getframe = sys._getframe

    def _importing_sqlglot():
        # True while the module-level code of some sqlglot file is running
        # (import time). Those reads only feed values that get stored
        # somewhere, and the runtime check compares everything stored.
        f = _getframe(2)
        while f is not None:
            c = f.f_code
            if c.co_name == "<module>" and c.co_filename.startswith(_SQLGLOT):
                return True
            f = f.f_back
        return False

    class _Rec:
        __slots__ = ("cls", "name", "value", "key", "restored")

        def __init__(self, cls, name, value):
            self.cls, self.name, self.value = cls, name, value
            self.key = _vm.class_key(cls) + "|" + name
            self.restored = False

        def __get__(self, obj, owner=None):
            if not self.restored and not _importing_sqlglot():
                _attrs.add(self.key)
                self.restored = True
                type.__setattr__(self.cls, self.name, self.value)   # restore
            return self.value

    def _wrap_module(module):
        for cls in _vm.module_classes(module):
            for name, value in list(cls.__dict__.items()):
                if _vm.trackable(cls, name, value):
                    rec = _Rec(cls, name, value)
                    try:
                        type.__setattr__(cls, name, rec)
                    except (TypeError, AttributeError):
                        continue
                    _wrapped.append(rec)

    _ATTR_TOOL = 3

    def _on_return(code, offset, retval):
        if code.co_name != "<module>" or not code.co_filename.startswith(_SQLGLOT):
            return _mon.DISABLE
        for mod in list(sys.modules.values()):
            if getattr(mod, "__file__", None) == code.co_filename:
                _wrap_module(mod)
                break
        return None

    _mon.use_tool_id(_ATTR_TOOL, "blast-radius-attrs")
    _mon.register_callback(_ATTR_TOOL, _mon.events.PY_RETURN, _on_return)
    _mon.set_events(_ATTR_TOOL, _mon.events.PY_RETURN)

    _mon.use_tool_id(_TOOL, "blast-radius")
    _mon.register_callback(_TOOL, _mon.events.LINE, _on_line)
    _mon.register_callback(_TOOL, _mon.events.PY_START, _on_start)
    _mon.set_events(_TOOL, _mon.events.LINE | _mon.events.PY_START)
    sys.addaudithook(_audit)
    atexit.register(_write)
