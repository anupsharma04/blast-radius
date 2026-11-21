"""
Blast Radius selector - version 3 (line-level coverage + file-level fallback)

For each patch, for each changed file:
  * sqlglot/*.py  -> line level (change_analysis.py): only tests that ran
                     the changed statements/functions. If the change is
                     risky (outside function bodies, comment-only, syntax
                     error, ...) -> file level.
  * fixture files -> tests that opened that fixture (per-test record).
  * otherwise     -> file level: every test in the test files that used the
                     file (maps/deps.json, Step 2).
  * anything unknown or odd (new/deleted/renamed/binary files, __init__.py,
    conftest.py, config files, patch doesn't apply) -> ALL tests.
A missed failing test costs 300 s, so every doubt resolves to running more.

Then the chosen tests are split across the best number of workers (as v1).
Logs go to stderr; stdout is only the JSON answer lines.
"""
import gzip
import heapq
import json
import os
import re
import subprocess
import sys

APP = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, APP)
import change_analysis as ca  # noqa: E402

DATA = os.environ.get("BLAST_DATA", "/data")
MAPS = os.path.join(APP, "maps")
# Original sqlglot sources at the pinned commit (downloaded in the Dockerfile).
SRC = os.environ.get("BLAST_SRC", os.path.join(APP, "src"))

RISKY_NAMES = {"__init__.py", "conftest.py"}
RISKY_HEADERS = ("new file mode", "deleted file mode", "rename from", "rename to",
                 "copy from", "copy to", "old mode", "new mode", "Binary files",
                 "GIT binary patch", "similarity index")
HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")

RUN_ALL = None   # marker meaning "run every test"
# Fingerprints of the original sqlglot's class constants, made by
# `valuecheck.py baseline` during the docker build (see Dockerfile).
BASELINE = os.environ.get("BLAST_BASELINE", os.path.join(APP, "baseline.json.gz"))


class ValueOnly:
    """A changed file where only class/module constant VALUES changed."""

    def __init__(self, path, new_text, observers):
        self.path, self.new_text, self.observers = path, new_text, observers


def log(*args):
    print(*args, file=sys.stderr)


# ---------------------------------------------------------------- patches

def parse_patch(text):
    """Split a unified diff into files. Returns a list of
    {"path": str, "hunks": [(old_start, old_count, [lines])]},
    or RUN_ALL if anything looks unusual."""
    lines = text.splitlines()
    files, i = [], 0
    while i < len(lines):
        if not lines[i].startswith("diff --git "):
            if lines[i].strip():
                return RUN_ALL                      # text we don't understand
            i += 1
            continue
        i += 1
        old = new = None
        # Header lines until the first hunk or the next file.
        while i < len(lines) and not lines[i].startswith(("@@", "diff --git ")):
            line = lines[i]
            if line.startswith(RISKY_HEADERS):
                return RUN_ALL
            if line.startswith("--- "):
                old = line[4:].split("\t")[0]
            elif line.startswith("+++ "):
                new = line[4:].split("\t")[0]
            i += 1
        if old is None or new is None or "/dev/null" in (old, new):
            return RUN_ALL                          # created/deleted/mode-only
        if not (old.startswith("a/") and new.startswith("b/")) or old[2:] != new[2:]:
            return RUN_ALL                          # renamed or odd paths
        hunks = []
        while i < len(lines) and lines[i].startswith("@@"):
            m = HUNK_RE.match(lines[i])
            if not m:
                return RUN_ALL
            old_start, old_count = int(m.group(1)), int(m.group(2) or 1)
            new_count = int(m.group(4) or 1)
            i += 1
            body, o, n = [], old_count, new_count
            # Use the hunk's line counts to know where it ends (a removed
            # SQL comment "-- x" appears as "--- x", so we can't guess).
            while o > 0 or n > 0:
                if i >= len(lines):
                    return RUN_ALL
                line = lines[i]
                tag = line[:1]
                if tag == " " or line == "":
                    o, n = o - 1, n - 1
                    line = " " + line[1:]
                elif tag == "-":
                    o -= 1
                elif tag == "+":
                    n -= 1
                elif tag != "\\":
                    return RUN_ALL
                body.append(line)
                i += 1
            while i < len(lines) and lines[i].startswith("\\"):
                i += 1                              # "\ No newline at end of file"
            hunks.append((old_start, old_count, body))
        if not hunks:
            return RUN_ALL
        files.append({"path": new[2:], "hunks": hunks})
    return files or RUN_ALL


