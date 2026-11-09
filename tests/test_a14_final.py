"""Bornes A14 : vrais fichiers, processus et connexions PostgreSQL isolées."""

import base64
import io
import random
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile

from django.db import close_old_connections, connection
from django.test import SimpleTestCase, TransactionTestCase, override_settings

from apps.core.errors import BusinessError
from apps.core.models import Operation
from apps.reporting import export_jobs, exports
from apps.reporting.filters import parse
from tests.support.benchmark_names import valid_a14_name
from tests.test_receiving import ReceivingFixtures


class A14WorkbookTests(SimpleTestCase):
    def workbook(self, count):
        return exports.workbook(
            "history",
            [{"cells": ["fait fictif", i]} for i in range(count)],
            parse({}),
            {"extracted": datetime(2026, 11, 2, tzinfo=UTC)},
        )

    def test_19999_rows_valid_xlsx(self):
        with ZipFile(io.BytesIO(self.workbook(19999))) as z:
            self.assertIsNone(z.testzip())
            self.assertEqual(z.read("xl/worksheets/sheet1.xml").count(b"<row>"), 20005)

    def test_20000_rows_valid_xlsx(self):
        with ZipFile(io.BytesIO(self.workbook(20000))) as z:
            self.assertIsNone(z.testzip())
            self.assertEqual(z.read("xl/worksheets/sheet1.xml").count(b"<row>"), 20006)

    def test_20001_rows_refused_without_truncation(self):
        with self.assertRaises(BusinessError):
            self.workbook(20001)

    def test_summary_data_rows_count_towards_total(self):
        with override_settings(REPORTING_EXPORT_MAX_ROWS=4), self.assertRaises(BusinessError):
            exports.workbook(
                "stocks", [], parse({}), {"extracted": datetime(2026, 11, 2, tzinfo=UTC)}
            )

    def test_config_cannot_raise_approved_row_ceiling(self):
        with override_settings(REPORTING_EXPORT_MAX_ROWS=99999), self.assertRaises(BusinessError):
            self.workbook(20001)

    def test_exact_20m_byte_buffer_and_next_byte_refused(self):
        b = exports.BoundedBuffer()
        self.assertEqual(b.write(b"a" * 20000000), 20000000)
        with self.assertRaises(BusinessError):
            b.write(b"b")
        self.assertEqual(len(b.getvalue()), 20000000)

    def test_actual_zip_size_checked(self):
        data = self.workbook(20)
        with override_settings(REPORTING_MAX_BYTES=len(data) - 1), self.assertRaises(BusinessError):
            self.workbook(20)

    def test_dangerous_text_remains_text(self):
        for value in [" =cmd", "\ufeff@bad", "\x01=cmd", "\u200b-2"]:
            self.assertTrue(exports.text(value).startswith("'"))

    def test_real_xlsx_below_and_above_20m(self):
        rng = random.Random(20261102)  # noqa: S311 — données compressibles fictives, pas des secrets.
        rows = [{"cells": [base64.b64encode(rng.randbytes(15000)).decode()]} for _ in range(1400)]
        meta = {"extracted": datetime(2026, 11, 2, tzinfo=UTC)}
        content = exports.workbook("history", rows[:1100], parse({}), meta)
        self.assertLess(len(content), 20000000)
        self.assertGreater(len(content), 15000000)
        with ZipFile(io.BytesIO(content)) as z:
            self.assertIsNone(z.testzip())
        with self.assertRaises(BusinessError):
            exports.workbook("history", rows, parse({}), meta)

    def test_benchmark_rejects_manual_and_production_names(self):
        for name in [
            "stock_app_example",
            "production",
            "bench_perf_m10_dense_ref",
            "bench_a14_x;DROP DATABASE x",
        ]:
            with self.assertRaises(ValueError):
                valid_a14_name(name)
        self.assertEqual(valid_a14_name("bench_a14_test"), "bench_a14_test")


