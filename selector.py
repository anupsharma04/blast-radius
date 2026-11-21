"""
Blast Radius selector - version 2 (file-level dependency map)

For each patch:
  1. Find which files it changes.
  2. Look each one up in maps/deps.json (built once by tools/build_map.py),
     which says which TEST FILES used that file when the tests ran
     (imported it, even lazily, or opened it as a fixture).
  3. Run every test in those test files. If anything about the patch is
     unusual or unknown, run ALL tests instead: a missed failing test costs
     300 s, far more than running everything (~29 s).
  4. Split the chosen tests across the best number of workers (as in v1).

Logs go to stderr; stdout is only the JSON answer lines.
"""
import heapq
import json
import os
import sys

DATA = "/data"
MAP_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "maps", "deps.json")

# File names that affect how many tests run/import, so any change -> run all.
RISKY_NAMES = {"__init__.py", "conftest.py"}

# Diff header lines that mean "not a simple edit of an existing file".
RISKY_HEADERS = ("new file mode", "deleted file mode", "rename from", "rename to",
                 "copy from", "copy to", "old mode", "new mode", "Binary files",
                 "GIT binary patch")


def log(*args):
    print(*args, file=sys.stderr)


def load_tests():
    # tests.json -> list of (file, duration in ms, test number)
    with open(os.path.join(DATA, "tests.json")) as f:
        tests = json.load(f)["tests"]
    return [(t["file"], round(t["seconds"] * 1000), t["number"]) for t in tests]


def split_into_workers(tests, k):
    # Greedy balancing: take the longest test first and give it to
    # whichever worker currently has the least total time.
    heap = [(0, i) for i in range(k)]          # (load in ms, worker index)
    workers = [[] for _ in range(k)]
    for ms, num in sorted(tests, reverse=True):
        load, i = heapq.heappop(heap)
        workers[i].append(num)
        heapq.heappush(heap, (load + ms, i))
    max_load = max(load for load, _ in heap)
    return workers, max_load


def best_split(tests):
    # tests: list of (ms, number). Try 1..8 workers and keep the cheapest:
    # cost = slowest worker + 2 seconds (2000 ms) per worker
    if not tests:
        return []                               # running nothing costs 0
    best_cost, best_workers = None, []
    for k in range(1, min(8, len(tests)) + 1):
        workers, max_load = split_into_workers(tests, k)
        cost = max_load + 2000 * k
        if best_cost is None or cost < best_cost:
            best_cost, best_workers = cost, workers
    return [w for w in best_workers if w]


def changed_paths(patch_text):
    """Return (set of repo paths the patch touches, risky flag).
    risky=True means: don't trust the map, run everything."""
    paths, risky = set(), False
    for line in patch_text.splitlines():
        if line.startswith("diff --git "):
            # "diff --git a/X b/Y": record both sides (they differ on renames).
            parts = line.split()
            if len(parts) != 4:
                risky = True                    # e.g. spaces in names: be safe
            for p in parts[2:]:
                paths.add(p[2:] if p[:2] in ("a/", "b/") else p)
        elif line.startswith(("--- ", "+++ ")):
            p = line[4:].split("\t")[0].strip()
            if p == "/dev/null":
                risky = True                    # file created or deleted
            else:
                paths.add(p[2:] if p[:2] in ("a/", "b/") else p)
        elif line.startswith(RISKY_HEADERS):
            risky = True
    if not paths:
        risky = True                            # couldn't understand the patch
    return paths, risky


def affected_test_files(paths, deps):
    """Return the set of test files to run, or None meaning 'run all'."""
    selected = set()
    for path in paths:
        name = path.rsplit("/", 1)[-1]
        if name in RISKY_NAMES:
            return None
        # Only source and test files are in the map; anything else
        # (pyproject.toml, setup.py, Makefile, CI config, ...) -> run all.
        if not path.startswith(("sqlglot/", "tests/")):
            return None
        if path not in deps:
            return None                         # unknown file: be safe
        selected.update(deps[path])
    return selected


def main():
    tests = load_tests()
    all_tests = [(ms, num) for _, ms, num in tests]
    with open(MAP_PATH) as f:
        dep_map = json.load(f)
    deps = dep_map["deps"]
    mapped_files = set(dep_map["test_files"])

    # Tests in files the map has never seen get run every time (extra safety
    # in case tests.json ever lists a test file we did not record).
    always = [(ms, num) for file, ms, num in tests if file not in mapped_files]

    commits_dir = os.path.join(DATA, "commits")
    commits = sorted(f[:-6] for f in os.listdir(commits_dir) if f.endswith(".patch"))

    all_split = best_split(all_tests)           # reused for every "run all"
    out = []
    n_all = 0
    for c in commits:
        try:
            with open(os.path.join(commits_dir, c + ".patch"), encoding="utf-8",
                      errors="replace") as f:
                paths, risky = changed_paths(f.read())
            files = None if risky else affected_test_files(paths, deps)
        except Exception as e:                  # never crash: fall back to all
            log(f"{c}: error {e!r}, running all")
            files = None

        if files is None:
            workers = all_split
            n_all += 1
        else:
            chosen = [(ms, num) for file, ms, num in tests if file in files]
            workers = best_split(chosen + always)
        out.append(json.dumps({"commit": c, "workers": workers}, separators=(",", ":")))

    log(f"{len(commits)} commits, {len(tests)} tests, {n_all} ran everything")
    sys.stdout.write("\n".join(out) + "\n")


if __name__ == "__main__":
    main()