# ---------------------------------------------------------------- workers

def split_into_workers(ordered, k):
    # Greedy balancing: longest test first (`ordered` is already sorted
    # longest-first), each to the least-loaded worker.
    heap = [(0, i) for i in range(k)]          # (load in ms, worker index)
    workers = [[] for _ in range(k)]
    for ms, num in ordered:
        load, i = heapq.heappop(heap)
        workers[i].append(num)
        heapq.heappush(heap, (load + ms, i))
    return workers, max(load for load, _ in heap)


def best_split(tests):
    # Try 1..8 workers; cost = slowest worker + 2000 ms per worker.
    if not tests:
        return []
    ordered = sorted(tests, reverse=True)      # sort once, reuse for every k
    longest = ordered[0][0]
    best_cost, best_workers = None, []
    for k in range(1, min(8, len(tests)) + 1):
        # The slowest worker is at least the longest test, so k workers cost
        # at least longest + 2000*k. Once that can't beat the best, more
        # workers can't either: stop early.
        if best_cost is not None and longest + 2000 * k >= best_cost:
            break
        workers, max_load = split_into_workers(ordered, k)
        cost = max_load + 2000 * k
        if best_cost is None or cost < best_cost:
            best_cost, best_workers = cost, workers
    return [w for w in best_workers if w]


# ---------------------------------------------------------------- selector

