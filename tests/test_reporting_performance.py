"""Contrats PERF-M10 : exactitude, lecture bornée et croissance SQL stable.

Aucun seuil de temps dépendant du matériel ; les durées sont des benchmarks.
"""

import io
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from unittest.mock import patch
from zipfile import ZipFile

from django.db import close_old_connections, connection
from django.test import TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext

from apps.accounts.models import User
from apps.core.errors import BusinessError
from apps.core.models import Operation
from apps.notifications.models import BusinessEvent, Outbox
from apps.receiving.models import QuarantineItem
from apps.receiving.state import item_state
from apps.reporting import exports, selectors, statistics
from apps.reporting.filters import parse
from apps.reporting.snapshots import snapshot
from tests.support.benchmark_names import valid_reporting_name
from tests.test_notifications import NotificationFixtures


class ReportingPerformanceTests(NotificationFixtures, TransactionTestCase):
    def data(self, name, **params):
        return list(selectors.rows(self.admin, name, parse(params)))

    def count_rows_queries(self, name):
        with CaptureQueriesContext(connection) as queries:
            self.data(name)
        return len(queries)

    def rich_facts(self):
        request, products, delivery, case = self.completed(physical=30)
        self.correct(delivery, case, [8])
        self.correct(delivery, case, [9])
        self.create_product(code="AUTRE-UNITE", unit="boîte")
        order = self.buy(products, [10], external_reference="PERF-FICTIF")
        self.assertEqual(self.receive(order, [12], [1]).status_code, 201)
        item = QuarantineItem.objects.get(kind="SURPLUS")
        self.assertEqual(
            self.post_json(
                f"/api/v1/quarantine/{item.pk}/accept-surplus",
                {
                    "expected_version": item_state(item.pk)["version"],
                    "quantity": 2,
                    "verification": True,
                    "expected_stock_version": self.stock(products[0])[3],
                },
            ).status_code,
            200,
        )
        self.unknown()
        return request, products, delivery

    def test_stock_queries_do_not_grow_per_product(self):
        self.create_product(code="PERF-0")
        small = self.count_rows_queries("stocks")
        for i in range(1, 21):
            self.create_product(code=f"PERF-{i}")
        self.assertLessEqual(self.count_rows_queries("stocks"), small + 1)

    def test_history_queries_batched_not_one_lookup_per_catalogue_operation(self):
        self.create_product(code="PERF-0")
        small = self.count_rows_queries("history")
        for i in range(1, 21):
            self.create_product(code=f"PERF-{i}")
        self.assertLessEqual(self.count_rows_queries("history"), small + 2)

    def test_history_http_selects_only_fifty_objects(self):
        for i in range(65):
            self.create_product(code=f"PERF-{i}")
        with CaptureQueriesContext(connection) as queries:
            response = self.admin_client.get("/analyses/history")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["rows"]), 50)
        reads = [
            q["sql"]
            for q in queries
            if 'FROM "core_operation"' in q["sql"]
            and "COUNT(" not in q["sql"]
            and "DISTINCT" not in q["sql"]
        ]
        self.assertTrue(reads)
        self.assertTrue(all("LIMIT 50" in sql for sql in reads), reads)

    def test_history_pages_equal_reference_slices_without_gaps(self):
        for i in range(60):
            self.create_product(code=f"PERF-{i}")
        expected = self.data("history")
        pages = [self.admin_client.get("/analyses/history", {"page": page}) for page in [1, 2]]
        actual = [row["url"] for response in pages for row in response.context["rows"]]
        self.assertEqual(actual, [row["url"] for row in expected])
        self.assertEqual(pages[0].context["count"], len(expected))

    def test_all_datasets_match_their_declared_public_schema(self):
        self.rich_facts()
        for name, (title, columns) in selectors.SCHEMAS.items():
            with self.subTest(name=name):
                self.assertTrue(title)
                rows = self.data(name)
                self.assertTrue(all(len(row["cells"]) == len(columns) for row in rows))
                self.assertTrue(all(row.get("url") for row in rows))

    def test_dashboard_counts_each_open_case_once_by_primary_category(self):
        self.rich_facts()
        dashboard = statistics.dashboard(self.admin)
        cases = self.data("cases")
        open_reports = {
            row["url"]
            for row in cases
            if row["cells"][3] == "Signalements"
            and row["cells"][4] in {"Ouvert", "En vérification"}
        }
        self.assertEqual(dashboard["counts"]["reports"], len(open_reports))
        self.assertEqual(dashboard["counts"]["reports"], 2)

    def test_xlsx_contains_current_rows_and_required_sheets_for_every_dataset(self):
        self.rich_facts()
        metadata = {"extracted": datetime(2026, 11, 1, tzinfo=UTC)}
        for name in selectors.SCHEMAS:
            data = self.data(name)
            with ZipFile(io.BytesIO(exports.workbook(name, data, parse({}), metadata))) as book:
                self.assertIn("xl/workbook.xml", book.namelist())
                sheet = book.read("xl/worksheets/sheet1.xml").decode()
                rows = sheet.count("<row ") + sheet.count("<row>")
                self.assertEqual(rows, 7 + len(data), name)
                self.assertEqual(
                    len([entry for entry in book.namelist() if entry.startswith("xl/worksheets/")]),
                    2
                    if name in {"stocks", "requests", "deliveries", "receipts", "purchases"}
                    else 1,
                )

    def test_successive_corrections_remain_at_origin_and_deltas_in_history(self):
        with patch(
            "apps.core.idempotency.database_now", return_value=datetime(2026, 10, 5, 9, tzinfo=UTC)
        ):
            _, _, delivery, case = self.completed(physical=30)
        with patch(
            "apps.core.idempotency.database_now", return_value=datetime(2026, 10, 10, 9, tzinfo=UTC)
        ):
            self.correct(delivery, case, [8])
        with patch(
            "apps.core.idempotency.database_now", return_value=datetime(2026, 10, 12, 9, tzinfo=UTC)
        ):
            self.correct(delivery, case, [9])
        f = {"start": "2026-10-01", "end": "2026-11-01"}
        row = self.data("deliveries", **f)[0]
        self.assertEqual((row["cells"][0].day, row["cells"][8]), (5, 9))
        corrections = [
            r["cells"] for r in self.data("history", **f) if r["cells"][3] == "Correction confirmée"
        ]
        self.assertEqual([r[0].day for r in corrections], [12, 10])
        self.assertIn("différence +1", corrections[0][5])
        self.assertIn("différence -2", corrections[1][5])

    def test_history_permissions_and_personal_scope_for_four_roles(self):
        self.rich_facts()
        doctor = User.objects.create_user(
            "medecin-perf",
            "Mot de passe fictif long !",
            role="DOCTOR",
            display_name="Demandeur B fictif",
            floor=self.other_floor,
            must_change_password=False,
        )
        for actor in [self.admin, self.assistant, doctor, self.replenisher]:
            personal = list(selectors.rows(actor, "history", parse({}), True))
            self.assertTrue(all(row.get("url") for row in personal))
            if actor.role != "ADMIN":
                with self.assertRaises(BusinessError):
                    list(selectors.rows(actor, "history", parse({})))

    def test_history_search_and_role_filters_return_matching_rows(self):
        request, _, _ = self.rich_facts()
        period = {"start": "2026-10-01", "end": "2026-12-01"}
        search = self.data("history", q=request.reference, **period)
        self.assertTrue(search)
        self.assertTrue(any(request.reference in str(row["cells"]) for row in search))
        by_role = self.data("history", role="ADMIN", **period)
        self.assertTrue(by_role)
        self.assertTrue(all(row["cells"][2] == "Gestionnaire" for row in by_role))
        corrections = self.data("history", action="corrections.confirm", **period)
        self.assertTrue(corrections)
        self.assertTrue(all("Correction" in row["cells"][3] for row in corrections))

    def test_history_limit_preserved_without_materializing_all_rows(self):
        for i in range(4):
            self.create_product(code=f"PERF-{i}")
        with override_settings(REPORTING_MAX_ROWS=1, REPORTING_EXPORT_MAX_ROWS=1):
            # A14 : limite de génération distincte de la consultation paginée.
            self.assertEqual(self.admin_client.get("/analyses/history").status_code, 200)
            self.assertEqual(
                self.admin_client.post("/analyses/history/export.xlsx").status_code, 422
            )
        self.assertFalse(Operation.objects.filter(command_type="reporting.export").exists())

    def test_read_paths_do_not_mutate_facts_or_stock(self):
        _, products, _ = self.rich_facts()
        before = (Operation.objects.count(), Outbox.objects.count(), self.stock(products[0]))
        for url in [
            "/analyses/stocks",
            "/analyses/history",
            "/tableau-de-bord",
            "/historique-personnel",
        ]:
            self.assertEqual(self.admin_client.get(url).status_code, 200)
        self.assertEqual(
            (Operation.objects.count(), Outbox.objects.count(), self.stock(products[0])), before
        )

    def test_snapshot_pagination_count_and_rows_ignore_concurrent_insert(self):
        self.create_product(code="PERF-0")

        def writer():
            close_old_connections()
            try:
                self.create_product(code="PERF-OTHER-CONNECTION")
            finally:
                connection.close()

        with snapshot():
            count = selectors.history_query(self.admin, parse({})).count()
            with ThreadPoolExecutor(max_workers=1) as pool:
                pool.submit(writer).result(timeout=20)
            self.assertEqual(len(self.data("history")), count)
        self.assertEqual(len(self.data("history")), count + 1)

    def test_benchmark_name_guard_accepts_only_fictitious_prefix(self):
        for value in [
            "stock_app_example",
            "production",
            "test_stock_app_default",
            "bench_perf_m10_x;DROP DATABASE x",
        ]:
            with self.assertRaises(ValueError):
                valid_reporting_name(value)
        self.assertEqual(valid_reporting_name("bench_perf_m10_fictive"), "bench_perf_m10_fictive")

    def test_dashboard_query_budget_with_need_and_shortage(self):
        self.demandable_product(physical=0)
        with CaptureQueriesContext(connection) as queries:
            counts = statistics.dashboard(self.admin)["counts"]
        self.assertEqual((counts["needs"], counts["shortages"]), (1, 1))
        self.assertLessEqual(len(queries), 9)

    def test_history_explain_analyze_is_readonly_and_bounded(self):
        self.create_product(code="PLAN-FICTIF")
        before = (Operation.objects.count(), Outbox.objects.count())
        with snapshot():
            result = json.loads(
                selectors.history_query(self.admin, parse({}))[:50].explain(
                    format="json",
                    analyze=True,
                    buffers=True,
                )
            )[0]
        self.assertLessEqual(result["Plan"]["Actual Rows"], 50)
        self.assertIn("Shared Hit Blocks", result["Plan"])
        self.assertIn("Execution Time", result)
        self.assertEqual((Operation.objects.count(), Outbox.objects.count()), before)

    def test_stock_sql_pagination_keeps_full_filtered_indicators(self):
        first = self.demandable_product(code="PERF-KNOWN", physical=10)
        self.assertEqual(self.send([first], [4]).status_code, 201)
        self.demandable_product(code="PERF-ZERO", physical=0)
        for i in range(52):
            self.create_product(code=f"PERF-UNKNOWN-{i}")
        for params in [
            {},
            {"q": "PERF-ZERO"},
            {"state": "ACTIVE"},
            {"state": "NOT_INVENTORIED"},
            {"state": "shortage"},
            {"product": str(first.pk)},
        ]:
            expected = self.data("stocks", **params)
            with CaptureQueriesContext(connection) as queries:
                response = self.admin_client.get("/analyses/stocks", params)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.context["count"], len(expected))
            self.assertEqual(
                response.context["indicators"], statistics.indicators("stocks", expected)
            )
            self.assertEqual(
                [r["url"] for r in response.context["rows"]], [r["url"] for r in expected[:50]]
            )
            reads = [
                q["sql"]
                for q in queries
                if 'FROM "inventory_stockbalance"' in q["sql"] and "COUNT(" not in q["sql"]
            ]
            self.assertTrue(reads)
            self.assertTrue(all("LIMIT 50" in statement for statement in reads))
        last = self.admin_client.get("/analyses/stocks", {"page": 2})
        self.assertEqual(len(last.context["rows"]), 4)

    def test_stock_sql_counts_and_page_share_repeatable_snapshot(self):
        product = self.demandable_product(physical=10)

        def writer():
            close_old_connections()
            try:
                return self.adjust([product], [0]).status_code
            finally:
                connection.close()

        with snapshot():
            selected, count, counts = selectors.stock_page(self.admin, parse({}))
            with ThreadPoolExecutor(max_workers=1) as pool:
                self.assertEqual(pool.submit(writer).result(timeout=20), 200)
            self.assertEqual((count, counts["above"], list(selected)[0]["cells"][4]), (1, 1, 10))
        selected, count, counts = selectors.stock_page(self.admin, parse({}))
        self.assertEqual((count, counts["shortage"], list(selected)[0]["cells"][4]), (1, 1, 0))

    def test_reactivation_index_is_valid_and_keeps_real_a11_trigger_visible(self):
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_get_indexdef(indexrelid), indisvalid FROM pg_index "
                "WHERE indexrelid=to_regclass(%s)",
                ["notif_stock_reactivation_idx"],
            )
            definition, valid = cursor.fetchone()
        self.assertTrue(valid)
        self.assertIn("(aggregate_id, revision)", definition)
        self.assertIn("recommendation.stock_reactivated", definition)
        product = self.demandable_product(physical=10)
        self.assertEqual(self.modify(product, minimum=10, target=20).status_code, 200)
        self.assertEqual(self.rec_action(product, "defer").status_code, 200)
        self.assertEqual(self.adjust([product], [9]).status_code, 200)
        self.assertEqual(
            BusinessEvent.objects.filter(
                event_type="recommendation.stock_reactivated", aggregate_id=product.pk
            ).count(),
            1,
        )
        self.assertEqual(statistics.dashboard(self.admin)["counts"]["needs"], 1)
