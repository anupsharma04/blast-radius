"""
Safety harness: does our selector ever skip a test that really fails?

For each patch in a folder (default tools/fake_patches/):
  1. Copy a clean sqlglot checkout (pinned commit), apply the patch with
     `git apply`, run the whole test suite with pytest -> the tests that
     REALLY fail (ground truth). A test counts as failing if pytest reports
     failure/error/timeout, or if it never ran (e.g. its file can't import).
  2. Run our selector.py on the same patch.
  3. Report every failing test the selector did NOT pick (a "miss").

Runs inside the builder image (tools/Dockerfile.build), see tools/validate.sh.
The fake patches are for TESTING ONLY; nothing here feeds the selector.
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = os.path.join(REPO, "tools")
SQLGLOT = "/opt/sqlglot"        # clean checkout in the builder image
WORK = "/tmp/validate"


def log(*args):
    print(*args, file=sys.stderr, flush=True)


def ground_truth(name, patch_path, test_files, all_ids, workers, timeout):
    """Apply the patch to a fresh copy and return (failing ids, note)."""
    tree = os.path.join(WORK, "trees", name)
    shutil.copytree(SQLGLOT, tree, symlinks=True)
    note = ""
    if patch_path:
        r = subprocess.run(["git", "apply", "--whitespace=nowarn", patch_path], cwd=tree,
                           capture_output=True, text=True)
        if r.returncode != 0:
            shutil.rmtree(tree)
            return None, "git apply failed: " + r.stderr.strip()[:200]
    out = os.path.join(WORK, f"{name}.outcomes.json")
    env = dict(os.environ, OUTCOME_OUT=out, PYTHONPATH=TOOLS)
    # Only run test files that still exist (a patch may delete one; its tests
    # then count as failing because they never run).
    files = [f for f in test_files if os.path.exists(os.path.join(tree, f))]
    cmd = [sys.executable, "-m", "pytest", "-p", "outcome_plugin", "-p", "no:cacheprovider",
           "-q", "-n", str(workers), f"--timeout={timeout}", *files]
    r = subprocess.run(cmd, cwd=tree, env=env, capture_output=True, text=True)
    shutil.rmtree(tree)
    if not os.path.exists(out):
        return set(all_ids), "pytest crashed: every test counts as failing"
    with open(out) as f:
        rec = json.load(f)
    outcomes = rec["outcomes"]
    # A worker killed by the machine (e.g. out of memory) is a broken MEASUREMENT,
    # not a test failure: refuse the result instead of counting its tests
    # as failing (seen once with a 3 GB memory cap: "539 failing" for a
    # harmless edit).
    output = r.stdout + r.stderr
    if "crashed" in output or "MemoryError" in output:
        return None, "INVALID ground truth: a test worker crashed (out of memory?)"
    bad_files = {e.split("::")[0] for e in rec["collect_errors"]}
    unexplained = [tid for tid in all_ids if tid not in outcomes
                   and tid.split("::")[0] not in bad_files
                   and os.path.exists(os.path.join(SQLGLOT, tid.split("::")[0]))
                   and tid.split("::")[0] in files]
    if len(unexplained) > 5:
        return None, f"INVALID ground truth: {len(unexplained)} tests never reported"
    failing = {tid for tid in all_ids if outcomes.get(tid) in (None, "failed")}
    if rec["collect_errors"]:
        note = "collection errors: " + ", ".join(rec["collect_errors"])[:150]
    return failing, note


def run_selector(patch_paths, tests_json):
    """Run selector.py on all patches at once; return {name: set of test numbers}."""
    data = os.path.join(WORK, "data")
    shutil.rmtree(data, ignore_errors=True)
    os.makedirs(os.path.join(data, "commits"))
    shutil.copy(tests_json, os.path.join(data, "tests.json"))
    for name, path in patch_paths.items():
        shutil.copy(path, os.path.join(data, "commits", name + ".patch"))
    # Fingerprints of the clean checkout for the constant-value check (the
    # docker image makes the same file at build time; see Dockerfile).
    baseline = os.path.join(WORK, "baseline.json.gz")
    if not os.path.exists(baseline):
        subprocess.run([sys.executable, os.path.join(REPO, "valuecheck.py"), "baseline",
                        SQLGLOT, baseline], check=True)
    env = dict(os.environ, BLAST_DATA=data, BLAST_SRC=SQLGLOT, BLAST_BASELINE=baseline)
    t0 = time.process_time()
    r = subprocess.run([sys.executable, os.path.join(REPO, "selector.py")], env=env,
                       capture_output=True, text=True, check=True)
    log(r.stderr)
    selected = {}
    for line in r.stdout.splitlines():
        rec = json.loads(line)
        selected[rec["commit"]] = {n for w in rec["workers"] for n in w}
    return selected


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--patches", default=os.path.join(TOOLS, "fake_patches"))
    ap.add_argument("--tests", default=os.path.join(REPO, "sample", "tests.json"))
    ap.add_argument("--parallel", type=int, default=2, help="patches tested at once")
    ap.add_argument("--workers", type=int, default=4, help="pytest-xdist workers per patch")
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("--out", default=os.path.join(TOOLS, "validate_results.json"))
    ap.add_argument("--cache", default=os.path.join(TOOLS, ".validate_cache.json"))
    ap.add_argument("--no-cache", action="store_true", help="recompute all ground truth")
    args = ap.parse_args()

    with open(args.tests) as f:
        tests = json.load(f)["tests"]
    id_to_num = {t["id"]: t["number"] for t in tests}
    all_ids = list(id_to_num)
    test_files = sorted({t["file"] for t in tests})

    kinds = {}
    kinds_path = os.path.join(args.patches, "kinds.json")
    if os.path.exists(kinds_path):
        with open(kinds_path) as f:
            kinds = json.load(f)
    patches = {fn[:-6]: os.path.join(args.patches, fn)
               for fn in sorted(os.listdir(args.patches)) if fn.endswith(".patch")}

    shutil.rmtree(WORK, ignore_errors=True)
    os.makedirs(os.path.join(WORK, "trees"))
    subprocess.run([sys.executable, "-m", "compileall", "-q", "sqlglot", "tests"], cwd=SQLGLOT)

    selected = run_selector(patches, args.tests)

    # Ground truth depends only on (patch text, tests.json, pinned sqlglot), so
    # cache it between runs of the harness. The cache lives outside git
    # (.gitignore) and is never read by the selector.
    with open(args.tests, "rb") as f:
        tests_hash = hashlib.sha256(f.read()).hexdigest()[:16]
    cache = {}
    if os.path.exists(args.cache) and not args.no_cache:
        with open(args.cache) as f:
            cache = json.load(f)

    def key_of(path):
        with open(path, "rb") as f:
            return tests_hash + ":" + hashlib.sha256(f.read()).hexdigest()

    # Baseline: the unpatched code must pass, otherwise ground truth is noisy.
    base_key = tests_hash + ":baseline"
    if base_key in cache:
        base_fail = set(cache[base_key]["failing"])
    else:
        base_fail, _ = ground_truth("_baseline", None, test_files, all_ids,
                                    args.workers, args.timeout)
        cache[base_key] = {"failing": sorted(base_fail), "note": ""}
    if base_fail:
        log(f"WARNING baseline failures (ignored below): {sorted(base_fail)[:5]}")

    def job(name):
        k = key_of(patches[name])
        if k in cache:
            c = cache[k]
            return name, (None if c["failing"] is None else set(c["failing"])), c["note"]
        t0 = time.time()
        failing, note = ground_truth(name, patches[name], test_files, all_ids,
                                     args.workers, args.timeout)
        log(f"  {name}: {len(failing) if failing is not None else '?'} failing "
            f"({time.time() - t0:.0f}s) {note}")
        if not note.startswith("INVALID"):         # never cache a broken measurement
            cache[k] = {"failing": None if failing is None else sorted(failing), "note": note}
        return name, failing, note

    results, total_miss = [], 0
    with ThreadPoolExecutor(args.parallel) as pool:
        for name, failing, note in pool.map(job, patches):
            if failing is None:
                results.append({"patch": name, "kind": kinds.get(name, ""), "error": note})
                continue
            failing -= base_fail
            fail_nums = {id_to_num[t] for t in failing}
            missed = sorted(t for t in failing if id_to_num[t] not in selected[name])
            total_miss += bool(missed)
            results.append({"patch": name, "kind": kinds.get(name, ""),
                            "selected": len(selected[name]), "failing": len(fail_nums),
                            "missed": missed, "note": note})

    with open(args.cache, "w") as f:
        json.dump(cache, f)
    with open(args.out, "w") as f:
        json.dump(results, f, indent=1)

    print(f"| patch | kind of change | tests selected | tests that failed | miss |")
    print(f"|---|---|---|---|---|")
    for r in results:
        if "error" in r:
            print(f"| {r['patch']} | {r['kind']} | - | - | ERROR: {r['error']} |")
            continue
        miss = f"**YES ({len(r['missed'])})**" if r["missed"] else "no"
        print(f"| {r['patch']} | {r['kind']} | {r['selected']} | {r['failing']} | {miss} |")
    print(f"\npatches with a miss: {total_miss} / {len(results)}")
    for r in results:
        if r.get("missed"):
            print(f"  {r['patch']} missed: {r['missed'][:8]}")


if __name__ == "__main__":
    main()
