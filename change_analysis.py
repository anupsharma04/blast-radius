"""
Line-level change analysis for one Python source file (Step 3).

Given the ORIGINAL source of a sqlglot file, a patch's hunks for it, and the
line-coverage map (line -> bitmask of tests that executed that line), work
out which tests could be affected. If anything is unclear, raise FileLevel so
the caller falls back to the safe file-level map (Step 2).

Key idea (why it is safe): the patched program behaves exactly like the
original one until it executes a changed statement. So a test that never
executes any changed statement cannot be affected... provided the change
doesn't do something at import time or change how a function is compiled.
Those cases are detected and sent to the file-level fallback:

  * syntax error in the new file                       -> file level
  * anything outside function bodies changed (module code, class bodies,
    def lines, signatures, decorators, default values) -> file level
  * comment/whitespace-only change (AST identical)     -> file level
  * docstring change                                   -> file level
  * function becomes/stops being a generator or async  -> file level
  * local/closure variables of a function changed      -> whole function
  * lines inserted or deleted (not 1-for-1 replaced)   -> whole function
"""

import ast

FUNC_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef)

# Code-object flags that change what CALLING a function does.
GEN_FLAGS = 0x20 | 0x80 | 0x100 | 0x200  # GENERATOR, COROUTINE, ITERABLE_COROUTINE, ASYNC_GENERATOR


class FileLevel(Exception):
    """Raised when line-level selection isn't safe for this file."""


class PatchError(Exception):
    """Raised when the patch doesn't apply cleanly to the original source."""


def first_line(node):
    """First line of a statement, counting decorators above a def/class."""
    return min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])])


class Func:
    """One function/method: its body line span and where it sits in the file."""
    __slots__ = ("node", "start", "end", "path", "parent")

    def __init__(self, node, path, parent):
        self.node = node
        self.start = first_line(node.body[0])   # first body line (after the def)
        self.end = node.end_lineno
        self.path = path                         # e.g. (("Parser",0), ("_parse_x",0))
        self.parent = parent                     # enclosing Func or None

    def outermost(self):
        f = self
        while f.parent is not None:
            f = f.parent
        return f


def collect_functions(tree):
    """All functions in the file (including methods and nested functions)."""
    funcs = []
    seen = {}

    def visit(node, path, parent):
        for child in ast.iter_child_nodes(node):
            # A def/class can only appear as a statement, so there's no need
            # to look inside expressions (saves most of the work).
            if not isinstance(child, (ast.stmt, ast.excepthandler, ast.match_case)):
                continue
            if isinstance(child, FUNC_TYPES + (ast.ClassDef,)):
                # Same name can appear twice (e.g. property getter/setter),
                # so number the occurrences to give each a unique path.
                key = (path, child.name)
                n = seen.get(key, 0)
                seen[key] = n + 1
                child_path = path + ((child.name, n),)
                if isinstance(child, FUNC_TYPES):
                    f = Func(child, child_path, parent)
                    funcs.append(f)
                    visit(child, child_path, f)
                else:
                    visit(child, child_path, parent)
            else:
                visit(child, path, parent)

    visit(tree, (), None)
    return funcs


def function_body_lines(tree):
    """Set of line numbers that lie inside some function body."""
    lines = set()
    for f in collect_functions(tree):
        lines.update(range(f.start, f.end + 1))
    return lines


def skeleton(node):
    """The file's structure with function BODIES left out. If this differs
    between old and new, something outside function bodies changed."""
    if isinstance(node, FUNC_TYPES):
        return (type(node).__name__, node.name, ast.dump(node.args),
                tuple(ast.dump(d) for d in node.decorator_list),
                ast.dump(node.returns) if node.returns else None,
                tuple(ast.dump(t) for t in getattr(node, "type_params", [])))
    if isinstance(node, ast.AST):
        return (type(node).__name__,) + tuple(skeleton(getattr(node, f, None))
                                               for f in node._fields)
    if isinstance(node, list):
        return tuple(skeleton(x) for x in node)
    return node


def code_shape(func_node, filename):
    """Compile one function on its own and describe how its code objects
    (incl. nested functions/lambdas) are built. Returns (shape, gen_flags)."""
    module = ast.Module(body=[func_node], type_ignores=[])
    try:
        code = compile(module, filename, "exec")
    except (SyntaxError, ValueError) as e:
        raise FileLevel(f"compile error: {e}")
    shape, flags = [], []

    def walk(c):
        shape.append((c.co_name, c.co_argcount, c.co_posonlyargcount, c.co_kwonlyargcount,
                      c.co_varnames, c.co_cellvars, c.co_freevars))
        flags.append(c.co_flags & GEN_FLAGS)
        for const in c.co_consts:
            if hasattr(const, "co_code"):
                walk(const)

    walk(code)
    return shape, flags


def _child_blocks(stmt):
    """Lists of statements nested inside a compound statement."""
    blocks = []
    for field in ("body", "orelse", "finalbody"):
        b = getattr(stmt, field, None)
        if isinstance(b, list) and b and isinstance(b[0], ast.stmt):
            blocks.append(b)
    for h in getattr(stmt, "handlers", []):
        blocks.append(h.body)
    for case in getattr(stmt, "cases", []):
        blocks.append(case.body)
    return blocks


def _is_docstring(stmt):
    return (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant)
            and isinstance(stmt.value.value, str))


def _region_in_block(stmts, line):
    for s in stmts:
        if first_line(s) <= line <= s.end_lineno:
            return _region_in_stmt(s, line)
    return None   # blank/comment line between statements


