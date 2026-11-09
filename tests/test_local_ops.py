"""M11 : vraies sauvegardes PostgreSQL et restauration locale dans une base neuve."""

import json
import os
import subprocess
import tempfile
import time
import uuid
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import psycopg
from django.core.management import call_command
from django.db import connection
from django.test import TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext

from apps.core.consistency import check_consistency
from apps.core.diagnostics import heartbeat, operational_status
from apps.core.local_recovery import (
    RecoveryError,
    backup,
    crypt_file,
    db_parameters,
    keygen,
    read_key,
    restore,
    sha,
    unpack_verified,
)
from apps.core.models import Operation
from apps.requests.models import Delivery
from tests.test_corrections import CorrectionFixtures
from tests.test_receiving import ReceivingFixtures


class LocalOperationsTests(ReceivingFixtures, CorrectionFixtures, TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.folder = tempfile.TemporaryDirectory(prefix="reserve-m11-test-")
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name).resolve()
        self.key = self.root / "key"
        keygen(self.key)

    def snapshot(self):
        return backup(self.root / "backup", self.key)

    def test_real_http_server_crash_and_restart_preserve_business_facts(self):
        import socket
        from urllib.error import URLError
        from urllib.request import ProxyHandler, build_opener

        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        environment = {
            **os.environ,
            "DATABASE_NAME": connection.settings_dict["NAME"],
            "DATABASE_ROLE": "migration",
            "NOTIFICATION_EXTERNAL_ALLOWED": "0",
            "NOTIFICATION_PUSH_ENABLED": "0",
            "LOCAL_RECOVERY_ENABLED": "1",
        }
        command = [
            str(Path(".venv/bin/python").absolute()),
            "manage.py",
            "runserver",
            f"127.0.0.1:{port}",
            "--noreload",
            "--settings=config.settings.local",
        ]
        opener = build_opener(ProxyHandler({}))

        def start():
            process = subprocess.Popen(  # noqa: S603
                command, env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )  # noqa: S603
            self.addCleanup(lambda: process.poll() is None and process.kill())
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                self.assertIsNone(process.poll())
                try:
                    with opener.open(
                        f"http://127.0.0.1:{port}/health/ready", timeout=1
                    ) as response:
                        if response.status == 200:
                            return process
                except (URLError, ConnectionError):
                    pass
                time.sleep(0.05)
            self.fail("Le serveur de laboratoire ne démarre pas")

        before = Operation.objects.count()
        first = start()
        first.kill()
        first.wait(timeout=5)
        with self.assertRaises((URLError, ConnectionError)):
            opener.open(f"http://127.0.0.1:{port}/health/ready", timeout=1)
        second = start()
        second.terminate()
        second.wait(timeout=5)
        self.assertEqual(Operation.objects.count(), before)

    def test_concurrent_request_timings_no_over_reservation(self):
        import threading
        from concurrent.futures import ThreadPoolExecutor

        from django.db import close_old_connections

        from apps.core import idempotency
        from apps.core.policies import ActorContext

        product = self.demandable_product(physical=3)
        context = ActorContext(self.assistant.pk, self.assistant.auth_version)
        payload = {"lines": [{"product_id": str(product.pk), "quantity": 1}]}
        bindings = [self.intention("requests.create", payload, context=context) for _ in range(5)]
        barrier = threading.Barrier(5, timeout=10)

        def execute(index):
            close_old_connections()
            try:
                barrier.wait()
                start = time.perf_counter()
                with CaptureQueriesContext(connection) as queries:
                    identifier, key = bindings[index]
                    outcome = idempotency.execute_intent(
                        context, intent_id=identifier, key=key, payload=payload
                    )
                return {
                    "status": outcome.status,
                    "seconds": time.perf_counter() - start,
                    "sql_count": len(queries),
                }
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=5) as pool:
            results = list(pool.map(execute, range(5)))
        self.assertEqual(sorted(r["status"] for r in results), [201, 201, 201, 409, 409])
        self.assertEqual(self.stock(product)[:3], (3, 3, 0))
        evidence = Path(os.environ.get("M11_EVIDENCE_ROOT", "docs/recette/M11-LOCAL"))
        evidence.mkdir(parents=True, exist_ok=True)
        (evidence / ("concurrency-" + uuid.uuid4().hex + ".json")).write_text(
            json.dumps(
                {
                    "connections": 5,
                    "results": results,
                    "final_P_R_D": [3, 3, 0],
                    "deadlock_or_overreservation": False,
                },
                indent=2,
            )
        )

    def test_key_is_private_and_cannot_be_overwritten(self):
        self.assertEqual(self.key.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(FileExistsError):
            keygen(self.key)
        self.key.chmod(0o644)
        with self.assertRaisesRegex(RecoveryError, "KEY_PERMISSIONS"):
            read_key(self.key)

    def test_recovery_requires_explicit_opt_in_even_on_loopback(self):
        with override_settings(LOCAL_RECOVERY_ENABLED=False):
            with self.assertRaisesRegex(RecoveryError, "LOCAL_OWNED_CLUSTER_REQUIRED"):
                db_parameters()
        with override_settings(LOCAL_RECOVERY_ENABLED=True):
            params = db_parameters()
        self.assertIn(params["host"], {"localhost", "127.0.0.1", "::1"})
        self.assertEqual(params["dbname"], connection.settings_dict["NAME"])

    def crafted_archive(self, files):
        import hashlib
        import zipfile

        root = self.root / "crafted"
        root.mkdir(mode=0o700)
        with zipfile.ZipFile(root / "payload.zip", "w") as bundle:
            for name, value in files.items():
                bundle.writestr(name, value)
            bundle.writestr(
                "metadata.json",
                json.dumps(
                    {
                        "format": 1,
                        "files": {k: hashlib.sha256(v).hexdigest() for k, v in files.items()},
                    }
                ),
            )
        crypt_file(root / "payload.zip", root / "backup.enc", read_key(self.key))
        (root / "manifest.json").write_text(
            json.dumps({"format": 1, "archive_sha256": sha(root / "backup.enc")})
        )
        return root

    def test_authenticated_but_incomplete_archive_is_rejected(self):
        archive = self.crafted_archive({})
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(RecoveryError, "ARCHIVE_INCOMPLETE"):
                unpack_verified(archive, self.key, temporary)

    def test_archive_traversal_rejected_before_restore(self):
        archive = self.crafted_archive(
            {"database.dump": b"fictive", "photos/../../escape": b"fictive"}
        )
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(RecoveryError, "ARCHIVE_PATH"):
                unpack_verified(archive, self.key, temporary)
        self.assertFalse((self.root / "escape").exists())

    def test_diagnostics_distinguish_missing_worker_and_unverified_backup(self):
        report = operational_status()
        self.assertEqual(report["database"], "AVAILABLE")
        self.assertEqual(report["worker"]["status"], "UNKNOWN")
        self.assertIn("BACKUP_NOT_RESTORE_VERIFIED", report["warnings"])
        self.assertFalse(report["notifications_initialized"])
        with patch("apps.core.diagnostics.MigrationExecutor.migration_plan", return_value=[1]):
            incomplete = operational_status()
        self.assertTrue(incomplete["migrations_pending"])
        self.assertEqual(incomplete["queue"], [])
        self.assertIsNone(incomplete["notifications_initialized"])
        product = self.demandable_product()
        from apps.notifications import dispatcher

        dispatcher.initialize()
        self.assertEqual(self.send([product], [1]).status_code, 201)
        pending = operational_status()
        self.assertTrue(pending["notifications_initialized"])
        self.assertGreaterEqual(pending["unprojected_events"], 1)
        heartbeat(self.root / "worker.json")
        self.assertEqual(
            operational_status(heartbeat_file=self.root / "worker.json")["worker"]["status"],
            "RUNNING",
        )
        heartbeat(self.root / "worker.json", state="STOPPED")
        self.assertEqual(
            operational_status(heartbeat_file=self.root / "worker.json")["worker"]["status"],
            "STALE_OR_STOPPED",
        )

    def test_archive_limit_refuses_without_announcing_success(self):
        with override_settings(M11_BACKUP_MAX_BYTES=1):
            with self.assertRaisesRegex(RecoveryError, "ARCHIVE_LIMIT"):
                self.snapshot()
        self.assertFalse((self.root / "backup/manifest.json").exists())

    def test_corrupted_diagnostic_files_do_not_leak_tracebacks(self):
        broken = self.root / "worker.json"
        broken.write_text("[]")
        report = operational_status(heartbeat_file=broken, backup_directory=self.root)
        self.assertEqual(report["worker"]["status"], "UNAVAILABLE")
        self.assertEqual(report["backup"]["status"], "INCOMPLETE")

    def test_diagnostic_detects_inconsistent_balance_without_writing(self):
        self.assertTrue(connection.settings_dict["NAME"].startswith("test_"))
        product = self.demandable_product(physical=10)
        with connection.cursor() as cursor:
            cursor.execute("ALTER TABLE inventory_stockbalance DISABLE TRIGGER USER")
            cursor.execute(
                "UPDATE inventory_stockbalance SET reserved=1 WHERE product_id=%s", [product.pk]
            )
            cursor.execute("ALTER TABLE inventory_stockbalance ENABLE TRIGGER USER")
        before = Operation.objects.count()
        report = check_consistency()
        self.assertFalse(report["ok"])
        self.assertTrue(any(i["object"] == str(product.pk) for i in report["issues"]))
        self.assertEqual(Operation.objects.count(), before)

    def test_actual_worker_crash_and_restart_does_not_duplicate_notifications(self):
        from apps.notifications import dispatcher
        from apps.notifications.models import Notification, WorkItem

        dispatcher.initialize()
        product = self.demandable_product()
        self.assertEqual(self.send([product], [2]).status_code, 201)
        before = Operation.objects.count()
        environment = {
            **os.environ,
            "DJANGO_SETTINGS_MODULE": "config.settings.migration",
            "DATABASE_NAME": connection.settings_dict["NAME"],
            "NOTIFICATION_EXTERNAL_ALLOWED": "0",
            "NOTIFICATION_PUSH_ENABLED": "0",
            "NOTIFICATION_EMAIL_MODE": "CAPTURE",
        }
        command = [
            str(Path(".venv/bin/python").absolute()),
            "manage.py",
            "run_outbox",
            "--limit",
            "20",
            "--heartbeat-file",
            str(self.root / "worker.json"),
        ]

        def launch():
            process = subprocess.Popen(  # noqa: S603
                command, env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )  # noqa: S603
            self.addCleanup(lambda: process.poll() is None and process.kill())
            return process

        first = launch()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not Notification.objects.exists():
            self.assertIsNone(first.poll(), "Worker arrêté prématurément")
            time.sleep(0.05)
        self.assertTrue(Notification.objects.exists())
        # Arrêt brutal de ce processus de test uniquement, puis reprise réelle.
        first.kill()
        first.wait(timeout=5)
        count = Notification.objects.count()
        self.assertEqual(
            operational_status(heartbeat_file=self.root / "worker.json")["worker"]["status"],
            "UNAVAILABLE",
        )
        second = launch()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if not WorkItem.objects.filter(state__in=["PENDING", "RETRY", "CLAIMED"]).exists():
                break
            time.sleep(0.05)
        self.assertFalse(
            WorkItem.objects.filter(state__in=["PENDING", "RETRY", "CLAIMED"]).exists()
        )
        second.terminate()
        second.wait(timeout=5)
        self.assertEqual(Notification.objects.count(), count)
        self.assertEqual(Operation.objects.count(), before)

    def test_missing_photo_is_reported_without_repair(self):
        from apps.catalog.models import PhotoAsset
        from apps.catalog.storage import photo_storage

        self.demandable_product()
        asset = PhotoAsset.objects.first()
        path = Path(photo_storage().path(asset.object_key))
        content = path.read_bytes()
        path.unlink()  # Uniquement fichier de test temporaire de cette fixture.
        before = Operation.objects.count()
        result = check_consistency()
        self.assertIn({"rule": "PHOTO_MISSING", "object": str(asset.pk)}, result["issues"])
        self.assertEqual(Operation.objects.count(), before)
        path.write_bytes(content)
        self.assertTrue(check_consistency()["ok"])

    def test_restore_failure_is_not_success_and_existing_database_is_protected(self):
        self.snapshot()
        name = "restore_m11_" + uuid.uuid4().hex
        target = self.root / "failed"
        with patch(
            "apps.core.local_recovery.pg_run", side_effect=RecoveryError("INJECTED_TOOL_FAILURE")
        ):
            with self.assertRaisesRegex(RecoveryError, "RETAINED_ISOLATED"):
                restore(self.root / "backup", self.key, database=name, destination=target)
        self.assertTrue((target / "failure.json").is_file())
        self.assertFalse((target / "recovery.json").exists())
        with self.assertRaisesRegex(RecoveryError, "DATABASE_EXISTS"):
            restore(
                self.root / "backup", self.key, database=name, destination=self.root / "another"
            )

    def test_missing_archive_and_wrong_key_cannot_restore(self):
        self.snapshot()
        wrong = self.root / "wrong.key"
        keygen(wrong)
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(RecoveryError, "AUTHENTICATION"):
                unpack_verified(self.root / "backup", wrong, temporary)
        (self.root / "backup/backup.enc").unlink()
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(FileNotFoundError):
                unpack_verified(self.root / "backup", self.key, temporary)

    def test_local_volume_measurements_real_pages_and_mutations(self):
        import hashlib
        import tracemalloc

        from apps.catalog.models import Product

        measurements = []

        def measure(label, action):
            tracemalloc.start()
            started = time.perf_counter()
            with CaptureQueriesContext(connection) as queries:
                response = action()
            elapsed = time.perf_counter() - started
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            self.assertIn(response.status_code, [200, 201, 302], response.content[:500])
            measurements.append(
                {
                    "label": label,
                    "seconds": elapsed,
                    "sql_count": len(queries),
                    "python_peak_bytes": peak,
                    "slowest_queries": [
                        {
                            "seconds": q["time"],
                            "sql_sha256": hashlib.sha256(q["sql"].encode()).hexdigest(),
                        }
                        for q in sorted(
                            queries.captured_queries, key=lambda q: float(q["time"]), reverse=True
                        )[:3]
                    ],
                }
            )
            return response

        request, products = self.prepare((4,), physical=100)
        product = products[0]
        measure("livraison", lambda: self.deliver(request))
        order = self.buy([product], [10])
        measure("réception_partielle", lambda: self.receive(order, [5]))
        measure("demande_et_réservation", lambda: self.send([product], [2]))
        delivery = Delivery.objects.get(request=request)
        case = self.error_case(delivery)
        for i in range(100):
            case.refresh_from_db()
            response = self.post_json(
                f"/api/v1/cases/{case.pk}/review",
                {
                    "expected_version": case.version,
                    "verification": True,
                    "message": f"Vérification fictive {i}",
                },
            )
            self.assertEqual(response.status_code, 200, response.content)
        for volume in [100, 1000]:
            existing = Product.objects.filter(code__startswith="BENCH-").count()
            Product.objects.bulk_create(
                [
                    Product(
                        code=f"BENCH-{i:05}",
                        name=f"Produit synthétique {i:05}",
                        unit="boîte",
                        minimum=0,
                        target=20,
                    )
                    for i in range(existing, volume)
                ]
            )
            for route in [
                "/catalogue",
                "/catalogue?q=BENCH-009",
                "/stocks",
                "/demandes",
                "/receptions",
                "/notifications",
                "/tableau-de-bord",
                "/analyses/stocks",
                "/analyses/history",
                f"/dossiers/{case.pk}",
            ]:
                measure(f"{volume}produits:{route}", lambda r=route: self.admin_client.get(r))
            measure(
                f"{volume}produits:export_stocks",
                lambda: self.admin_client.post("/analyses/stocks/export.xlsx"),
            )
        evidence = Path(os.environ.get("M11_EVIDENCE_ROOT", "docs/recette/M11-LOCAL"))
        evidence.mkdir(parents=True, exist_ok=True)
        (evidence / ("performance-" + uuid.uuid4().hex + ".json")).write_text(
            json.dumps(
                {
                    "measurements": measurements,
                    "scope": "Catalogue 100/1000 non inventoriés + produit actif, dossier100notes, données fictives ; mémoire Python tracemalloc, pas RSS/SLA production.",
                },
                ensure_ascii=False,
                indent=2,
            )
        )

    def test_backup_ciphertext_and_verified_integrity(self):
        product = self.demandable_product(physical=15)
        self.buy([product], [10])
        report = self.snapshot()
        self.assertFalse(report["restored_and_verified"])
        self.assertEqual((self.root / "backup").stat().st_mode & 0o777, 0o700)
        self.assertNotIn(b"PGDMP", (self.root / "backup/backup.enc").read_bytes())
        with tempfile.TemporaryDirectory() as temporary:
            meta = unpack_verified(self.root / "backup", self.key, temporary)
            self.assertIn("database.dump", meta["files"])
            self.assertTrue(any(k.startswith("photos/") for k in meta["files"]))
        self.assertTrue(check_consistency()["ok"])

    def test_missing_archive_and_changed_archive_are_rejected(self):
        self.snapshot()
        archive = self.root / "backup/backup.enc"
        content = archive.read_bytes()
        archive.write_bytes(content[:-1])
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(RecoveryError, "ARCHIVE_INTEGRITY"):
                unpack_verified(self.root / "backup", self.key, temporary)

    def test_ciphertext_tamper_even_with_updated_public_hash_is_rejected(self):
        self.snapshot()
        archive = self.root / "backup/backup.enc"
        content = bytearray(archive.read_bytes())
        content[-20] ^= 1
        archive.write_bytes(content)
        manifest = self.root / "backup/manifest.json"
        report = json.loads(manifest.read_text())
        report["archive_sha256"] = sha(archive)
        manifest.write_text(json.dumps(report))
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(RecoveryError, "AUTHENTICATION"):
                unpack_verified(self.root / "backup", self.key, temporary)

    def test_restore_never_targets_demo_or_existing_files(self):
        with self.assertRaisesRegex(RecoveryError, "PREFIX"):
            restore(
                self.root, self.key, database="stock_app_example", destination=self.root / "restore"
            )
        with self.assertRaisesRegex(RecoveryError, "DIRECTORY"):
            restore(self.root, self.key, database="restore_m11_unused", destination=self.root)

    def test_backup_destination_must_not_exist_and_key_is_separate(self):
        self.snapshot()
        with self.assertRaisesRegex(RecoveryError, "DESTINATION_EXISTS"):
            self.snapshot()
        with self.assertRaisesRegex(RecoveryError, "SEPARATION"):
            backup(self.root, self.key)

    def test_consistency_is_readonly_and_accepts_open_initial_cases(self):
        product = self.demandable_product(physical=0)
        order = self.buy([product], [10])
        response = self.receive(order, [3], [1])
        self.assertEqual(response.status_code, 201, response.content)
        before = Operation.objects.count()
        report = check_consistency()
        self.assertTrue(report["ok"], report)
        call_command("check_consistency", stdout=StringIO())
        self.assertEqual(Operation.objects.count(), before)

    def test_restore_incompatible_schema_refused_before_database_creation(self):
        self.snapshot()
        name = "restore_m11_" + uuid.uuid4().hex
        with patch("apps.core.local_recovery.migrations", return_value={}):
            with self.assertRaisesRegex(RecoveryError, "INCOMPATIBLE"):
                restore(
                    self.root / "backup",
                    self.key,
                    database=name,
                    destination=self.root / "recovery",
                )
        with psycopg.connect(**db_parameters("postgres")) as conn:
            self.assertIsNone(
                conn.execute("SELECT 1 FROM pg_database WHERE datname=%s", [name]).fetchone()
            )

    def test_real_restore_point_photos_and_new_facts_are_identified(self):
        # Exercice conservé, clé hors archive et hors dépôt ; source jetable uniquement.
        exercise = uuid.uuid4().hex
        self.root = Path(".local/m11-exercises").resolve() / exercise
        self.root.mkdir(parents=True, mode=0o700)
        key_root = Path(".local/m11-keys").resolve()
        key_root.mkdir(mode=0o700, exist_ok=True)
        self.key = key_root / (exercise + ".key")
        keygen(self.key)
        request, products = self.prepare((10,), physical=30)
        product = products[0]
        self.assertEqual(self.deliver(request).status_code, 201)
        delivery = Delivery.objects.get(request=request)
        case = self.error_case(delivery)
        for quantity in [8, 9]:
            body = CorrectionFixtures.correction_body(self, delivery, case, [quantity])
            preview = self.post_json("/api/v1/corrections/preview", body)
            self.assertEqual(preview.status_code, 200, preview.content)
            body["preview_hash"] = preview.json()["preview"]["preview_hash"]
            corrected = self.post_json("/api/v1/corrections/confirm", body)
            self.assertEqual(corrected.status_code, 200, corrected.content)
        self.assertEqual(self.send([product], [4]).status_code, 201)
        order = self.buy([product], [10])
        response = self.receive(order, [5], [1])
        self.assertEqual(response.status_code, 201, response.content)
        at_point = set(Operation.objects.values_list("id", flat=True))
        started = time.monotonic()
        self.snapshot()
        backup_seconds = time.monotonic() - started
        self.assertEqual(self.receive(order, [2]).status_code, 201)
        after_point = set(Operation.objects.values_list("id", flat=True)) - at_point
        self.assertTrue(after_point)
        name = "restore_m11_" + uuid.uuid4().hex
        target = self.root / "recovery"
        started = time.monotonic()
        result = restore(self.root / "backup", self.key, database=name, destination=target)
        restore_seconds = time.monotonic() - started
        self.assertFalse(result["consistency_verified"])
        with psycopg.connect(**db_parameters(name)) as conn:
            restored_ids = {row[0] for row in conn.execute("SELECT id FROM core_operation")}
            self.assertEqual(restored_ids, at_point)
            self.assertFalse(after_point & restored_ids)
            self.assertEqual(
                conn.execute(
                    "SELECT received FROM purchasing_orderline WHERE order_id=%s", [order.pk]
                ).fetchone()[0],
                5,
            )
        environment = {
            **os.environ,
            "DJANGO_SETTINGS_MODULE": "config.settings.restored",
            "RESERVE_RECOVERY_DIR": str(target),
        }
        completed = subprocess.run(  # noqa: S603
            [str(Path(".venv/bin/python").absolute()), "manage.py", "check_consistency"],
            env=environment,
            capture_output=True,
            text=True,
            timeout=60,
        )  # noqa: S603
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        verified = subprocess.run(  # noqa: S603
            [
                str(Path(".venv/bin/python").absolute()),
                "manage.py",
                "verify_restoration",
                "--backup-directory",
                str(self.root / "backup"),
            ],
            env=environment,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(verified.returncode, 0, verified.stdout + verified.stderr)
        guard_code = """
import json, django
django.setup()
from django.db import connection, DatabaseError
from django.conf import settings
from apps.core.models import OperationIntent
from apps.core.idempotency import authorize
from apps.core.errors import BusinessError
from apps.accounts.models import UserSession
from apps.accounts.sessions import checked_session
result = {'external_disabled': not settings.NOTIFICATION_EXTERNAL_ALLOWED and not settings.NOTIFICATION_PUSH_ENABLED}
intent = OperationIntent.objects.select_related('actor').first()
try:
    authorize(intent.actor, intent)
except BusinessError as exc:
    result['old_intent_refused'] = exc.code == 'RECOVERY_RECONCILIATION_REQUIRED'
session = UserSession.objects.select_related('user').first()
try:
    checked_session(session.user, session.pk)
except BusinessError as exc:
    result['old_session_refused'] = exc.code == 'RECOVERY_RECONCILIATION_REQUIRED'
try:
    with connection.cursor() as cursor:
        cursor.execute('UPDATE catalog_product SET name=name')
except DatabaseError as exc:
    result['writes_refused'] = getattr(exc.__cause__, 'sqlstate', '') == '25006'
print(json.dumps(result))
"""
        guards = subprocess.run(  # noqa: S603
            [str(Path(".venv/bin/python").absolute()), "-c", guard_code],
            env=environment,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(guards.returncode, 0, guards.stdout + guards.stderr)
        guard_result = json.loads(guards.stdout)
        self.assertEqual(
            guard_result,
            {
                "external_disabled": True,
                "old_intent_refused": True,
                "old_session_refused": True,
                "writes_refused": True,
            },
        )
        worker = subprocess.run(  # noqa: S603
            [str(Path(".venv/bin/python").absolute()), "manage.py", "run_outbox", "--once"],
            env=environment,
            capture_output=True,
            text=True,
            timeout=60,
        )  # noqa: S603
        self.assertNotEqual(worker.returncode, 0)
        self.assertIn("worker suspendu", worker.stderr)
        # Base nouvellement restaurée conservée isolée pour investigation ; aucune base existante supprimée.
        evidence = Path(os.environ.get("M11_EVIDENCE_ROOT", "docs/recette/M11-LOCAL"))
        evidence.mkdir(parents=True, exist_ok=True)
        (evidence / f"restore-{name}.json").write_text(
            json.dumps(
                {
                    "database": name,
                    "restored_operations": len(restored_ids),
                    "post_point_operations_not_replayed": [str(i) for i in after_point],
                    "consistency": json.loads(completed.stdout),
                    "external_worker_blocked": True,
                    "backup_seconds": backup_seconds,
                    "restore_seconds": restore_seconds,
                    "archive_bytes": (self.root / "backup/backup.enc").stat().st_size,
                    "exercise_directory": str(self.root),
                    "restoration_attested": True,
                    "recovery_guards": guard_result,
                    "paper_reconciliation_proposal_only": [
                        {
                            "paper": "FICTIF-001",
                            "operation": str(next(iter(restored_ids))),
                            "finding": "ALREADY_RECORDED_DO_NOT_REENTER",
                        },
                        {
                            "paper": "FICTIF-002",
                            "operation": str(next(iter(after_point))),
                            "finding": "POST_SNAPSHOT_REQUIRES_HUMAN_RECONCILIATION",
                        },
                        {
                            "paper": "FICTIF-003",
                            "finding": "UNCERTAIN_ABSENCE_IS_NOT_PROOF_OF_NO_PHYSICAL_MOVEMENT",
                        },
                    ],
                },
                indent=2,
            )
        )
