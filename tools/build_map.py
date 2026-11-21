"""
Build the file-level dependency map (Step 2). Run ONCE, locally, inside the
scorer's Docker image with network access. See tools/build_map.sh.

What it does:
  1. Downloads sqlglot at the pinned commit (a GitHub tarball, no git needed).
  2. For every test file listed in tests.json, runs `pytest <file>` in its own
     fresh Python process with tools/record_plugin.py, which records every
     sqlglot file that run used (imported modules + opened fixture files).
  3. Inverts that into: changed file -> test files that depend on it,
     and writes it to maps/deps.json (committed to the repo).

A fresh process per test file matters: Python keeps imported modules in
memory, so if all files ran in one process we couldn't tell which test file
caused which import.
"""

import argparse
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from concurrent.futures import ThreadPoolExecutor

COMMIT = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
TARBALL = f"https://github.com/tobymao/sqlglot/archive/{COMMIT}.tar.gz"
TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))


def log(*args):
    print(*args, file=sys.stderr, flush=True)


def download_sqlglot(dest):
    """Download and unpack sqlglot; return the path of the checkout."""
    log("downloading", TARBALL)
    data = urllib.request.urlopen(TARBALL).read()
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        tar.extractall(dest, filter="data")
    return os.path.join(dest, f"sqlglot-{COMMIT}")


def record_one(root, test_file, out_dir):
    """Run one test file under pytest with the recorder; return its record."""
    out_path = os.path.join(out_dir, test_file.replace("/", "__") + ".json")
    env = dict(os.environ)
    env["RECORD_OUT"] = out_path
    # So pytest can find record_plugin.py via `-p record_plugin`.
    env["PYTHONPATH"] = TOOLS_DIR
    # Don't let leftover .pyc files skip the source-file opens we record.
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    cmd = [sys.executable, "-m", "pytest", "-p", "record_plugin",
           "-p", "no:cacheprovider", "-q", test_file]
    proc = subprocess.run(cmd, cwd=root, env=env, capture_output=True, text=True)
    if not os.path.exists(out_path):
        # The plugin never wrote output: pytest crashed. Show why and stop.
        log(proc.stdout[-3000:], proc.stderr[-3000:])
        raise SystemExit(f"recording failed for {test_file}")
    with open(out_path) as f:
        return json.load(f)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tests", default="sample/tests.json")
    parser.add_argument("--out", default="maps/deps.json")
    parser.add_argument("--jobs", type=int, default=4)
    args = parser.parse_args()

    with open(args.tests) as f:
        tests = json.load(f)["tests"]
    test_files = sorted({t["file"] for t in tests})
    expected_ids = {t["id"] for t in tests}

    work = tempfile.mkdtemp()
    root = download_sqlglot(work)
    out_dir = os.path.join(work, "records")
    os.makedirs(out_dir)

    # Run several test files at once (each is still its own process).
    with ThreadPoolExecutor(args.jobs) as pool:
        records = dict(zip(test_files, pool.map(
            lambda tf: record_one(root, tf, out_dir), test_files)))

    # Sanity checks: did we run the same tests as tests.json, and did they
    # pass on the unmodified code? (They should; a patch is what breaks them.)
    seen_ids = set()
    for test_file, rec in records.items():
        seen_ids.update(rec["outcomes"])
        bad = [t for t, o in rec["outcomes"].items() if o == "failed"]
        if bad:
            log(f"WARNING {test_file}: {len(bad)} tests fail at baseline, e.g. {bad[:3]}")
    missing = expected_ids - seen_ids
    if missing:
        log(f"WARNING {len(missing)} tests from tests.json never ran, e.g. {sorted(missing)[:3]}")

    # Invert: test file -> deps  becomes  dep -> test files.
    dep_to_tests = {}
    for test_file, rec in records.items():
        for dep in rec["deps"]:
            dep_to_tests.setdefault(dep, set()).add(test_file)
        # A test file always depends on itself.
        dep_to_tests.setdefault(test_file, set()).add(test_file)

    result = {
        "commit": COMMIT,
        "test_files": test_files,
        "deps": {dep: sorted(tfs) for dep, tfs in sorted(dep_to_tests.items())},
    }
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(result, f, indent=0)
    log(f"wrote {args.out}: {len(dep_to_tests)} files mapped, "
        f"{len(seen_ids)} tests ran across {len(test_files)} test files")


if __name__ == "__main__":
    main()
