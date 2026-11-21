"""
EVALUATION ONLY: where does our selector still spend a lot?

Generates N random one-line mutations of sqlglot (operator flips, constant
changes, True/False flips, comment/docstring edits, at module, class and
function level), runs selector.py on all of them, and reports the cost per
kind of change and per selector decision ("file level (...)", "ALL", value
check, ...). No ground truth here (that's validate.py); this only shows which
kinds of change still fall back to broad selections.

Nothing in the selector or its maps is built from this.

Usage (inside the builder image):
  python tools/explore_mutations.py --n 400 --seed 1
"""

import argparse
import ast
import collections
import difflib
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tokenize
import io

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SQLGLOT = "/opt/sqlglot"
WORK = "/tmp/explore"

SWAPS = [("==", "!="), ("!=", "=="), (" is not ", " is "), (" is ", " is not "),
         (" and ", " or "), (" or ", " and "), ("<=", "<"), (">=", ">"), (" < ", " <= "),
         (" > ", " >= "), (" + ", " - "), (" - ", " + "), ("True", "False"), ("False", "True"),
         (" not ", " "), ("None", "0")]


def level_of(tree, line):
    """'function', 'class' or 'module' for a line."""
    best = "module"
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and \
                node.body[0].lineno <= line <= node.end_lineno:
            return "function"
        if isinstance(node, ast.ClassDef) and node.lineno <= line <= node.end_lineno:
            best = "class"
    return best


def mutate(path, rng):
    with open(path, encoding="utf-8") as f:
        text = f.read()
    lines = text.splitlines(keepends=True)
    tree = ast.parse(text)
    for _ in range(50):
        i = rng.randrange(len(lines))
        line = lines[i]
        s = line.strip()
        if not s or s.startswith(("import ", "from ", "@")):
            continue
        r = rng.random()
        if s.startswith("#") or r < 0.08:
            indent = line[: len(line) - len(line.lstrip())]
            new = indent + "# note: reviewed\n" + line
            kind = "comment"
        elif r < 0.30:
            nums = list(re.finditer(r"(?<![\w.])(\d+)(?![\w.])", line))
            strs = list(re.finditer(r'"([A-Za-z_ ]{1,20})"', line))
            if nums and (not strs or rng.random() < 0.5):
                m = rng.choice(nums)
                new = line[:m.start()] + str(int(m.group(1)) + 1) + line[m.end():]
                kind = "number"
            elif strs:
                m = rng.choice(strs)
                new = line[:m.start(1)] + m.group(1) + "X" + line[m.end(1):]
                kind = "string"
            else:
                continue
        else:
            options = [(a, b) for a, b in SWAPS if a in line]
            if not options:
                continue
            a, b = rng.choice(options)
            idx = [m.start() for m in re.finditer(re.escape(a), line)]
            k = rng.choice(idx)
            new = line[:k] + b + line[k + len(a):]
            kind = "operator"
        if new == line:
            continue
        new_text = "".join(lines[:i]) + new + "".join(lines[i + 1:])
        try:
            ast.parse(new_text)
        except SyntaxError:
            continue
        # comment-only edits that land inside a docstring/string are fine too
        return kind, level_of(tree, i + 1), text, new_text
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=400)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--tests", default=os.path.join(REPO, "sample", "tests.json"))
    args = ap.parse_args()
    rng = random.Random(args.seed)
    files = []
    for dp, dn, fn in os.walk(os.path.join(SQLGLOT, "sqlglot")):
        dn[:] = [d for d in dn if d != "__pycache__"]
        for f in fn:
            if f.endswith(".py") and f not in ("__init__.py", "__main__.py"):
                files.append(os.path.join(dp, f))
    files.sort()
    # weight files by size (a random line of the codebase)
    sizes = [os.path.getsize(f) for f in files]

    shutil.rmtree(WORK, ignore_errors=True)
    data = os.path.join(WORK, "data")
    os.makedirs(os.path.join(data, "commits"))
    shutil.copy(args.tests, os.path.join(data, "tests.json"))
    meta = {}
    while len(meta) < args.n:
        path = rng.choices(files, sizes)[0]
        got = mutate(path, rng)
        if not got:
            continue
        kind, level, old, new = got
        rel = os.path.relpath(path, SQLGLOT)
        diff = "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True),
                                            "a/" + rel, "b/" + rel))
        name = f"m{len(meta):04d}"
        with open(os.path.join(data, "commits", name + ".patch"), "w") as f:
            f.write(f"diff --git a/{rel} b/{rel}\n" + diff)
        meta[name] = {"kind": kind, "level": level, "file": rel}

    baseline = os.path.join(WORK, "baseline.json.gz")
    subprocess.run([sys.executable, os.path.join(REPO, "valuecheck.py"), "baseline", SQLGLOT,
                    baseline], check=True, capture_output=True)
    env = dict(os.environ, BLAST_DATA=data, BLAST_SRC=SQLGLOT, BLAST_BASELINE=baseline)
    r = subprocess.run([sys.executable, os.path.join(REPO, "selector.py")], env=env,
                       capture_output=True, text=True, check=True)
    # Decision per commit from the log (file lines come before "<id>: N tests").
    reason, pending = {}, []
    for line in r.stderr.splitlines():
        m = re.match(r"^(m\d{4}): (ALL tests|\d+ tests)", line)
        if m:
            reason[m.group(1)] = "; ".join(pending) or ("ALL" if "ALL" in m.group(2) else "line/value")
            pending = []
        elif "file level" in line or "value check" in line or "docstring" in line or "patch does not" in line:
            pending.append(re.sub(r"^\s*\S+: ", "", line.strip())[:70])
    with open(args.tests) as f:
        ms = {t["number"]: round(t["seconds"] * 1000) for t in json.load(f)["tests"]}
    cost = {}
    for line in r.stdout.splitlines():
        rec = json.loads(line)
        w = rec["workers"]
        cost[rec["commit"]] = (max(sum(ms[n] for n in k) for k in w) + 2000 * len(w)) / 1000 if w else 0
    by = collections.defaultdict(list)
    for name, m in meta.items():
        by[(m["kind"], m["level"])].append(cost[name])
    print("kind      level     count  avg cost  share of total")
    total = sum(cost.values())
    for k, v in sorted(by.items(), key=lambda kv: -sum(kv[1])):
        print(f"{k[0]:9s} {k[1]:9s} {len(v):5d}  {sum(v)/len(v):7.1f}  {100*sum(v)/total:5.1f}%")
    print(f"total {total:.0f} s over {len(cost)} mutations, avg {total/len(cost):.1f} s")
    why = collections.defaultdict(list)
    for name in meta:
        rs = reason.get(name, "?")
        rs = re.sub(r"\(.*?:.*", "(...)", rs) if "untrackable" not in rs else re.sub(r"\[.*", "", rs)
        why[rs].append(cost[name])
    print("\ndecision                                                     count  avg cost  share")
    for k, v in sorted(why.items(), key=lambda kv: -sum(kv[1]))[:25]:
        print(f"{k[:60]:60s} {len(v):5d}  {sum(v)/len(v):7.1f}  {100*sum(v)/total:5.1f}%")
    with open(os.path.join(REPO, "tools", "explore_last.json"), "w") as f:
        json.dump({n: dict(meta[n], cost=cost[n], reason=reason.get(n)) for n in meta}, f, indent=0)


if __name__ == "__main__":
    main()
