"""
Value-level change detection (used for edits to class-level constants).

Problem: a patch like `WEEK_OFFSET = -1` -> `-2` inside `class TSQL` changes
code that runs at IMPORT time, so line coverage can't say which tests care.
Until now we fell back to "every test that imports the file" (often 1000+).

Idea, in two halves:

1. Map building (tools/linecov_site/sitecustomize.py): every class-level
   constant of every sqlglot class (e.g. TSQL.WEEK_OFFSET,
   SingleStoreParser.SHOW_PARSERS) is temporarily replaced by a tiny
   "recording descriptor" that returns the same value but notes which test
   read it. So the map says: (class, attribute) -> tests that read it.
   Reads made while sqlglot itself is being imported are NOT counted;
   whatever those reads compute gets stored somewhere, and half 2 compares
   everything that gets stored.

2. Runtime (selector): import the PATCHED sqlglot in a child process and
   compute a fingerprint (a hash of the value) of every class attribute and
   every module global. Compare with the fingerprints of the original code
   (computed at docker build time). Then:
     * only class attributes that the recorder could track changed
         -> run the tests that read one of them;
     * anything else changed (a module global, an Enum, a function, a class
       added/removed, an import error, ...) -> caller falls back to the
       file-level map, exactly as before.

This file must not import sqlglot at module level: the recorder loads it in
every test process before sqlglot is imported.
"""

import enum
import os
import re
import sys
import types

# Dunder attributes that legitimately differ when only line numbers move.
SKIP_DUNDERS = {"__firstlineno__"}
# Module bookkeeping set by the import system (differs under the import hook
# that loads the patched text; never something sqlglot code computes).
SKIP_MODULE_GLOBALS = {"__loader__", "__spec__", "__cached__", "__builtins__", "__file__"}


def is_dunder(name):
    return name.startswith("__") and name.endswith("__")


def class_key(cls):
    return f"{cls.__module__}:{cls.__qualname__}"


def module_classes(module):
    """Classes DEFINED in `module` (found through its globals), including
    nested classes (Snowflake.Tokenizer), in a fixed order."""
    out, seen = [], set()
    name = module.__name__

    def visit(cls):
        if id(cls) in seen:
            return
        seen.add(id(cls))
        out.append(cls)
        for v in list(cls.__dict__.values()):
            if isinstance(v, type) and v.__module__ == name \
                    and v.__qualname__.startswith(cls.__qualname__ + "."):
                visit(v)

    for v in list(vars(module).values()):
        if isinstance(v, type) and v.__module__ == name and "<locals>" not in v.__qualname__:
            visit(v)
    return out


def trackable(cls, name, value):
    """Can the recorder put a recording descriptor on cls.<name>?
    Only plain data (numbers, strings, dicts, lists, ...): not functions or
    other descriptors (they implement __get__), not nested classes, not
    Enum members, not dunders."""
    if is_dunder(name) or isinstance(cls, enum.EnumMeta):
        return False
    if isinstance(value, type) or hasattr(type(value), "__get__"):
        return False
    return True


# ---------------------------------------------------------------------------
# Fingerprints
# ---------------------------------------------------------------------------

_ADDRESS = re.compile(r"0x[0-9a-fA-F]{6,}")

_PRIMITIVES = {type(None): "n", bool: "b", int: "i", float: "f", complex: "c",
               str: "s", bytes: "y"}


