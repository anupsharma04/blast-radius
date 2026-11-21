"""
pytest plugin used by tools/validate.py: records every test's outcome and
every file that failed to collect (import errors) to $OUTCOME_OUT.

Works with pytest-xdist (-n N): workers send their reports to the main
("controller") process, and only the controller writes the file.
"""

import json
import os

_outcomes = {}
_collect_errors = []


def pytest_collectreport(report):
    if report.failed:
        _collect_errors.append(report.nodeid)


def pytest_runtest_logreport(report):
    if report.failed:
        _outcomes[report.nodeid] = "failed"
    elif report.when == "call" and report.nodeid not in _outcomes:
        _outcomes[report.nodeid] = "skipped" if report.skipped else "passed"
    elif report.skipped and report.nodeid not in _outcomes:
        _outcomes[report.nodeid] = "skipped"


def pytest_sessionfinish(session):
    if hasattr(session.config, "workerinput"):     # an xdist worker: don't write
        return
    with open(os.environ["OUTCOME_OUT"], "w") as f:
        json.dump({"outcomes": _outcomes, "collect_errors": _collect_errors}, f)
