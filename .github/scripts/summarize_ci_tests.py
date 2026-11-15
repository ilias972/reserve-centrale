"""Write a secret-free test summary to the GitHub Actions job summary."""

from __future__ import annotations

import json
import os
from pathlib import Path


def main() -> int:
    report_path = Path(os.environ.get("TEST_REPORT_PATH", ""))
    exit_code = os.environ.get("TEST_EXIT_CODE", "not recorded")
    report = None

    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pass

    if report:
        tests = report.get("tests", [])
        counts = {
            status: sum(item.get("status") == status for item in tests)
            for status in ("PASS", "FAIL", "ERROR", "SKIP")
        }
        tests_run = report.get("tests_run", len(tests))
        duration = report.get("duration_seconds", "unavailable")
        python_version = report.get("python", "unavailable")
        django_version = report.get("django", "unavailable")
        psycopg_version = report.get("psycopg", "unavailable")
        postgres_version = report.get("postgresql", "unavailable").splitlines()[0]
        status_text = "available"
    else:
        counts = {"PASS": 0, "FAIL": 0, "ERROR": 0, "SKIP": 0}
        tests_run = "unavailable"
        duration = "unavailable"
        python_version = django_version = psycopg_version = postgres_version = "unavailable"
        status_text = "not produced (the test process may have stopped before reporting)"

    summary = [
        "## Django test results",
        "",
        f"- Report: {status_text}",
        f"- Tests run: {tests_run}",
        f"- Passed: {counts['PASS']}",
        f"- Failed: {counts['FAIL']}",
        f"- Errors: {counts['ERROR']}",
        f"- Skipped: {counts['SKIP']}",
        f"- Test process exit code: {exit_code}",
        f"- Duration (seconds): {duration}",
        f"- Python: {python_version}",
        f"- Django: {django_version}",
        f"- psycopg: {psycopg_version}",
        f"- PostgreSQL: {postgres_version}",
        "",
        "Detailed test evidence remains in the ephemeral runner workspace and is not uploaded.",
        "",
    ]
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with Path(summary_path).open("a", encoding="utf-8") as output:
            output.write("\n".join(summary))

    runner_temp = os.environ.get("RUNNER_TEMP")
    if runner_temp:
        safe_report = {
            "report_available": bool(report),
            "tests_run": tests_run,
            "passed": counts["PASS"],
            "failed": counts["FAIL"],
            "errors": counts["ERROR"],
            "skipped": counts["SKIP"],
            "process_exit_code": exit_code,
            "duration_seconds": duration,
            "python": python_version,
            "django": django_version,
            "psycopg": psycopg_version,
            "postgresql": postgres_version,
        }
        output_path = Path(runner_temp) / "reserve-tests" / "summary.json"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(safe_report, indent=2) + "\n", encoding="utf-8")

    print("\n".join(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
