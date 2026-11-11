"""COMAPEO_REQUIRE_SERVER=1 turns a skipped real_server test into a failure and
fails the run if none passed; COMAPEO_REQUIRE_QGIS=1 makes a missing QGIS an
error. CI sets them so that a green run means the real-server tests ran."""

from __future__ import annotations

import os
from collections import Counter

import pytest

REQUIRE_SERVER = os.environ.get("COMAPEO_REQUIRE_SERVER") == "1"
REQUIRE_QGIS = os.environ.get("COMAPEO_REQUIRE_QGIS") == "1"

if REQUIRE_QGIS:
    import qgis.core
else:
    try:
        import qgis.core  # noqa: F401
    except ImportError:
        # tests/qgis imports qgis at module level; outside QGIS's Python skip it.
        collect_ignore = ["qgis"]

_counts: Counter[tuple[bool, str]] = Counter()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    real = item.get_closest_marker("real_server") is not None
    if REQUIRE_SERVER and real and report.skipped and not hasattr(report, "wasxfail"):
        reason = report.longrepr[2] if isinstance(report.longrepr, tuple) else ""
        report.outcome = "failed"
        report.longrepr = f"real_server skip under COMAPEO_REQUIRE_SERVER=1: {reason}"
    if report.when == "call" or (report.when == "setup" and not report.passed):
        _counts[(real, report.outcome)] += 1


def pytest_sessionfinish(session, exitstatus):
    if REQUIRE_SERVER and not _counts[(True, "passed")] and exitstatus == 0:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


def pytest_terminal_summary(terminalreporter):
    if not (REQUIRE_SERVER or REQUIRE_QGIS):
        return
    lines = []
    for real, label in ((True, "real_server"), (False, "other")):
        counts = ", ".join(
            f"{_counts[(real, outcome)]} {outcome}"
            for outcome in ("passed", "failed", "skipped")
        )
        lines.append(f"{label}: {counts}")
    if REQUIRE_SERVER and not _counts[(True, "passed")]:
        lines.append("no real_server test passed")
    for line in lines:
        terminalreporter.write_line(line)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            name = os.environ.get("COMAPEO_JOB_LABEL", "pytest")
            f.write(f"### {name}\n\n" + "".join(f"- {line}\n" for line in lines))
