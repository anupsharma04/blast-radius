"""
Build maps/docreaders.json: who READS docstrings at the pinned commit.

A patch that only edits docstring text changes nothing but __doc__ strings
(and maybe line numbers, which the "source observers" already cover). So
only code that reads docstrings can notice. This scans the checkout for
docstring readers:
  * test files / test helpers that mention one of READ_TOKENS
    (tests/test_docs.py runs every doctest in sqlglot);
  * lines in sqlglot/ that mention one of READ_TOKENS. At runtime the
    selector adds every test that executed such a line (line coverage).
A reader line that is not inside a function (runs at import time for every
test) is listed under "import_time"; the selector then disables the rule.

One pattern is not a reader: setattr(X, "__doc__", getattr(X, "__doc__", ...) ...)
writes a class's own docstring back onto it (sqlglot uses it to turn None
into ""); the text only matters to whoever reads it later, and those
readers are found by the same scan.

Usage (inside the builder image):  python tools/build_docmap.py /opt/sqlglot maps/docreaders.json
"""

import ast
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
import change_analysis  # noqa: E402

READ_TOKENS = ("__doc__", "doctest", "getdoc", "pydoc", "cleandoc", "help(")


def is_self_copy(stmt):
    """setattr(X, "__doc__", <expr that reads only X.__doc__>)"""
    if not (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call)):
        return False
    call = stmt.value
    if not (isinstance(call.func, ast.Name) and call.func.id == "setattr" and len(call.args) == 3):
        return False
    target, name, value = call.args
    if not (isinstance(name, ast.Constant) and name.value == "__doc__"):
        return False
    target_dump = ast.dump(target)
    for node in ast.walk(value):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id == "getattr" and len(node.args) >= 2:
            if ast.dump(node.args[0]) != target_dump:
                return False
        elif isinstance(node, ast.Attribute) and node.attr == "__doc__":
            if ast.dump(node.value) != target_dump:
                return False
        elif isinstance(node, ast.Name) and node.id in ("doctest", "pydoc", "help"):
            return False
    return True


def scan(root):
    test_files, src_lines, import_time = [], {}, []
    for top in ("sqlglot", "tests"):
        for dirpath, dirnames, files in os.walk(os.path.join(root, top)):
            dirnames[:] = sorted(d for d in dirnames if d != "__pycache__")
            for f in sorted(files):
                if not f.endswith(".py"):
                    continue
                path = os.path.join(dirpath, f)
                rel = os.path.relpath(path, root).replace(os.sep, "/")
                with open(path, encoding="utf-8") as fh:
                    text = fh.read()
                if not any(tok in text for tok in READ_TOKENS):
                    continue
                if top == "tests":
                    test_files.append(rel)
                    continue
                tree = ast.parse(text)
                in_func = change_analysis.function_body_lines(tree)
                lines = text.splitlines()
                # statement that contains each line
                stmts = {}
                for node in ast.walk(tree):
                    if isinstance(node, ast.stmt) and not isinstance(
                            node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                        for ln in range(node.lineno, node.end_lineno + 1):
                            if ln not in stmts or stmts[ln].lineno < node.lineno:
                                stmts[ln] = node
                for i, line in enumerate(lines, 1):
                    code = line.split("#", 1)[0]
                    if not any(tok in code for tok in READ_TOKENS):
                        continue
                    stmt = stmts.get(i)
                    if stmt is not None and is_self_copy(stmt):
                        continue
                    if _inside_string(tree, i):
                        continue                 # e.g. ">>> x  # doctest: +SKIP" in a docstring
                    if i in in_func:
                        src_lines.setdefault(rel, []).append(i)
                    else:
                        import_time.append(f"{rel}:{i}")
    return {"tokens": list(READ_TOKENS), "test_files": test_files,
            "src_lines": src_lines, "import_time": import_time}


def _inside_string(tree, line):
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and node.lineno < line <= node.end_lineno:
            return True
        if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and node.lineno == line == node.end_lineno and "__doc__" not in node.value:
            continue
    return False


if __name__ == "__main__":
    result = scan(sys.argv[1])
    with open(sys.argv[2], "w") as f:
        json.dump(result, f, indent=1)
    print(json.dumps(result, indent=1))