class Fingerprinter:
    """fp(obj) -> an int hash of the object's VALUE (not its identity).
    Built on Python's hash(), which is the same in every process as long as
    PYTHONHASHSEED is fixed (valuecheck.py always sets it to 0).
    Line numbers are left out on purpose: a moved line is the business of
    the "source observers" (see change_analysis.py)."""

    def __init__(self, loose=False):
        # loose=True: ignore the ORDER of dict items and list/tuple items.
        # Used only for values whose order differs between two runs of the
        # ORIGINAL code (built by iterating a set of classes, which hash by
        # memory address), so no reliably passing test depends on it.
        self.loose = loose
        self.memo = {}
        self.keep = []           # keep objects alive so id() stays unique

    def fp(self, v):
        t = type(v)
        tag = _PRIMITIVES.get(t)
        if tag is not None:
            # Numbers go through repr(): hash() of numbers collides on
            # purpose (hash(-1) == hash(-2), hash(2**61) == hash(1), ...).
            return hash((tag, v if t is str or t is bytes else repr(v)))
        k = id(v)
        got = self.memo.get(k)
        if got is not None:
            return got
        self.memo[k] = -1                  # placeholder while visiting (cycles)
        self.keep.append(v)
        d = hash(self._parts(v, t))
        self.memo[k] = d
        return d

    def _parts(self, v, t):
        fp = self.fp
        if isinstance(v, type):
            return ("T", class_key(v))
        if t is types.ModuleType:
            return ("M", v.__name__)
        if isinstance(v, enum.Enum):
            return ("E", class_key(t), str(v.name), fp(v.value))
        # Order is ignored (a) in loose mode and (b) for sequences of classes /
        # dicts keyed by classes: those are usually built by iterating a set
        # of classes, whose order follows memory addresses and so changes
        # between processes. (A patch that only MOVES lines never reaches
        # this check: change_analysis.py sends it to file level.)
        if t is tuple or t is list:
            items = [fp(x) for x in v]
            unordered = self.loose or (v and all(isinstance(x, type) for x in v))
            return (t.__name__, tuple(sorted(items) if unordered else items))
        if t is dict:
            items = [(fp(a), fp(b)) for a, b in v.items()]
            unordered = self.loose or (v and all(isinstance(x, type) for x in v))
            return ("d", tuple(sorted(items) if unordered else items))
        if t is set or t is frozenset:
            return (t.__name__, tuple(sorted(fp(x) for x in v)))
        if t is types.FunctionType:
            q = v.__qualname__
            if "<lambda>" not in q and "<locals>" not in q and "__wrapped__" not in v.__dict__:
                # A plain `def` at module/class level: compare by NAME only.
                # Changes to its signature/body are found from the patch
                # (change_analysis.py) and select every test that ran it.
                return ("FR", getattr(v, "__module__", "") or "", q)
            return ("F", v.__qualname__, self.code(v.__code__), fp(v.__defaults__),
                    fp(v.__kwdefaults__), fp(tuple(self.cell(c) for c in v.__closure__ or ())),
                    fp(v.__dict__))
        if t is types.CodeType:
            return ("C", self.code(v))
        if t is types.MethodType:
            return ("BM", fp(v.__func__), fp(v.__self__))
        if t in (types.BuiltinFunctionType, types.WrapperDescriptorType,
                 types.MethodDescriptorType, types.MethodWrapperType,
                 types.ClassMethodDescriptorType, types.GetSetDescriptorType,
                 types.MemberDescriptorType):
            owner = getattr(v, "__self__", None)
            return ("B", getattr(v, "__module__", "") or "", getattr(v, "__qualname__", t.__name__),
                    fp(owner) if t is types.BuiltinFunctionType
                    and not isinstance(owner, types.ModuleType) else 0)
        if t is staticmethod or t is classmethod:
            return (t.__name__, fp(v.__func__))
        if t is property:
            return ("P", fp(v.fget), fp(v.fset), fp(v.fdel))
        if isinstance(v, re.Pattern):
            return ("re", v.pattern, v.flags)
        # Any other object: its type, its contents if it is a container
        # subclass (defaultdict, OrderedDict, namedtuple, ...), its attributes,
        # and its repr() if its class defines one (datetime, Decimal, ...).
        parts = ["O", class_key(t)]
        if isinstance(v, dict):
            parts.append(("d", tuple((fp(a), fp(b)) for a, b in v.items())))
            factory = getattr(v, "default_factory", None)
            if factory is not None:
                parts.append(fp(factory))
        elif isinstance(v, (list, tuple)):
            parts.append(("l", tuple(fp(x) for x in v)))
        elif isinstance(v, (set, frozenset)):
            parts.append(("s", tuple(sorted(fp(x) for x in v))))
        d = getattr(v, "__dict__", None)
        if isinstance(d, dict):
            parts.append(fp(d))
        if t.__repr__ is not object.__repr__ and not isinstance(v, (dict, list, tuple, set,
                                                                   frozenset)):
            try:
                r = repr(v)
            except Exception:            # noqa: BLE001
                r = "<repr failed>"
            # Drop memory addresses ("<Foo object at 0x7f..>"), which differ
            # between processes; the object's attributes are compared anyway.
            parts.append(_ADDRESS.sub("0x?", r))
        for klass in t.__mro__:
            for slot in klass.__dict__.get("__slots__", ()):
                if isinstance(slot, str) and slot not in ("__dict__", "__weakref__"):
                    parts.append((slot, fp(getattr(v, slot)) if hasattr(v, slot) else None))
        if t.__module__ == "functools" and hasattr(v, "func"):      # partial
            parts += [fp(v.func), fp(v.args), fp(v.keywords)]
        return tuple(parts)

    @staticmethod
    def cell(c):
        try:
            return c.cell_contents
        except ValueError:
            return "<empty cell>"

    def code(self, c):
        return hash((c.co_qualname, c.co_code, c.co_flags, c.co_argcount,
                     c.co_kwonlyargcount, c.co_posonlyargcount, c.co_names,
                     c.co_varnames, c.co_freevars, c.co_cellvars,
                     tuple(self.code(k) if type(k) is types.CodeType else self.fp(k)
                           for k in c.co_consts)))


