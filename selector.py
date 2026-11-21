"""
Blast Radius selector - version 1
Runs every test, but splits them evenly across the best number of workers.
"""
import heapq
import json
import os
import sys

DATA = "/data"


def load_tests():
    # tests.json -> list of (duration in ms, test number)
    with open(os.path.join(DATA, "tests.json")) as f:
        tests = json.load(f)["tests"]
    return [(round(t["seconds"] * 1000), t["number"]) for t in tests]


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
    # Try 1..8 workers and keep the cheapest:
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


def main():
    tests = load_tests()
    commits_dir = os.path.join(DATA, "commits")
    commits = sorted(f[:-6] for f in os.listdir(commits_dir) if f.endswith(".patch"))

    # Version 1: every commit runs every test, so the split is the same.
    workers = best_split(tests)
    print(f"{len(commits)} commits, {len(tests)} tests, {len(workers)} workers",
          file=sys.stderr)

    out = []
    for c in commits:
        out.append(json.dumps({"commit": c, "workers": workers}, separators=(",", ":")))
    sys.stdout.write("\n".join(out) + "\n")


if __name__ == "__main__":
    main()
