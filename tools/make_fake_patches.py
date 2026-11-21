"""
Generate realistic FAKE patches against sqlglot (pinned commit) for testing
our selector's safety on kinds of change the sample set may not contain.

These patches are ONLY used by tools/validate.py to find misses. Nothing in
the selector or the maps is built from them.

Usage (any OS):  python tools/make_fake_patches.py <path to pristine sqlglot checkout>
Writes tools/fake_patches/<name>.patch and tools/fake_patches/kinds.json.
"""

import difflib
import json
import os
import sys

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_patches")

# (name, kind, file, operation, old text, new text)
#   operation: "edit" (replace old with new; old must occur exactly once),
#              "new" (create file with `new`), "delete" (remove file)
EDITS = [
    ("rename_function", "rename a module-level function (callers unchanged)",
     "sqlglot/optimizer/eliminate_joins.py", "edit",
     "def _join_is_used(scope: Scope, join: exp.Join, alias: str) -> bool:",
     "def _join_used(scope: Scope, join: exp.Join, alias: str) -> bool:"),
    ("rename_method", "rename a generator method (dispatch by name)",
     "sqlglot/generators/duckdb.py", "edit",
     "    def timeslice_sql(self, expression: exp.TimeSlice) -> str:",
     "    def time_slice_sql(self, expression: exp.TimeSlice) -> str:"),
    ("signature_required_arg", "function signature: optional arg becomes required",
     "sqlglot/optimizer/normalize_identifiers.py", "edit",
     "def normalize_identifiers(expression, dialect=None, store_original_column_identifiers=False):",
     "def normalize_identifiers(expression, dialect, store_original_column_identifiers=False):"),
    ("signature_default_value", "function signature: default value changed",
     "sqlglot/optimizer/normalize_identifiers.py", "edit",
     "def normalize_identifiers(expression, dialect=None, store_original_column_identifiers=False):",
     "def normalize_identifiers(expression, dialect=None, store_original_column_identifiers=True):"),
    ("import_removed", "import line removed",
     "sqlglot/dialects/tsql.py", "edit",
     "from sqlglot.tokens import TokenType\n", ""),
    ("module_dict_entry", "class-level FUNCTIONS table entry changed",
     "sqlglot/parsers/duckdb.py", "edit",
     '"STRFTIME": build_formatted_time(exp.TimeToStr),',
     '"STRFTIME": build_formatted_time(exp.StrToTime),'),
    ("transforms_lambda", "lambda inside class-level TRANSFORMS dict changed",
     "sqlglot/generators/drill.py", "edit",
     'exp.CurrentTimestamp: lambda *_: "CURRENT_TIMESTAMP",',
     'exp.CurrentTimestamp: lambda *_: "NOW()",'),
    ("base_class_change", "class base class changed (Postgres -> MySQL)",
     "sqlglot/dialects/materialize.py", "edit",
     "from sqlglot.dialects.postgres import Postgres\n",
     "from sqlglot.dialects.mysql import MySQL as Postgres\n"),
    ("docstring_edit", "docstring text edited",
     "sqlglot/helper.py", "edit",
     '"""Returns the value in `seq` at position `index`, or `None` if `index` is out of bounds."""',
     '"""Return the value at `index` in `seq`, or `None` when the index is out of range."""'),
    ("comment_only_py", "comment added inside a function",
     "sqlglot/generators/clickhouse.py", "edit",
     "    asc, nulls_first = order\n",
     "    # order is (ascending, nulls_first)\n    asc, nulls_first = order\n"),
    ("comment_only_sql", "SQL '--' comment line edited in a fixture (diff shows '--- ')",
     "tests/fixtures/optimizer/qualify_columns.sql", "edit",
     "-- Qualify columns\n", "-- Qualify all columns\n"),
    ("new_file", "new source file added",
     "sqlglot/dialects/mydialect.py", "new", None,
     "from sqlglot.dialects.dialect import Dialect\n\n\nclass MyDialect(Dialect):\n    pass\n"),
    ("delete_file", "source file deleted",
     "sqlglot/dialects/doris.py", "delete", None, None),
    ("init_edit", "__init__.py edited",
     "sqlglot/optimizer/__init__.py", "edit",
     '"""Lazy re-exports from optimizer submodules.',
     '"""Lazy re-exports from the optimizer submodules.'),
    ("test_file_edit", "test file assertion changed",
     "tests/test_transpile.py", "edit",
     'self.assertEqual(transpile("SELECT 1 overwrite")[0], "SELECT 1 AS overwrite")',
     'self.assertEqual(transpile("SELECT 1 overwrite")[0], "SELECT 1 AS overwrite_")'),
    ("test_helper_edit", "shared test helper edited (tests/helpers.py)",
     "tests/helpers.py", "edit",
     'if line and not line.startswith("--")]',
     'if line and not line.startswith("-")]'),
    ("pyproject_edit", "non-Python config file edited",
     "pyproject.toml", "edit",
     'requires-python = ">= 3.9"', 'requires-python = ">= 3.10"'),
    ("syntax_error", "syntax error inside a function body",
     "sqlglot/generators/clickhouse.py", "edit",
     "    if nulls_first:\n        self.unsupported(\"arraySort and arrayReverseSort",
     "    if nulls_first\n        self.unsupported(\"arraySort and arrayReverseSort"),
    ("insert_statement", "new statement inserted in a function body",
     "sqlglot/optimizer/eliminate_joins.py", "edit",
     "    inner_source = scope.sources.get(alias)\n",
     "    inner_source = scope.sources.get(alias)\n    if join.kind == \"CROSS\":\n        return True\n"),
    # --- kinds aimed at the comment / lambda / observer rules ---
    ("comment_shift_typing", "comment line inserted at top of a typing module (shifts line numbers)",
     "sqlglot/typing/snowflake.py", "edit",
     "from __future__ import annotations\n",
     "from __future__ import annotations\n\n# Snowflake-specific type annotators\n"),
    ("blank_lines_in_function", "blank lines/whitespace added inside a function",
     "sqlglot/generators/clickhouse.py", "edit",
     "    asc, nulls_first = order\n",
     "\n    asc, nulls_first = order\n\n"),
    ("encoding_cookie", "encoding declaration added to a file with non-ASCII text",
     "sqlglot/tokens.py", "edit",
     "from __future__ import annotations\n",
     "# -*- coding: latin-1 -*-\nfrom __future__ import annotations\n"),
    ("lambda_body_change", "lambda body in class-level TRANSFORMS changed (used by tests)",
     "sqlglot/generators/duckdb.py", "edit",
     'exp.LogicalOr: lambda self, e: self.func("BOOL_OR", _cast_to_boolean(e.this)),',
     'exp.LogicalOr: lambda self, e: self.func("BOOL_OR", e.this),'),
    ("lambda_table_key", "key of a TRANSFORMS lambda entry changed",
     "sqlglot/generators/duckdb.py", "edit",
     'exp.LogicalAnd: lambda self, e: self.func("BOOL_AND", _cast_to_boolean(e.this)),',
     'exp.LogicalOr: lambda self, e: self.func("BOOL_AND", _cast_to_boolean(e.this)),'),
    ("comment_in_class_table", "comment added inside a class-level dict",
     "sqlglot/generators/duckdb.py", "edit",
     '        exp.LogicalOr: lambda self, e:',
     '        # boolean aggregates\n        exp.LogicalOr: lambda self, e:'),
    ("body_operator_swap", "operator swapped in a function body",
     "sqlglot/generators/clickhouse.py", "edit",
     'return self.func("arraySort" if asc else "arrayReverseSort", expression.this)',
     'return self.func("arraySort" if not asc else "arrayReverseSort", expression.this)'),
    ("syntax_error_eager_module", "syntax error in a module every test imports (diff.py)",
     "sqlglot/diff.py", "edit",
     "        return node_mapping\n", "        return node_mapping)\n"),
    # --- def-line changes (signature / default / decorator) ---
    ("sig_method_default", "default value of a widely used Generator method changed",
     "sqlglot/generator.py", "edit",
     '    def seg(self, sql: str, sep: str = " ") -> str:',
     '    def seg(self, sql: str, sep: str = "  ") -> str:'),
    ("sig_parser_method_default", "default of a dialect parser method changed",
     "sqlglot/parsers/mysql.py", "edit",
     "        this: str,\n        target: bool | str = False,\n        full: bool | None = None,",
     "        this: str,\n        target: bool | str = True,\n        full: bool | None = None,"),
    ("sig_factory_default", "default of a factory called at import time to build tables",
     "sqlglot/dialects/dialect.py", "edit",
     '    unit_mapping: dict[str, str] | None = None,\n    default_unit: str | None = "DAY",',
     '    unit_mapping: dict[str, str] | None = None,\n    default_unit: str | None = "WEEK",'),
    ("sig_required_arg_method", "method gets a new required argument",
     "sqlglot/generator.py", "edit",
     '    def sep(self, sep: str = " ") -> str:',
     '    def sep(self, sep: str) -> str:'),
    ("decorator_classmethod_removed", "@classmethod removed from a method",
     "sqlglot/expressions/core.py", "edit",
     "    @classmethod\n    def load(cls, obj: list[dict[str, Any]] | None) -> Expr:",
     "    def load(cls, obj: list[dict[str, Any]] | None) -> Expr:"),
    # --- docstring-only changes (tools/build_docmap.py) ---
    ("docstring_class_expr", "class docstring of the base Expr edited (sqlglot copies __doc__ at import)",
     "sqlglot/expressions/core.py", "edit",
     "    The base class for all expressions in a syntax tree. Each Expr encapsulates",
     "    The base class of every expression in a syntax tree. Each Expr encapsulates"),
    ("docstring_module", "module docstring edited",
     "sqlglot/expressions/core.py", "edit",
     '"""sqlglot expressions core - base classes, traits, operators, and helpers."""',
     '"""Core of sqlglot expressions: base classes, traits, operators and helpers."""'),
    ("docstring_doctest_example", "expected output of a >>> example in a docstring changed",
     "sqlglot/optimizer/simplify.py", "edit",
     "        >>> simplify(expression).sql()\n        'TRUE'",
     "        >>> simplify(expression).sql()\n        'FALSE'"),
    ("docstring_grows", "docstring gets an extra line (shifts every line below)",
     "sqlglot/expressions/core.py", "edit",
     "    is optional or not.\n",
     "    is optional or not.\n    (Extra line added to this docstring.)\n"),
    # --- class/module constant VALUE changes (valuemap.py / valuecheck.py) ---
    ("value_scalar_dialect", "class constant (int) changed on a dialect",
     "sqlglot/dialects/postgres.py", "edit",
     "    INDEX_OFFSET = 1\n    ASCII_ONLY_NORMALIZATION = True",
     "    INDEX_OFFSET = 0\n    ASCII_ONLY_NORMALIZATION = True"),
    ("value_time_mapping", "entry of a class dict that feeds import-time derived tables",
     "sqlglot/dialects/postgres.py", "edit",
     '"HH24": "%H",  # 09', '"HH24": "%I",  # 09'),
    ("value_tokenizer_quotes", "tokenizer list constant changed (derived at import)",
     "sqlglot/dialects/mysql.py", "edit",
     "        QUOTES = [\"'\", '\"']", "        QUOTES = [\"'\"]"),
    ("value_enum_member", "class constant switched to another Enum member",
     "sqlglot/dialects/mysql.py", "edit",
     "    NORMALIZATION_STRATEGY = NormalizationStrategy.CASE_SENSITIVE",
     "    NORMALIZATION_STRATEGY = NormalizationStrategy.CASE_INSENSITIVE"),
    ("value_base_generator_flag", "base Generator class flag changed (inherited by all)",
     "sqlglot/generator.py", "edit",
     '    LIMIT_FETCH = "ALL"', '    LIMIT_FETCH = "LIMIT"'),
    ("value_arg_types", "arg_types of a core expression changed",
     "sqlglot/expressions/core.py", "edit",
     'class Alias(Expression):\n    arg_types = {"this": True, "alias": False}',
     'class Alias(Expression):\n    arg_types = {"this": True, "alias": True}'),
    ("value_module_constant", "module-level constant dict entry changed",
     "sqlglot/dialects/dialect.py", "edit",
     '    "\\\\t": "\\t",\n', '    "\\\\t": "\\v",\n'),
    ("value_reorder_entries", "two entries of a class dict swapped (moved lines)",
     "sqlglot/dialects/postgres.py", "edit",
     "        exp.Distinct: AliasPrecedence.SOURCE,\n        exp.Having: AliasPrecedence.SOURCE,\n",
     "        exp.Having: AliasPrecedence.SOURCE,\n        exp.Distinct: AliasPrecedence.SOURCE,\n"),
    ("value_parameter_token", "string constant on the base Generator changed",
     "sqlglot/generator.py", "edit",
     '    PARAMETER_TOKEN = "@"', '    PARAMETER_TOKEN = "$"'),
]