class A14ProcessTests(ReceivingFixtures, TransactionTestCase):
    def export(self, name="stocks"):
        return self.admin_client.post(f"/analyses/{name}/export.xlsx")

    def test_export_reader_bounds_one_sql_without_repeated_offsets(self):
        from django.test.utils import CaptureQueriesContext

        from apps.catalog.models import Product
        from apps.reporting.iteration import stream

        products = [self.create_product(code=f"BOUNDED-{i}") for i in range(3)]
        query = Product.objects.filter(pk__in=[p.pk for p in products]).order_by("pk")
        with (
            override_settings(REPORTING_EXPORT_READER=True, REPORTING_EXPORT_MAX_ROWS=1),
            patch.dict(connection.settings_dict, {"DISABLE_SERVER_SIDE_CURSORS": True}),
            CaptureQueriesContext(connection) as queries,
        ):
            rows = list(stream(query))
        self.assertEqual(len(rows), 2)  # La ligne excédentaire permet le refus, pas une troncature.
        self.assertEqual(len(queries), 1)
        self.assertIn("LIMIT 2", queries[0]["sql"])
        self.assertNotIn("OFFSET", queries[0]["sql"])

    def test_surplus_post_filter_keeps_late_matching_candidate(self):
        from apps.receiving.models import QuarantineItem
        from apps.receiving.state import item_state
        from apps.reporting import selectors
        from apps.reporting.snapshots import materialize, snapshot

        for i, name in enumerate(
            ["Late matching product", "Other product one", "Other product two"]
        ):
            product = self.create_product(code=f"POSTFILTER-{i}", name=name)
            self.assertEqual(self.initial([product], [0]).status_code, 201)
            self.activate(product)
            order = self.buy([product], [20])
            self.assertEqual(self.receive(order, [23]).status_code, 201)
            item = QuarantineItem.objects.get(receipt_line__order_line__product=product)
            state = item_state(item.pk)
            response = self.post_json(
                f"/api/v1/quarantine/{item.pk}/accept-surplus",
                {
                    "expected_version": state["version"],
                    "quantity": 3,
                    "verification": True,
                    "expected_stock_version": self.stock(product)[3],
                },
            )
            self.assertEqual(response.status_code, 200, response.content)
        with (
            override_settings(
                REPORTING_EXPORT_READER=True, REPORTING_EXPORT_MAX_ROWS=1, REPORTING_MAX_ROWS=1
            ),
            patch.dict(connection.settings_dict, {"DISABLE_SERVER_SIDE_CURSORS": True}),
            snapshot(),
        ):
            rows = materialize(selectors.rows(self.admin, "surplus", parse({"q": "Late matching"})))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["cells"][2], "Late matching product")

    def test_real_child_export_is_valid_and_read_only(self):
        p = self.demandable_product()
        before = self.stock(p)
        r = self.export()
        self.assertEqual(r.status_code, 200, r.content[:200])
        with ZipFile(io.BytesIO(r.content)) as z:
            self.assertIsNone(z.testzip())
        self.assertEqual(self.stock(p), before)
        self.assertEqual(Operation.objects.filter(command_type="reporting.export").count(), 1)

    def test_timeout_kills_reaps_and_cleans_partial_file(self):
        processes, directories = [], []
        original = subprocess.Popen

        def start(*args, **kwargs):
            process = original(*args, **kwargs)
            processes.append(process)
            return process

        command = [
            sys.executable,
            "-c",
            "import json,sys,time; p=json.load(sys.stdin); "
            "open(p['output'],'wb').write(b'partial'); time.sleep(60)",
        ]
        temporary = tempfile.TemporaryDirectory

        def folder(*args, **kwargs):
            result = temporary(*args, **kwargs)
            directories.append(result.name)
            return result

        with (
            override_settings(REPORTING_EXPORT_TIMEOUT_MS=300),
            patch.object(export_jobs, "worker_command", return_value=command),
            patch.object(export_jobs.subprocess, "Popen", side_effect=start),
            patch.object(export_jobs.tempfile, "TemporaryDirectory", side_effect=folder),
        ):
            started = time.monotonic()
            self.assertEqual(self.export().status_code, 422)
            self.assertLess(time.monotonic() - started, 5)
        self.assertTrue(processes)
        self.assertTrue(all(p.poll() is not None for p in processes))
        self.assertTrue(all(not Path(p).exists() for p in directories))
        self.assertFalse(Operation.objects.filter(command_type="reporting.export").exists())
        self.assertEqual(self.export().status_code, 200)

    def test_preparation_timeout_does_not_leave_query_running(self):
        command = [
            sys.executable,
            "-c",
            "import json,sys; p=json.load(sys.stdin); "
            "import psycopg; d=p['settings']['DATABASES']['default']; "
            "c=psycopg.connect(dbname=d['NAME'],host=d['HOST'],port=d['PORT'],user=d['USER'],password=d['PASSWORD'],options='-c statement_timeout=150'); "
            "c.execute('SELECT pg_sleep(60)')",
        ]
        with (
            override_settings(REPORTING_EXPORT_TIMEOUT_MS=400),
            patch.object(export_jobs, "worker_command", return_value=command),
        ):
            self.assertEqual(self.export().status_code, 422)
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND state='active' AND query='SELECT pg_sleep(60)'"
            )
            self.assertEqual(cursor.fetchone()[0], 0)

    def test_real_reader_sql_uses_remaining_deadline(self):
        # Injection de latence dans le vrai chemin du lecteur, pas un autre générateur.
        command = [
            sys.executable,
            "-c",
            """from apps.reporting import iteration
original = iteration.stream
def slow(q):
    from django.db import connection
    with connection.cursor() as c:
        c.execute('SELECT pg_sleep(60)')
    yield from original(q)
iteration.stream = slow
from apps.reporting.export_worker import main
main()
""",
        ]
        with (
            override_settings(REPORTING_EXPORT_TIMEOUT_MS=800),
            patch.object(export_jobs, "worker_command", return_value=command),
        ):
            started = time.monotonic()
            self.assertEqual(self.export().status_code, 422)
            self.assertGreater(time.monotonic() - started, 0.6)
        time.sleep(0.15)
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND state='active' AND query='SELECT pg_sleep(60)'"
            )
            self.assertEqual(cursor.fetchone()[0], 0)
        self.assertFalse(Operation.objects.filter(command_type="reporting.export").exists())

    def test_revoked_authorization_after_child_prevents_response(self):
        from apps.accounts.models import User

        original = export_jobs.generate

        def revoke(*args, **kwargs):
            result = original(*args, **kwargs)
            User.objects.filter(pk=self.admin.pk).update(auth_version=self.admin.auth_version + 1)
            return result

        with patch.object(export_jobs, "generate", side_effect=revoke):
            self.assertEqual(self.export().status_code, 401)
        self.assertFalse(Operation.objects.filter(command_type="reporting.export").exists())

    def test_two_real_exports_and_third_refused_then_slot_reused(self):
        self.demandable_product()
        command = [
            sys.executable,
            "-c",
            """from apps.reporting import iteration
+original=iteration.stream
+def slow(q):
+    import time
+    time.sleep(1.5)
+    yield from original(q)
+iteration.stream=slow
+from apps.reporting.export_worker import main
+main()
+""".replace("\n+", "\n"),
        ]
        original = subprocess.Popen
        ready, mutex, processes = threading.Event(), threading.Lock(), []

        def spawn(*args, **kwargs):
            p = original(*args, **kwargs)
            with mutex:
                processes.append(p)
                if len(processes) == 2:
                    ready.set()
            return p

        clients = [self.logged(self.admin), self.logged(self.admin)]

        def run(client):
            close_old_connections()
            try:
                return client.post("/analyses/stocks/export.xlsx")
            finally:
                connection.close()

        before = list(
            __import__(
                "apps.inventory.models", fromlist=["StockBalance"]
            ).StockBalance.objects.values_list("physical", "reserved")
        )
        with (
            patch.object(export_jobs, "worker_command", return_value=command),
            patch.object(export_jobs.subprocess, "Popen", side_effect=spawn),
        ):
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(run, c) for c in clients]
                self.assertTrue(ready.wait(8))
                self.assertEqual(self.export().status_code, 422)
                results = [f.result(timeout=10) for f in futures]
        self.assertEqual([r.status_code for r in results], [200, 200])
        self.assertTrue(all(p.poll() is not None for p in processes))
        self.assertEqual(Operation.objects.filter(command_type="reporting.export").count(), 2)
        self.assertEqual(self.export().status_code, 200)
        self.assertEqual(
            before,
            list(
                __import__(
                    "apps.inventory.models", fromlist=["StockBalance"]
                ).StockBalance.objects.values_list("physical", "reserved")
            ),
        )

    def test_all_non_admin_roles_refused_export(self):
        from apps.accounts.models import User
        from tests.test_accounts import FICTIVE_PASSWORD

        doctor = User.objects.create_user(
            "a14-doctor",
            FICTIVE_PASSWORD,
            role="DOCTOR",
            floor=self.floor,
            must_change_password=False,
        )
        for u in [self.assistant, doctor, self.replenisher]:
            r = self.logged(u).post("/analyses/stocks/export.xlsx")
            self.assertEqual(r.status_code, 403)

    def test_large_paginated_history_not_limited_as_export(self):
        for i in range(3):
            self.create_product(code=f"A14-{i}")
        with override_settings(REPORTING_MAX_ROWS=1, REPORTING_EXPORT_MAX_ROWS=1):
            self.assertEqual(self.admin_client.get("/analyses/history").status_code, 200)
            self.assertEqual(self.export("history").status_code, 422)

    def test_real_export_reader_snapshot_survives_mutation_between_sql_batches(self):
        from xml.etree import ElementTree as ET

        products = [self.create_product(code=f"RR-{i:03}", name=f"RR-{i:03}") for i in range(120)]
        for offset in range(0, 120, 50):
            batch = products[offset : offset + 50]
            self.assertEqual(self.initial(batch, [100] * len(batch)).status_code, 201)
        with tempfile.TemporaryDirectory(prefix="a14-snapshot-") as directory:
            marker = Path(directory) / "reading"
            release = Path(directory) / "continue"
            code = f"""from apps.reporting import iteration
import time
from pathlib import Path
original=iteration.stream
def gated(query):
    for i,row in enumerate(original(query)):
        if i==1:
            Path({str(marker)!r}).touch()
            while not Path({str(release)!r}).exists():
                time.sleep(.01)
            from apps.inventory.models import StockBalance
            if StockBalance.objects.get(product_id={str(products[-1].pk)!r}).physical != 100:
                raise AssertionError("Le lecteur a perdu son snapshot entre deux requêtes SQL")
        yield row
iteration.stream=gated
from apps.reporting.export_worker import main
main()
"""

            def run():
                close_old_connections()
                try:
                    return self.export()
                finally:
                    connection.close()

            with (
                patch.object(
                    export_jobs, "worker_command", return_value=[sys.executable, "-c", code]
                ),
                ThreadPoolExecutor(max_workers=1) as pool,
            ):
                future = pool.submit(run)
                try:
                    until = time.monotonic() + 8
                    while not marker.exists() and time.monotonic() < until:
                        time.sleep(0.02)
                    self.assertTrue(
                        marker.exists(), "Le véritable lecteur n'a pas atteint le premier lot"
                    )
                    r = self.adjust([products[0], products[-1]], [101, 101])
                    self.assertEqual(r.status_code, 200, r.content)
                finally:
                    release.touch()
                response = future.result(timeout=10)
            self.assertEqual(response.status_code, 200, response.content[:200])
            with ZipFile(io.BytesIO(response.content)) as archive:
                root = ET.fromstring(archive.read("xl/worksheets/sheet1.xml"))  # noqa: S314 — XLSX local interne.
            ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
            data = {}
            for row in root.findall(".//" + ns + "row")[7:]:
                cells = ["".join(c.itertext()) for c in row]
                data[cells[0]] = cells[4]
            self.assertEqual(data["RR-000"], "100")
            self.assertEqual(data["RR-119"], "100")
            self.assertEqual(self.stock(products[0])[0], 101)
            self.assertEqual(self.stock(products[-1])[0], 101)
            self.assertEqual(Operation.objects.filter(command_type="reporting.export").count(), 1)


