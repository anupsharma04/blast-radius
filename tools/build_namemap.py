"""
Build maps/names.json.gz: for every name bound at module level in sqlglot
(constants like DATE_UNITS, EXPRESSION_METADATA, ...), which tests can see it.

Used when the runtime fingerprint check (valuecheck.py) finds that a
module-level DATA constant changed. A function can only read a module
constant by mentioning its name: as a global (LOAD_GLOBAL), as an attribute
(`module.NAME`, LOAD_ATTR) or as a string (`getattr(module, "NAME")`). All
three end up in the function's code object (co_names / co_consts). So:

  readers[NAME] = tests that executed any function (or lambda) in sqlglot
                  whose code mentions NAME          (line coverage, maps/cov)
  test_files[NAME] = test files whose code mentions NAME (they read it
                  directly); all their tests run.

Code that runs at import time and reads NAME stores what it computes
somewhere; the fingerprint check sees that as a separate change.
The only generic module scans in sqlglot (dir(module) in Dialect._try_load,
inspect.getmembers in helper.subclasses) keep classes only; the selector
uses this map only for data constants, never classes or functions.

Usage (builder image, after tools/build_linemap.sh):
  python tools/build_namemap.py /opt/sqlglot maps
"""

import ast
import gzip
import inspect
import json
import os
import sys
import types


def module_level_names(tree):
    names = set()
    for node in tree.body:
        for sub in ast.walk(node) if isinstance(node, (ast.If, ast.Try)) else [node]:
            if isinstance(sub, ast.Assign):
                for tgt in sub.targets:
                    for n in ast.walk(tgt):
                        if isinstance(n, ast.Name):
                            names.add(n.id)
            elif isinstance(sub, (ast.AnnAssign, ast.AugAssign)) and isinstance(sub.target, ast.Name):
                names.add(sub.target.id)
            elif isinstance(sub, (ast.Import, ast.ImportFrom)):
                for a in sub.names:
                    names.add((a.asname or a.name).split(".")[0])
            elif isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(sub.name)
    return names


def code_objects(code):
    yield code
    for c in code.co_consts:
        if isinstance(c, types.CodeType):
            yield from code_objects(c)


def mentioned(code):
    out = set(code.co_names)
    for c in code.co_consts:
        if isinstance(c, str) and c.isidentifier():
            out.add(c)
    return out


def iter_py(root, top):
    for dp, dn, fn in os.walk(os.path.join(root, top)):
        dn[:] = sorted(d for d in dn if d != "__pycache__")
        for f in sorted(fn):
            if f.endswith(".py"):
                path = os.path.join(dp, f)
                yield os.path.relpath(path, root).replace(os.sep, "/"), path


def main():
    root, maps = sys.argv[1], sys.argv[2]
    sources = {}
    global_names = set()
    for rel, path in iter_py(root, "sqlglot"):
        with open(path, encoding="utf-8") as f:
            sources[rel] = f.read()
        global_names |= module_level_names(ast.parse(sources[rel]))

    readers = {}
    for rel, text in sources.items():
        cov_path = os.path.join(maps, "cov", rel.replace("/", "__") + ".json.gz")
        cov = {}
        if os.path.exists(cov_path):
            with gzip.open(cov_path, "rt") as f:
                d = json.load(f)
            sets = [int(s, 16) for s in d["sets"]]
            cov = {ln: sets[k] for ln, k in d["lines"]}
        for code in code_objects(compile(text, rel, "exec")):
            if not code.co_flags & inspect.CO_OPTIMIZED:
                continue                        # module/class body: import time
            names = mentioned(code) & global_names
            if not names:
                continue
            mask = 0
            for _, _, ln in code.co_lines():
                if ln is not None:
                    mask |= cov.get(ln, 0)
            for n in names:
                readers[n] = readers.get(n, 0) | mask

    test_files = {}
    for rel, path in iter_py(root, "tests"):
        with open(path, encoding="utf-8") as f:
            try:
                code = compile(f.read(), rel, "exec")
            except SyntaxError:
                continue
        names = set()
        for c in code_objects(code):
            names |= mentioned(c)
        for n in names & global_names:
            test_files.setdefault(n, []).append(rel)

    sets, index, out = [], {}, {}
    for n, m in sorted(readers.items()):
        if m not in index:
            index[m] = len(sets)
            sets.append(format(m, "x"))
        out[n] = index[m]
    with gzip.open(os.path.join(maps, "names.json.gz"), "wt") as f:
        json.dump({"sets": sets, "readers": out, "test_files": test_files,
                   "names": sorted(global_names)}, f, separators=(",", ":"))
    print(f"{len(global_names)} module-level names, {len(readers)} read by functions, "
          f"{len(test_files)} mentioned by tests", file=sys.stderr)


if __name__ == "__main__":
    main()
