import hashlib
import json
import os
import platform
import time
import unittest
from datetime import UTC, datetime
from pathlib import Path

import django
import psycopg
from django.db import connection
from django.test.runner import DiscoverRunner


class EvidenceResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.evidence = []

    def addSuccess(self, test):
        super().addSuccess(test)
        self.evidence.append({"test": test.id(), "status": "PASS"})

    def addFailure(self, test, error):
        super().addFailure(test, error)
        self.evidence.append({"test": test.id(), "status": "FAIL"})

    def addError(self, test, error):
        super().addError(test, error)
        self.evidence.append({"test": test.id(), "status": "ERROR"})

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self.evidence.append({"test": test.id(), "status": "SKIP", "reason": reason})


class EvidenceRunner(DiscoverRunner):
    def get_resultclass(self):
        return EvidenceResult

    def run_suite(self, suite, **kwargs):
        self.started = time.monotonic()
        with connection.cursor() as cursor:
            cursor.execute("SELECT version(), current_database()")
            postgres_version, database = cursor.fetchone()
        self.database_evidence = {"postgresql": postgres_version, "database": database}
        root = Path(__file__).resolve().parent.parent
        sources = sorted(
            p
            for folder in ["apps", "config", "tests", "scripts", "templates", "static"]
            for p in (root / folder).rglob("*")
            if p.is_file() and p.suffix in {".py", ".html", ".css", ".js"}
        )
        sources += [root / "manage.py"] + [
            root / name for name in ["pyproject.toml", "requirements.txt", "requirements-dev.txt"]
        ]
        manifest = {
            str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sources
        }
        self.source_digest = hashlib.sha256(
            json.dumps(manifest, sort_keys=True).encode()
        ).hexdigest()
        return super().run_suite(suite, **kwargs)

    def suite_result(self, suite, result, **kwargs):
        from tests.accounts_evidence import observations as accounts_observations
        from tests.catalog_evidence import DEPENDENT_SCENARIOS
        from tests.catalog_evidence import observations as catalog_observations
        from tests.inventory_evidence import DEPENDENT_SCENARIOS as inventory_scenarios
        from tests.inventory_evidence import observations as inventory_observations
        from tests.probes.commands import observations
        from tests.requests_evidence import DEPENDENT_SCENARIOS as request_scenarios
        from tests.requests_evidence import observations as request_observations

        path = Path(os.environ.get("TEST_REPORT_PATH", "reports/tests.json"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "executed_at_utc": datetime.now(UTC).isoformat(),
                    "python": platform.python_version(),
                    "django": django.get_version(),
                    "psycopg": psycopg.__version__,
                    **self.database_evidence,
                    "source_digest": self.source_digest,
                    "duration_seconds": round(time.monotonic() - self.started, 3),
                    "observations": observations,
                    "accounts_observations": accounts_observations,
                    "catalog_observations": catalog_observations,
                    "dependent_catalog_scenarios": DEPENDENT_SCENARIOS,
                    "inventory_observations": inventory_observations,
                    "dependent_inventory_scenarios": inventory_scenarios,
                    "request_observations": request_observations,
                    "dependent_request_scenarios": request_scenarios,
                    "tests_run": result.testsRun,
                    "failures": len(result.failures),
                    "errors": len(result.errors),
                    "skips": len(result.skipped),
                    "tests": result.evidence,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n"
        )
        return super().suite_result(suite, result, **kwargs)