class A14ContentionTests(ReceivingFixtures, TransactionTestCase):
    def test_fifteen_independent_requesters_contend_without_over_reservation(self):
        import uuid

        from apps.accounts.models import User
        from apps.inventory.models import StockBalance
        from apps.requests.models import InternalRequest
        from tests.test_accounts import FICTIVE_PASSWORD

        product = self.demandable_product(physical=12, maximum=12)
        users = [
            User.objects.create_user(
                f"a14-concurrent-{i}",
                FICTIVE_PASSWORD,
                role="ASSISTANT" if i % 2 == 0 else "DOCTOR",
                floor=self.floor,
                must_change_password=False,
            )
            for i in range(15)
        ]
        clients = [self.logged(u) for u in users]
        barrier = threading.Barrier(15)
        body = {"lines": [{"product_id": str(product.pk), "quantity": 12}]}
        keys = [(uuid.uuid4(), uuid.uuid4().hex) for _ in clients]

        def run(i):
            close_old_connections()
            try:
                barrier.wait(timeout=15)
                return self.post_json(
                    "/api/v1/requests", body, clients[i], identifier=keys[i][0], key=keys[i][1]
                )
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=15) as pool:
            results = list(pool.map(run, range(15)))
        self.assertEqual(sum(r.status_code == 201 for r in results), 1)
        self.assertEqual(sum(r.status_code == 409 for r in results), 14)
        balance = StockBalance.objects.get(product=product)
        self.assertEqual((balance.physical, balance.reserved, balance.available), (12, 12, 0))
        self.assertEqual(InternalRequest.objects.count(), 1)
        winner = next(i for i, r in enumerate(results) if r.status_code == 201)
        replay = self.post_json(
            "/api/v1/requests",
            body,
            clients[winner],
            identifier=keys[winner][0],
            key=keys[winner][1],
        )
        self.assertEqual(replay.json()["operation_id"], results[winner].json()["operation_id"])
        self.assertEqual(InternalRequest.objects.count(), 1)

    def test_double_purchase_confirmation_is_one_order_and_one_purchase_event(self):
        import uuid

        from apps.notifications.models import BusinessEvent
        from apps.purchasing.models import PurchaseOrder

        product = self.demandable_product(physical=100, maximum=100)
        body = self.purchase_body([product], [12], external_reference="A14-fictive-distinct")
        identifier, key = uuid.uuid4(), uuid.uuid4().hex
        first = self.post_json("/api/v1/orders", body, identifier=identifier, key=key)
        second = self.post_json("/api/v1/orders", body, identifier=identifier, key=key)
        self.assertEqual(first.status_code, 201)
        self.assertEqual(first.json(), second.json())
        self.assertEqual(PurchaseOrder.objects.count(), 1)
        self.assertEqual(Operation.objects.filter(command_type="orders.create").count(), 1)
        self.assertEqual(
            BusinessEvent.objects.filter(
                operation_id=first.json()["operation_id"], event_type="purchase.recorded"
            ).count(),
            1,
        )
        self.assertEqual(self.stock(product)[:3], (100, 0, 100))

    def test_two_simultaneous_receipt_replays_credit_twelve_once(self):
        import uuid

        from apps.receiving.models import Receipt

        product = self.demandable_product(physical=100, maximum=100)
        order = self.buy([product], [12])
        body = self.receiving_body(order, [12])
        identifier, key = uuid.uuid4(), uuid.uuid4().hex
        clients = [self.logged(self.replenisher), self.logged(self.replenisher)]
        barrier = threading.Barrier(2)

        def run(i):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                return self.post_json(
                    f"/api/v1/orders/{order.pk}/receipts",
                    body,
                    clients[i],
                    identifier=identifier,
                    key=key,
                )
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            result = list(pool.map(run, range(2)))
        self.assertEqual([r.status_code for r in result], [201, 201])
        self.assertEqual(result[0].json()["operation_id"], result[1].json()["operation_id"])
        self.assertEqual(Receipt.objects.count(), 1)
        self.assertEqual(self.stock(product)[:3], (112, 0, 112))
        line = order.lines.get()
        self.assertEqual(
            (line.quantity, line.received, line.cancelled, line.waiting), (12, 12, 0, 0)
        )


class A14InstrumentationTests(TransactionTestCase):
    def test_named_cursor_fetch_is_included_in_sql_measurement(self):
        import importlib

        from django.db.backends.utils import CursorWrapper
        from django.http import HttpResponse
        from django.template.base import Template

        original_getattr, original_render = CursorWrapper.__getattr__, Template.render
        try:
            lab = importlib.import_module("tests.support.sql_instrumentation")
            CursorWrapper.__getattr__ = lab.cursor_attribute
            Template.render = lab.timed_render

            def response(request):
                from django.db import transaction

                with transaction.atomic(), connection.chunked_cursor() as cursor:
                    cursor.execute("SELECT i, pg_sleep(.05) FROM generate_series(1,2) i")
                    self.assertEqual(len(cursor.fetchmany(2)), 2)
                return HttpResponse("ok")

            result = lab.TimingMiddleware(response)(None)
            self.assertGreaterEqual(float(result["X-Test-sql-fetch-ms"]), 80)
            self.assertGreaterEqual(float(result["X-Test-queries"]), 2)
        finally:
            CursorWrapper.__getattr__ = original_getattr
            Template.render = original_render
