"""
pytest plugin used by tools/build_linemap.py (Step 3): records each test's
outcome (passed / failed / skipped) to $RECORD_OUT, so the builder can check
that every test passes on the unmodified code.

The line recording itself lives in tools/linecov_site/sitecustomize.py,
because it must also run inside child processes that tests start.
"""

import json
import os

_outcomes = {}


def pytest_runtest_logreport(report):
    if report.failed:
        _outcomes[report.nodeid] = "failed"
    elif report.when == "call" and report.nodeid not in _outcomes:
        _outcomes[report.nodeid] = "skipped" if report.skipped else "passed"
    elif report.skipped and report.nodeid not in _outcomes:
        _outcomes[report.nodeid] = "skipped"


def pytest_sessionfinish(session):
    with open(os.environ["RECORD_OUT"], "w") as f:
        json.dump(_outcomes, f)
