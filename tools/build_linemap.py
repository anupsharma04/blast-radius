"""
Build the per-test LINE coverage map (Step 3). Run once, locally, inside the
builder image (tools/Dockerfile.build), see tools/build_linemap.sh.

For every test in tests.json, and for 2 different hash seeds, run
`pytest <test id>` in its own fresh process with tools/linecov_plugin.py.
Each run reports which sqlglot lines (inside functions) the test executed
and which fixture files it opened.

Why 2 hash seeds: Python randomises the iteration order of sets of strings per
process, so a test can take slightly different code paths from run to run.
Taking the union of two runs makes the map more complete.

Output (in maps/):
  linemap_index.json   test ids (bit i = i-th test), list of sqlglot .py
                       files that were analysed, fixture -> tests bitmask
  cov/<file>.json.gz   for one sqlglot file: {"sets": [hex bitmask, ...],
                       "lines": [[line, set index], ...]}. Many lines share
                       exactly the same set of tests, so we store each
                       distinct set once and point to it ("interning").
Only lines inside function bodies are kept; changes anywhere else use the
file-level map (maps/deps.json).
"""

import argparse
import gzip
import json
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
import change_analysis  # noqa: E402  (shared with the selector)

TOOLS_DIR = os.path.join(REPO, "tools")
SQLGLOT = "/opt/sqlglot"


def log(*args):
    print(*args, file=sys.stderr, flush=True)


def cov_filename(rel_path):
    return rel_path.replace("/", "__") + ".json.gz"


def run_one(index, test_id, seed, tmp):
    # One directory per run: the main pytest process AND every child process
    # it starts (via sitecustomize.py) each write <pid>.json into it.
    run_dir = os.path.join(tmp, f"{seed}_{index}")
    os.makedirs(run_dir)
    out = os.path.join(run_dir, "outcomes.json")
    env = dict(os.environ, RECORD_OUT=out, LINECOV_DIR=run_dir, LINECOV_ROOT=SQLGLOT,
               PYTHONPATH=os.pathsep.join([os.path.join(TOOLS_DIR, "linecov_site"), TOOLS_DIR]),
               PYTHONHASHSEED=str(seed))
    cmd = [sys.executable, "-m", "pytest", "-p", "linecov_plugin", "-p", "no:cacheprovider",
           "-q", test_id]
    proc = subprocess.run(cmd, cwd=SQLGLOT, env=env, capture_output=True, text=True)
    if not os.path.exists(out):
        log(proc.stdout[-2000:], proc.stderr[-2000:])
        raise SystemExit(f"recording failed for {test_id}")
    with open(out) as f:
        outcomes = json.load(f)
    # Merge every process's record into one.
    lines, opened, n_procs = {}, set(), 0
    for name in os.listdir(run_dir):
        if name == "outcomes.json":
            continue
        n_procs += 1
        with open(os.path.join(run_dir, name)) as f:
            r = json.load(f)
        for path, nums in r["lines"].items():
            lines.setdefault(path, set()).update(nums)
        opened.update(r["opened"])
    shutil.rmtree(run_dir)
    if n_procs == 0:
        raise SystemExit(f"no line records for {test_id} (sitecustomize not loaded?)")
    rec = {"lines": lines, "opened": opened, "outcomes": outcomes, "procs": n_procs}
    return index, test_id, seed, rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tests", default="sample/tests.json")
    ap.add_argument("--out", default="maps")
    ap.add_argument("--seeds", default="0,1")
    ap.add_argument("--jobs", type=int, default=os.cpu_count())
    args = ap.parse_args()

    with open(os.path.join(REPO, args.tests)) as f:
        tests = sorted(json.load(f)["tests"], key=lambda t: t["number"])
    test_ids = [t["id"] for t in tests]
    seeds = [int(s) for s in args.seeds.split(",")]

    # Pre-compile once so the 3000+ short processes don't each recompile sqlglot.
    subprocess.run([sys.executable, "-m", "compileall", "-q", "sqlglot", "tests"],
                   cwd=SQLGLOT, check=True)

    tmp = "/tmp/linecov"
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    jobs = [(i, tid, s) for s in seeds for i, tid in enumerate(test_ids)]

    line_masks = {}      # "sqlglot/x.py" -> {line: bitmask}
    fixture_masks = {}   # "tests/fixtures/..." -> bitmask
    bad = []
    started = time.time()
    with ThreadPoolExecutor(args.jobs) as pool:
        for n, (i, tid, seed, rec) in enumerate(pool.map(lambda j: run_one(*j, tmp), jobs), 1):
            bit = 1 << i
            for path, lines in rec["lines"].items():
                d = line_masks.setdefault(path, {})
                for ln in lines:
                    d[ln] = d.get(ln, 0) | bit
            for path in rec["opened"]:
                fixture_masks[path] = fixture_masks.get(path, 0) | bit
            outcome = rec["outcomes"].get(tid)
            if outcome != "passed":
                bad.append((seed, tid, outcome))
            if n % 200 == 0:
                log(f"  {n}/{len(jobs)} runs, {time.time() - started:.0f}s")

    for seed, tid, outcome in bad:
        log(f"WARNING seed {seed}: {tid} -> {outcome}")

    # Write one compact coverage file per sqlglot source file.
    cov_dir = os.path.join(args.out, "cov")
    os.makedirs(cov_dir, exist_ok=True)
    for name in os.listdir(cov_dir):
        os.remove(os.path.join(cov_dir, name))
    files, kept, total = [], 0, 0
    for dirpath, _, names in os.walk(os.path.join(SQLGLOT, "sqlglot")):
        for name in sorted(names):
            if not name.endswith(".py"):
                continue
            rel = os.path.relpath(os.path.join(dirpath, name), SQLGLOT).replace(os.sep, "/")
            files.append(rel)
            masks = line_masks.get(rel, {})
            if not masks:
                continue
            with open(os.path.join(SQLGLOT, rel), encoding="utf-8") as f:
                body = change_analysis.function_body_lines(change_analysis.ast.parse(f.read()))
            sets, set_index, lines = [], {}, []
            for ln in sorted(masks):
                total += 1
                if ln not in body:
                    continue          # import-time line: file-level map handles it
                m = masks[ln]
                if m not in set_index:
                    set_index[m] = len(sets)
                    sets.append(format(m, "x"))
                lines.append([ln, set_index[m]])
                kept += 1
            with gzip.open(os.path.join(cov_dir, cov_filename(rel)), "wt") as f:
                json.dump({"sets": sets, "lines": lines}, f, separators=(",", ":"))

    index = {
        "commit": "eb11aec4be8cb2122afe713a4816662c3f0fb2fa",
        "seeds": seeds,
        "test_ids": test_ids,
        "files": sorted(files),
        "fixtures": {p: format(m, "x") for p, m in sorted(fixture_masks.items())},
    }
    with open(os.path.join(args.out, "linemap_index.json"), "w") as f:
        json.dump(index, f, separators=(",", ":"))
    log(f"done in {time.time() - started:.0f}s: {len(files)} source files, "
        f"{kept}/{total} covered lines kept, {len(fixture_masks)} fixture files, "
        f"{len(bad)} non-passing runs")


if __name__ == "__main__":
    main()
