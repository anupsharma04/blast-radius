"""
Runs in a CHILD process (so the selector itself never imports sqlglot).

  python valuecheck.py baseline <src> <out.json.gz>
      Docker build time: fingerprint the ORIGINAL sqlglot (see valuemap.py).
      Runs the fingerprint several times to find values whose dict order
      changes from run to run; those are compared ignoring order.

  python valuecheck.py check <src> <baseline.json.gz>
      (stdin: {"files": {path: patched text}, "defs": [[module, qualname], ...]})
      Runtime: import sqlglot with the patched files swapped in, fingerprint
      everything again, and print {"ok": bool, "changed": [...], "why": str}.
      ok=False means "something we can't attribute to class constants
      changed" and the selector falls back to the file-level map.

Always run with PYTHONHASHSEED=0 so set/dict orders match between the two.
"""

import gzip
import importlib.abc
import importlib.util
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import valuemap  # noqa: E402


class PatchedSource(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    """Import hook: for the changed files, run the PATCHED text instead of
    the file on disk (same file name and module name, so everything else is
    identical). Other modules import normally (from .pyc when available)."""

    def __init__(self, src, files):
        self.by_path = {os.path.realpath(os.path.join(src, p)): text for p, text in files.items()}
        self.executed = set()            # patched files that really ran

    def find_spec(self, name, path, target=None):
        # Ask the normal finders where the module is; take over only if it
        # is one of the changed files.
        for finder in sys.meta_path:
            if finder is self or not hasattr(finder, "find_spec"):
                continue
            spec = finder.find_spec(name, path, target)
            if spec is None:
                continue
            if spec.origin and os.path.realpath(spec.origin) in self.by_path:
                spec.loader = self
            return spec
        return None

    def create_module(self, spec):
        return None                      # default module object

    def exec_module(self, module):
        origin = module.__spec__.origin
        code = compile(self.by_path[os.path.realpath(origin)], origin, "exec", dont_inherit=True)
        exec(code, module.__dict__)
        self.executed.add(os.path.realpath(origin))


def _run_fp(src, loose_keys=()):
    """Fingerprint in THIS process (call once per process)."""
    sys.path.insert(0, src)
    res = valuemap.fingerprint_all(src)
    if loose_keys:
        # Recompute the order-unstable keys ignoring dict order.
        loose = valuemap.Fingerprinter(loose=True)
        for key in loose_keys:
            obj = _lookup(key)
            if obj is not _MISSING:
                res["pairs"][key] = loose.fp(obj)
    return res


_MISSING = object()


def _lookup(key):
    """The live object behind a fingerprint key ("G|module|name" or
    "A|module:Class.Nested|name")."""
    kind, *rest = key.split("|")
    if kind == "G":
        mod = sys.modules.get(rest[0])
        return vars(mod).get(rest[1], _MISSING) if mod else _MISSING
    if kind == "A":
        modname, qual = rest[0].split(":", 1)
        obj = sys.modules.get(modname)
        for part in qual.split("."):
            if obj is None or obj is _MISSING:
                return _MISSING
            obj = vars(obj).get(part, _MISSING)
        return obj.__dict__.get(rest[1], _MISSING) if isinstance(obj, type) else _MISSING
    return _MISSING


def _child(args, stdin=None):
    env = dict(os.environ, PYTHONHASHSEED="0", PYTHONDONTWRITEBYTECODE="1")
    p = subprocess.run([sys.executable, "-s", os.path.abspath(__file__)] + args,
                       input=stdin, capture_output=True, text=True, env=env, cwd="/tmp"
                       if os.path.isdir("/tmp") else None)
    if p.returncode != 0:
        raise RuntimeError(p.stderr[-2000:])
    return json.loads(p.stdout)


def build_baseline(src, out):
    # 1) Which keys change between identical runs? (dict order of tables
    #    built from sets of classes)
    runs = [_child(["dump", src], "[]") for _ in range(3)]
    unstable = sorted(k for k in runs[0]["pairs"]
                      if len({r["pairs"].get(k) for r in runs}) > 1)
    # 2) Fingerprint with those keys compared ignoring order; must now be stable.
    print(f"baseline: {len(unstable)} order-unstable values", file=sys.stderr)
    final = [_child(["dump", src], json.dumps(unstable)) for _ in range(2)]
    still = sorted(k for k in final[0]["pairs"] if final[0]["pairs"][k] != final[1]["pairs"].get(k))
    base = final[0]
    with gzip.open(out, "wt") as f:
        json.dump({"pairs": base["pairs"], "loose": unstable, "never_equal": still,
                   "failed": base["failed"], "cached": base["cached"],
                   "trackable": sorted(base["trackable"])}, f, separators=(",", ":"))
    print(f"baseline: {len(base['pairs'])} values, {len(unstable)} order-unstable, "
          f"{len(still)} never stable, failed imports {base['failed']}", file=sys.stderr)


def check(src, baseline_path, request):
    """request = {"files": {path: patched text}, "defs": [[module, qualname], ...]}
    ("defs": the defs the patch changed; the selector runs their callers)."""
    files = request["files"]
    changed_defs = {tuple(d) for d in request.get("defs", ())}
    with gzip.open(baseline_path, "rt") as f:
        base = json.load(f)
    hook = PatchedSource(src, files)
    sys.meta_path.insert(0, hook)
    res = _run_fp(src, base["loose"])
    if hook.executed != set(hook.by_path):
        # A patched file wasn't imported (e.g. it needs a package this image
        # lacks): we saw nothing, which proves nothing.
        return {"ok": False, "why": "patched module not imported"}
    if res["failed"] != base["failed"]:
        return {"ok": False, "why": f"import errors differ: {res['failed']}"}
    if res["cached"] != base["cached"]:
        return {"ok": False, "why": "functools caches filled at import"}
    old, new = base["pairs"], res["pairs"]
    if old.keys() != new.keys():
        diff = sorted(old.keys() ^ new.keys())[:5]
        return {"ok": False, "why": f"attributes/classes added or removed: {diff}"}
    # never_equal: values whose fingerprint differs between two runs of the
    # ORIGINAL code. We can't tell if the patch changed them -> fall back.
    never = set(base["never_equal"])
    changed = sorted(k for k in old if old[k] != new[k] or k in never)
    track = set(base["trackable"])
    consts, funcs, globs, bad = [], [], [], []
    for k in changed:
        if k in never:
            bad.append(k)
        elif k in track:
            consts.append(k)                    # class constant -> its readers
        elif _is_changed_def(k, changed_defs):
            continue                            # the selector runs its callers
        elif (f := _plain_function(k)) is not None:
            funcs.append(f)                     # e.g. default value changed -> callers
        elif k.startswith("G|") and _is_data(_lookup(k)):
            globs.append(k.split("|", 2)[1:])   # module constant -> tests using the name
        else:
            bad.append(k)
    if bad:
        return {"ok": False, "why": f"untrackable values changed: {bad[:5]}"}
    return {"ok": True, "changed": consts, "functions": funcs, "globals": globs}


def _is_data(v):
    """Plain data (numbers, strings, containers, enum members, compiled
    regexes, ...): not a module, class, function or other callable."""
    import types as _t
    if v is _MISSING or isinstance(v, (type, _t.ModuleType)) or callable(v):
        return False
    return True


def _plain_function(key):
    """(module, qualname) if the value under `key` is a plain named def
    (possibly a staticmethod/classmethod around it), else None."""
    import types as _t
    o = _lookup(key)
    if isinstance(o, (staticmethod, classmethod)):
        o = o.__func__
    if isinstance(o, _t.FunctionType) and "<" not in o.__qualname__ \
            and "__wrapped__" not in o.__dict__:
        return [o.__module__, o.__qualname__]
    return None


def _is_changed_def(key, changed_defs):
    """Is the (patched) value under `key` one of the defs the patch changed,
    possibly wrapped (staticmethod/classmethod/property/functools.wraps)?"""
    obj = _lookup(key)
    todo, seen = [obj], set()
    while todo:
        o = todo.pop()
        if id(o) in seen or o is _MISSING:
            continue
        seen.add(id(o))
        if (getattr(o, "__module__", None), getattr(o, "__qualname__", None)) in changed_defs \
                and callable(o):
            return True
        for attr in ("__func__", "__wrapped__", "fget", "fset", "fdel"):
            inner = getattr(o, attr, None)
            if inner is not None:
                todo.append(inner)
    return False


def main():
    mode = sys.argv[1]
    if mode == "baseline":
        build_baseline(sys.argv[2], sys.argv[3])
    elif mode == "dump":
        res = _run_fp(sys.argv[2], json.load(sys.stdin))
        res["trackable"] = sorted(res["trackable"])
        json.dump(res, sys.stdout)
    elif mode == "check":
        files = json.load(sys.stdin)
        try:
            out = check(sys.argv[2], sys.argv[3], files)
        except BaseException as e:      # noqa: BLE001  anything odd -> fall back
            out = {"ok": False, "why": f"check crashed: {type(e).__name__}: {e}"}
        json.dump(out, sys.stdout)


if __name__ == "__main__":
    main()
