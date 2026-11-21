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
  * encoding declaration (# -*- coding: ...) changed   -> file level
  * anything that runs at import time changed (module code, class bodies,
    def lines, signatures, decorators, default values, table keys)
                                                       -> file level
  * docstring change                                   -> file level
  * function becomes/stops being a generator or async  -> file level
  * local/closure variables of a function changed      -> whole function
  * lines inserted or deleted (not 1-for-1 replaced)   -> whole function
  * body of a lambda outside functions changed (e.g. a TRANSFORMS entry)
                                                       -> tests that called it
  * comment/whitespace-only change (identical AST)     -> no tests from here

In every case the selector ALSO adds the file's "source observers": tests
that read the file as text or inspect its code objects (e.g. test_typing.py
compares code objects, which include line numbers). Comments and shifted
line numbers are invisible to everything else.
"""

import ast
import re

FUNC_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef)

# Code-object flags that change what CALLING a function does.
GEN_FLAGS = 0x20 | 0x80 | 0x100 | 0x200  # GENERATOR, COROUTINE, ITERABLE_COROUTINE, ASYNC_GENERATOR


class FileLevel(Exception):
    """Raised when line-level selection isn't safe for this file."""


class ValueChange(FileLevel):
    """Only the VALUES of class/module-level assignments changed (e.g.
    `WEEK_OFFSET = -1` -> `-2`). The selector can then ask valuecheck.py
    which class constants really changed and run only the tests that read
    them (valuemap.py); if that isn't possible it treats this like any
    other FileLevel."""

    def __init__(self, msg, new_text):
        super().__init__(msg)
        self.new_text = new_text


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


def outside_lambdas(tree):
    """Lambdas that are NOT inside a function body (e.g. values in class-level
    TRANSFORMS / FUNCTIONS tables), in a fixed order. The lambda object is
    created at import time, but its BODY only runs when it is called."""
    out = []

    def collect(node):
        if isinstance(node, ast.Lambda):
            out.append(node)
            return
        if isinstance(node, FUNC_TYPES):
            # Decorators/defaults/annotations are evaluated at definition
            # time (outside the body), so look for lambdas there too.
            parts = node.decorator_list + [node.args] + ([node.returns] if node.returns else [])
            for p in parts + list(getattr(node, "type_params", [])):
                collect(p)
            return
        for child in ast.iter_child_nodes(node):
            collect(child)

    collect(tree)
    return out


def lambda_lines(tree):
    """Set of line numbers covered by lambdas outside function bodies."""
    lines = set()
    for lam in outside_lambdas(tree):
        lines.update(range(lam.lineno, lam.end_lineno + 1))
    return lines


# Rule B switch: treat lambda bodies in class/module-level tables like small
# functions (only tests that called the lambda), instead of file level.
LAMBDA_RULE = False


def skeleton(node):
    """The file's structure with function BODIES (and, with LAMBDA_RULE,
    bodies of lambdas outside functions) left out. If this differs between
    old and new, something that runs at import time changed."""
    if isinstance(node, FUNC_TYPES):
        return (type(node).__name__, node.name, skeleton(node.args),
                tuple(skeleton(d) for d in node.decorator_list),
                skeleton(node.returns) if node.returns else None,
                tuple(skeleton(t) for t in getattr(node, "type_params", [])))
    if LAMBDA_RULE and isinstance(node, ast.Lambda):
        return ("Lambda", skeleton(node.args))
    if isinstance(node, ast.AST):
        return (type(node).__name__,) + tuple(skeleton(getattr(node, f, None))
                                               for f in node._fields)
    if isinstance(node, list):
        return tuple(skeleton(x) for x in node)
    return node


def _text(lines, node):
    """The full source lines a statement occupies (incl. decorators)."""
    return lines[first_line(node) - 1:node.end_lineno]


def skeleton_equal(old_body, new_body, old_lines, new_lines):
    """Same answer as skeleton(old) == skeleton(new) for two statement lists,
    but faster: a statement whose source text is identical parses to an
    identical tree, so only statements whose text changed are examined
    (class bodies are compared statement by statement the same way)."""
    if len(old_body) != len(new_body):
        return False
    for o, n in zip(old_body, new_body):
        if _text(old_lines, o) == _text(new_lines, n):
            continue
        if type(o) is not type(n):
            return False
        if isinstance(o, ast.ClassDef):
            for field in ("name", "bases", "keywords", "decorator_list", "type_params"):
                if skeleton(getattr(o, field, None)) != skeleton(getattr(n, field, None)):
                    return False
            if not skeleton_equal(o.body, n.body, old_lines, new_lines):
                return False
        elif skeleton(o) != skeleton(n):
            return False
    return True


_ASSIGNS = (ast.Assign, ast.AnnAssign)


def values_only_equal(old_body, new_body, old_lines, new_lines):
    """True if two module/class bodies differ ONLY in the right-hand side of
    plain assignments (same targets, same annotations). Functions must be
    unchanged (identical text or identical AST); everything else (imports,
    class headers, if-blocks, decorators, ...) must have identical structure."""
    if len(old_body) != len(new_body):
        return False
    for o, n in zip(old_body, new_body):
        if _text(old_lines, o) == _text(new_lines, n):
            continue
        if type(o) is not type(n):
            return False
        if isinstance(o, ast.ClassDef):
            for field in ("name", "bases", "keywords", "decorator_list", "type_params"):
                if skeleton(getattr(o, field, None)) != skeleton(getattr(n, field, None)):
                    return False
            if not values_only_equal(o.body, n.body, old_lines, new_lines):
                return False
        elif isinstance(o, ast.Assign):
            if ast.dump(ast.Module(o.targets, [])) != ast.dump(ast.Module(n.targets, [])):
                return False
        elif isinstance(o, ast.AnnAssign):
            if (ast.dump(o.target) != ast.dump(n.target) or o.simple != n.simple
                    or ast.dump(o.annotation) != ast.dump(n.annotation)
                    or (o.value is None) != (n.value is None)):
                return False
        elif ast.dump(o) != ast.dump(n):          # functions, imports, ifs, ...
            return False
    return True


def _moves_lines(hunks):
    """True if some removed line comes back as an added line (entries were
    moved/reordered). The value check ignores the order of some containers,
    so such patches must not rely on it."""
    removed, added = set(), set()
    for _, _, body in hunks:
        for text in body:
            if text[:1] == "-" and text[1:].strip():
                removed.add(text[1:].strip())
            elif text[:1] == "+" and text[1:].strip():
                added.add(text[1:].strip())
    return bool(removed & added)


def code_shape(func_node, filename):
    """Compile one function (or lambda) on its own and describe how its code
    objects (incl. nested functions/lambdas) are built. Returns (shape, gen_flags)."""
    if isinstance(func_node, ast.Lambda):
        module, mode = ast.Expression(body=func_node), "eval"
    else:
        module, mode = ast.Module(body=[func_node], type_ignores=[]), "exec"
    try:
        code = compile(module, filename, mode)
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
        self._outer = None
        self._dumps = {}
        self._lambdas = None

    def skeleton(self):
        if self._skeleton is None:
            self._skeleton = skeleton(self.tree)
        return self._skeleton

    def outer_funcs(self):
        """path -> Func, for every OUTERMOST function (cached)."""
        if self._outer is None:
            self._outer = {f.path: f for f in self.funcs if f.parent is None}
        return self._outer

    def dump_of(self, path):
        if path not in self._dumps:
            self._dumps[path] = ast.dump(self.outer_funcs()[path].node)
        return self._dumps[path]

    def lambdas(self):
        if self._lambdas is None:
            self._lambdas = outside_lambdas(self.tree)
        return self._lambdas

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


# Rule A switch: a comment/whitespace-only change selects no tests here; the
# caller always adds the file's "source observers" (tests that read the file
# as text or inspect its code objects - the only tests that can notice).
COMMENT_RULE = True

_COOKIE = re.compile(r"^[ \t\f]*#.*?coding[:=]")


def _cookie_lines(lines):
    """Encoding declarations in the first two lines (they change how the
    whole file is decoded, so they are never 'just a comment')."""
    return [ln for ln in lines[:2] if _COOKIE.match(ln)]


def affected_mask(info, hunks, cov):
    """Bitmask of tests affected by this file's hunks.
    info: SourceInfo of the original file.
    cov:  dict line -> int bitmask (tests that executed that line).
    Raises FileLevel or PatchError when line-level isn't safe.
    (The caller adds the file's source observers to the result.)"""
    new_lines, stmt_lines, func_lines = apply_hunks(info.lines, hunks)
    if _cookie_lines(new_lines) != _cookie_lines(info.lines):
        raise FileLevel("encoding declaration changed")
    try:
        new_tree = ast.parse("\n".join(new_lines) + "\n", info.filename)
    except (SyntaxError, ValueError) as e:
        raise FileLevel(f"syntax error: {e}")
    if not skeleton_equal(info.tree.body, new_tree.body, info.lines, new_lines):
        if values_only_equal(info.tree.body, new_tree.body, info.lines, new_lines)                 and not _moves_lines(hunks):
            raise ValueChange("only class/module constant values changed",
                              "\n".join(new_lines) + "\n")
        raise FileLevel("code that runs at import time changed")

    # Which outermost functions and which outside-lambdas really changed?
    # (The skeleton is equal, so every code change is inside one of them.)
    new_funcs_list = collect_functions(new_tree)
    new_funcs = {f.path: f for f in new_funcs_list}
    old_outer = info.outer_funcs()
    new_outer = {f.path: f for f in new_funcs_list if f.parent is None}
    if set(old_outer) != set(new_outer):
        raise FileLevel("functions added, removed or renamed")

    def same_text(old_node, new_node):
        # Identical source text -> identical code; skip the expensive AST
        # comparison (most functions in a file are untouched).
        return (info.lines[first_line(old_node) - 1:old_node.end_lineno]
                == new_lines[first_line(new_node) - 1:new_node.end_lineno])

    changed_outer = {p for p, of in old_outer.items()
                     if not same_text(of.node, new_outer[p].node)
                     and info.dump_of(p) != ast.dump(new_outer[p].node)}

    old_lams, new_lams = info.lambdas(), outside_lambdas(new_tree)
    if len(old_lams) != len(new_lams):
        raise FileLevel("lambdas added or removed")
    changed_lams = [i for i, lam in enumerate(new_lams)
                    if not same_text(old_lams[i], lam)
                    and ast.dump(old_lams[i]) != ast.dump(lam)]

    if not changed_outer and not changed_lams:
        if not COMMENT_RULE:
            raise FileLevel("comment/whitespace-only change")
        return 0                                    # only source observers can tell

    def span(lo, hi):
        m = 0
        for ln in range(lo, hi + 1):
            m |= cov.get(ln, 0)
        return m

    mask = 0
    # Changed lambda bodies: only tests that called the lambda can be affected.
    for i in changed_lams:
        if not LAMBDA_RULE:
            raise FileLevel("lambda changed")
        old_shape = code_shape(old_lams[i], info.filename)
        new_shape = code_shape(new_lams[i], info.filename)
        if old_shape != new_shape:
            raise FileLevel("lambda variables/generator status changed")
        mask |= span(old_lams[i].lineno, old_lams[i].end_lineno)

    if not changed_outer:
        return mask

    # Changed functions: compare compiled shape with the new version.
    escalate = set()
    for path in changed_outer:
        old_f = old_outer[path]
        old_shape, old_flags = code_shape(old_f.node, info.filename)
        new_shape, new_flags = code_shape(new_funcs[path].node, info.filename)
        if old_flags != new_flags:
            raise FileLevel("generator/async status changed")
        if old_shape != new_shape:
            escalate.add(path)                       # variables changed

    # Map changed lines to the function they're in (old numbering). Lines
    # outside functions or in unchanged functions are comment/whitespace
    # edits (the skeleton/dump checks above prove it): nothing to run.
    owners = {}
    for line in sorted(stmt_lines | func_lines):
        f = info.innermost(line)
        if f is not None and f.outermost().path in changed_outer:
            owners[line] = f

    # Safety net: a changed function that no changed line points into
    # (shouldn't happen) -> every test that ran any of it.
    for path in changed_outer - {f.outermost().path for f in owners.values()}:
        old_f = old_outer[path]
        mask |= span(old_f.start, old_f.end)

    for line, f in owners.items():
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