def _region_in_stmt(stmt, line):
    """Line range(s) whose execution means 'this changed line was reached'."""
    if _is_docstring(stmt):
        return "DOC"
    blocks = _child_blocks(stmt)
    if not blocks:                                   # simple statement
        return [(first_line(stmt), stmt.end_lineno)]
    if isinstance(stmt, (ast.Try, ast.TryStar, ast.Match)):
        # except/case clauses are only evaluated in some situations; just use
        # every test that ran any part of the whole try/match.
        return [(first_line(stmt), stmt.end_lineno)]
    header = (first_line(stmt), first_line(stmt.body[0]) - 1)   # e.g. "if x:" lines
    if header[0] <= line <= header[1]:
        return [header]
    for block in blocks:
        r = _region_in_block(block, line)
        if r is not None:
            return r
    return [header]    # "else:" line, or a comment inside the block


class SourceInfo:
    """Parsed original file (cached per run)."""

    def __init__(self, source, filename):
        self.filename = filename
        self.lines = source.splitlines()
        self.tree = ast.parse(source, filename)
        self.funcs = collect_functions(self.tree)
        self._skeleton = None

    def skeleton(self):
        if self._skeleton is None:
            self._skeleton = skeleton(self.tree)
        return self._skeleton

    def innermost(self, line):
        best = None
        for f in self.funcs:
            if f.start <= line <= f.end and (best is None or f.start >= best.start):
                best = f
        return best


def apply_hunks(old_lines, hunks):
    """Apply unified-diff hunks to the original lines.
    Returns (new_lines, stmt_lines, func_lines):
      stmt_lines: old line numbers replaced 1-for-1 (statement-level OK)
      func_lines: old line numbers around insertions/deletions (function-level)"""
    new, cursor = [], 0            # cursor = number of old lines consumed
    stmt_lines, func_lines = set(), set()
    for old_start, old_count, body in hunks:
        start = old_start - 1 if old_count > 0 else old_start
        if start < cursor or start > len(old_lines):
            raise PatchError("hunks out of order")
        new.extend(old_lines[cursor:start])
        cursor = start
        removed, added = [], 0

        def flush():
            nonlocal removed, added
            if removed or added:
                if removed and added == len(removed):
                    stmt_lines.update(removed)
                else:
                    # Insertion/deletion: the lines just before and after
                    # (old numbering) tell us which function it's in.
                    block_start = removed[0] if removed else cursor + 1
                    block_end = removed[-1] if removed else cursor
                    func_lines.update(removed)
                    func_lines.add(block_start - 1)
                    func_lines.add(block_end + 1)
            removed, added = [], 0

        for text in body:
            tag, content = text[:1], text[1:]
            if tag == " ":
                flush()
                if cursor >= len(old_lines) or old_lines[cursor] != content:
                    raise PatchError("context mismatch")
                new.append(content)
                cursor += 1
            elif tag == "-":
                if cursor >= len(old_lines) or old_lines[cursor] != content:
                    raise PatchError("removed line mismatch")
                removed.append(cursor + 1)          # 1-based old line number
                cursor += 1
            elif tag == "+":
                new.append(content)
                added += 1
            elif tag == "\\":                         # "\ No newline at end of file"
                continue
            else:
                raise PatchError(f"bad hunk line {text[:20]!r}")
        flush()
    new.extend(old_lines[cursor:])
    return new, stmt_lines, func_lines


def affected_mask(info, hunks, cov):
    """Bitmask of tests affected by this file's hunks.
    info: SourceInfo of the original file.
    cov:  dict line -> int bitmask (tests that executed that line).
    Raises FileLevel or PatchError when line-level isn't safe."""
    new_lines, stmt_lines, func_lines = apply_hunks(info.lines, hunks)
    try:
        new_tree = ast.parse("\n".join(new_lines) + "\n", info.filename)
    except (SyntaxError, ValueError) as e:
        raise FileLevel(f"syntax error: {e}")
    if skeleton(new_tree) != info.skeleton():
        raise FileLevel("code outside function bodies changed")

    new_funcs = {f.path: f for f in collect_functions(new_tree)}
    changed = sorted(stmt_lines | func_lines)

    # Every changed line must sit inside a function body.
    owners = {}
    for line in changed:
        f = info.innermost(line)
        if f is None:
            raise FileLevel(f"line {line} is not inside a function body")
        owners[line] = f

    # Compare each changed (outermost) function with its new version.
    escalate, any_code_change = set(), False
    for outer in {f.outermost() for f in owners.values()}:
        new_outer = new_funcs.get(outer.path)
        if new_outer is None:
            raise FileLevel("function moved or renamed")
        if ast.dump(outer.node) != ast.dump(new_outer.node):
            any_code_change = True
        old_shape, old_flags = code_shape(outer.node, info.filename)
        new_shape, new_flags = code_shape(new_outer.node, info.filename)
        if old_flags != new_flags:
            raise FileLevel("generator/async status changed")
        if old_shape != new_shape:
            escalate.add(outer.path)                 # variables changed
    if not any_code_change:
        raise FileLevel("comment/whitespace-only change")

    def span(lo, hi):
        m = 0
        for ln in range(lo, hi + 1):
            m |= cov.get(ln, 0)
        return m

    mask = 0
    for line in changed:
        f = owners[line]
        outer = f.outermost()
        if outer.path in escalate:
            mask |= span(outer.start, outer.end)
            continue
        if line in func_lines:
            mask |= span(f.start, f.end)
            continue
        region = _region_in_block(f.node.body, line)
        if region == "DOC":
            raise FileLevel("docstring changed")
        m = 0
        for lo, hi in region or []:
            m |= span(lo, hi)
        if m == 0:
            # No test ran this exact statement (or it has no bytecode, e.g.
            # `global x`): widen to every test that ran the function.
            m = span(f.start, f.end)
        mask |= m
    return mask