class Selector:
    def __init__(self):
        with open(os.path.join(DATA, "tests.json")) as f:
            tests = json.load(f)["tests"]
        self.ms = {t["number"]: round(t["seconds"] * 1000) for t in tests}
        self.all_numbers = set(self.ms)
        number_of_id = {t["id"]: t["number"] for t in tests}

        with open(os.path.join(MAPS, "deps.json")) as f:
            dep_map = json.load(f)
        self.deps = dep_map["deps"]
        tests_by_file = {}
        for t in tests:
            tests_by_file.setdefault(t["file"], set()).add(t["number"])
        self.tests_by_file = tests_by_file

        with open(os.path.join(MAPS, "linemap_index.json")) as f:
            idx = json.load(f)
        # bit i of a coverage mask = idx["test_ids"][i] -> test number here
        self.bit_to_number = [number_of_id.get(tid) for tid in idx["test_ids"]]
        self.line_files = set(idx["files"])
        self.fixtures = idx["fixtures"]
        self.observers = idx["observers"]

        # Safety net: tests the maps have never seen are always run.
        known = set(idx["test_ids"]) & set(t["id"] for t in tests
                                         if t["file"] in dep_map["test_files"])
        self.always = {number_of_id[tid] for tid in number_of_id if tid not in known}

        self._sources, self._cov = {}, {}
        self._attrs = None

    def numbers_from_mask(self, mask_hex_or_int):
        m = int(mask_hex_or_int, 16) if isinstance(mask_hex_or_int, str) else mask_hex_or_int
        out, i = set(), 0
        while m:
            if m & 1:
                n = self.bit_to_number[i]
                if n is not None:
                    out.add(n)
            m >>= 1
            i += 1
        return out

    def file_level(self, path):
        """Step 2: all tests in test files that used `path` (None = all)."""
        if path not in self.deps:
            return RUN_ALL
        out = set()
        for tf in self.deps[path]:
            out |= self.tests_by_file.get(tf, set())
        return out

    def source_info(self, path):
        if path not in self._sources:
            with open(os.path.join(SRC, path), encoding="utf-8") as f:
                self._sources[path] = ca.SourceInfo(f.read(), path)
        return self._sources[path]

    def coverage(self, path):
        if path not in self._cov:
            cov = {}
            cov_path = os.path.join(MAPS, "cov", path.replace("/", "__") + ".json.gz")
            if os.path.exists(cov_path):       # no file = no test ran its functions
                with gzip.open(cov_path, "rt") as f:
                    data = json.load(f)
                sets = [int(s, 16) for s in data["sets"]]
                cov = {ln: sets[k] for ln, k in data["lines"]}
            self._cov[path] = cov
        return self._cov[path]

    def tests_for_file(self, fd):
        """Tests to run because of one changed file (RUN_ALL = everything)."""
        path = fd["path"]
        name = path.rsplit("/", 1)[-1]
        if name in RISKY_NAMES or not path.startswith(("sqlglot/", "tests/")):
            return RUN_ALL
        if path not in self.deps:
            return RUN_ALL                         # unknown file
        if path.startswith("sqlglot/") and path.endswith(".py") and path in self.line_files:
            # Tests that read this file's text or code objects can notice ANY
            # edit (even a comment shifting line numbers): always include them.
            observers = self.numbers_from_mask(self.observers.get(path, 0))
            try:
                mask = ca.affected_mask(self.source_info(path), fd["hunks"], self.coverage(path))
                return self.numbers_from_mask(mask) | observers
            except ca.ValueChange as e:
                return ValueOnly(path, e.new_text, observers)   # decided in select()
            except ca.FileLevel as e:
                log(f"    {path}: file level ({e})")
                fl = self.file_level(path)
                return RUN_ALL if fl is RUN_ALL else fl | observers
            except ca.PatchError as e:
                log(f"    {path}: patch does not apply ({e}) -> all")
                return RUN_ALL
        if path in self.fixtures:
            return self.numbers_from_mask(self.fixtures[path])
        return self.file_level(path)

    def attrs(self):
        """Class constant -> tests that read it (maps/attrs.json.gz)."""
        if self._attrs is None:
            with gzip.open(os.path.join(MAPS, "attrs.json.gz"), "rt") as f:
                data = json.load(f)
            self._attrs = (data["sets"], data["pairs"], set(data["tracked"]))
        return self._attrs

    def value_tests(self, files):
        """Tests that read a class constant the patch really changed, or
        None if that can't be decided safely (-> file level)."""
        if not os.path.exists(BASELINE):
            return None
        try:
            p = subprocess.run(
                [sys.executable, "-s", os.path.join(APP, "valuecheck.py"), "check", SRC, BASELINE],
                input=json.dumps(files), capture_output=True, text=True, timeout=60,
                env=dict(os.environ, PYTHONHASHSEED="0", PYTHONDONTWRITEBYTECODE="1"))
            res = json.loads(p.stdout)
        except Exception as e:                      # noqa: BLE001
            log(f"    value check failed: {e!r}")
            return None
        if not res.get("ok"):
            log(f"    value check: {res.get('why')}")
            return None
        sets, pairs, tracked = self.attrs()
        mask = 0
        for key in res["changed"]:
            name = key[2:]                          # "A|module:Class|ATTR"
            if not key.startswith("A|") or name not in tracked:
                log(f"    value check: {key} is not tracked")
                return None
            if name in pairs:                       # absent = no test read it
                mask |= int(sets[pairs[name]], 16)
        log(f"    value check: changed {[k[2:] for k in res['changed']]}")
        return self.numbers_from_mask(mask)

    def select(self, patch_text):
        files = parse_patch(patch_text)
        if files is RUN_ALL:
            return RUN_ALL
        chosen = set(self.always)
        value_only = []
        for fd in files:
            t = self.tests_for_file(fd)
            if t is RUN_ALL:
                return RUN_ALL
            if isinstance(t, ValueOnly):
                value_only.append(t)
                continue
            chosen |= t
        if value_only:
            # One check for all such files of this commit together.
            for v in value_only:
                chosen |= v.observers
            found = self.value_tests({v.path: v.new_text for v in value_only})
            if found is None:
                for v in value_only:
                    log(f"    {v.path}: file level (constant values changed)")
                    fl = self.file_level(v.path)
                    if fl is RUN_ALL:
                        return RUN_ALL
                    chosen |= fl
            else:
                chosen |= found
        return chosen


def main():
    sel = Selector()
    commits_dir = os.path.join(DATA, "commits")
    commits = sorted(f[:-6] for f in os.listdir(commits_dir) if f.endswith(".patch"))
    all_split = None
    out = []
    for c in commits:
        try:
            with open(os.path.join(commits_dir, c + ".patch"), encoding="utf-8",
                      errors="replace") as f:
                chosen = sel.select(f.read())
        except Exception as e:                      # never crash: run everything
            log(f"{c}: error {e!r}, running all")
            chosen = RUN_ALL
        if chosen is RUN_ALL:
            if all_split is None:
                all_split = best_split([(ms, n) for n, ms in sel.ms.items()])
            workers = all_split
            log(f"{c}: ALL tests")
        else:
            workers = best_split([(sel.ms[n], n) for n in chosen])
            log(f"{c}: {len(chosen)} tests")
        out.append(json.dumps({"commit": c, "workers": workers}, separators=(",", ":")))
    sys.stdout.write("\n".join(out) + "\n")


if __name__ == "__main__":
    main()