def read(path):
    with open(path, encoding="utf-8", newline="") as f:
        return f.read()


def make_diff(rel, old, new):
    """Git-style unified diff for one file (old/new may be None)."""
    a = old.splitlines(keepends=True) if old is not None else []
    b = new.splitlines(keepends=True) if new is not None else []
    head = [f"diff --git a/{rel} b/{rel}\n"]
    if old is None:
        head.append("new file mode 100644\n")
    elif new is None:
        head.append("deleted file mode 100644\n")
    head.append("index 0000000..1111111" + ("" if old is None or new is None else " 100644") + "\n")
    lines = list(difflib.unified_diff(a, b, fromfile="/dev/null" if old is None else f"a/{rel}",
                                      tofile="/dev/null" if new is None else f"b/{rel}", n=3))
    # difflib writes "@@ -1,N +1,M @@"; git uses the same format.
    return "".join(head + [ln if ln.endswith("\n") else ln + "\n\\ No newline at end of file\n"
                           for ln in lines])


def main():
    src = sys.argv[1]
    os.makedirs(OUT, exist_ok=True)
    kinds = {}
    for name, kind, rel, op, old_text, new_text in EDITS:
        path = os.path.join(src, rel)
        if op == "new":
            diff = make_diff(rel, None, new_text)
        elif op == "delete":
            diff = make_diff(rel, read(path), None)
        else:
            content = read(path)
            count = content.count(old_text)
            if count != 1:
                raise SystemExit(f"{name}: expected 1 match in {rel}, found {count}")
            diff = make_diff(rel, content, content.replace(old_text, new_text))
        with open(os.path.join(OUT, name + ".patch"), "w", encoding="utf-8", newline="\n") as f:
            f.write(diff)
        kinds[name] = kind
    with open(os.path.join(OUT, "kinds.json"), "w") as f:
        json.dump(kinds, f, indent=1)
    print(f"wrote {len(EDITS)} patches to {OUT}")


if __name__ == "__main__":
    main()