def self_test():
    """Values that must never get the same fingerprint (hash() pitfalls)."""
    f = Fingerprinter()
    groups = [-1, -2, 0, 1, True, 1.0, 2 ** 61, 2 ** 61 - 1, -0.0, 0.0, "1", b"1", None,
              (1, 2), (2, 1), [1, 2], {"a": 1}, {"a": 2}, {1, 2}, {1, 3}]
    import collections
    import datetime
    import decimal
    groups += [collections.defaultdict(list, a=[1]), collections.defaultdict(list, a=[2]),
               collections.OrderedDict(a=1), collections.OrderedDict(a=3),
               collections.namedtuple("P", "x")(1), collections.namedtuple("P", "x")(2),
               decimal.Decimal("1.5"), decimal.Decimal("2.5"),
               datetime.date(2020, 1, 1), datetime.date(2020, 1, 2)]
    seen = {}
    for g in groups:
        d = f.fp(g)
        assert d not in seen, f"fingerprint collision: {g!r} vs {seen[d]!r}"
        seen[d] = g


def all_module_names(src_root):
    """Every importable module under src_root/sqlglot, sorted."""
    names = []
    base = os.path.join(src_root, "sqlglot")
    for dirpath, dirnames, files in os.walk(base):
        dirnames[:] = sorted(d for d in dirnames if d != "__pycache__"
                             and os.path.isfile(os.path.join(dirpath, d, "__init__.py")))
        rel = os.path.relpath(dirpath, src_root).replace(os.sep, ".")
        for f in sorted(files):
            if f.endswith(".py") and f != "__main__.py":
                names.append(rel if f == "__init__.py" else f"{rel}.{f[:-3]}")
    return sorted(names)


def fingerprint_all(src_root, loose=False):
    """Import every sqlglot module and fingerprint every class attribute and
    module global. Returns {"failed": [...], "pairs": {key: digest},
    "trackable": set of class-attribute keys the recorder can track}."""
    import importlib
    self_test()
    failed = []
    for name in all_module_names(src_root):
        try:
            importlib.import_module(name)
        except Exception as e:   # noqa: BLE001  (record, compare later)
            failed.append(f"{name}: {type(e).__name__}")
    f = Fingerprinter(loose)
    pairs, track, cached = {}, set(), 0
    for name in sorted(n for n in sys.modules if n == "sqlglot" or n.startswith("sqlglot.")):
        mod = sys.modules[name]
        if mod is None:
            continue
        classes = module_classes(mod)
        defined = {id(c) for c in classes}
        for gname, v in list(vars(mod).items()):
            if gname in SKIP_DUNDERS or gname in SKIP_MODULE_GLOBALS                     or (isinstance(v, type) and id(v) in defined):
                continue
            pairs[f"G|{name}|{gname}"] = f.fp(v)
            info = getattr(v, "cache_info", None)
            if callable(info):
                try:
                    cached += info().currsize
                except Exception:  # noqa: BLE001
                    pass
        for cls in classes:
            ck = class_key(cls)
            pairs[f"K|{ck}"] = f.fp(tuple(class_key(b) for b in cls.__bases__))
            for aname, v in list(cls.__dict__.items()):
                if aname in SKIP_DUNDERS:
                    continue
                key = f"A|{ck}|{aname}"
                pairs[key] = f.fp(v)
                if trackable(cls, aname, v):
                    track.add(key)
    return {"failed": failed, "pairs": pairs, "trackable": track, "cached": cached}
