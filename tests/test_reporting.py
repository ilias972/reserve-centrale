"""M10 : services réels, PostgreSQL, OOXML et permissions serveur."""

import io
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from django.db import DatabaseError, close_old_connections, connection
from django.test import TransactionTestCase, override_settings

from apps.accounts.models import User
from apps.audit.models import AuditEvent
from apps.core.errors import BusinessError
from apps.core.models import Operation
from apps.inventory.models import StockEntry
from apps.notifications.models import Outbox
from apps.receiving.models import QuarantineItem, Receipt
from apps.reporting import exports, selectors, statistics
from apps.reporting.filters import PARIS, parse
from apps.reporting.snapshots import materialize, snapshot
from tests.test_notifications import NotificationFixtures
from tests.test_receiving import ReceivingFixtures

ROOT = Path("docs/recette/M10")
NS = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def sheet(content):
    with ZipFile(io.BytesIO(content)) as z:
        assert z.testzip() is None
        return ET.fromstring(z.read("xl/worksheets/sheet1.xml"))  # noqa: S314 — XML généré localement


class ReportingTests(NotificationFixtures, TransactionTestCase):
    def filters(self, **extra):
        return parse(extra)

    def data(self, name, **params):
        return list(selectors.rows(self.admin, name, self.filters(**params)))

    def export(self, name="stocks", query=""):
        return self.admin_client.post("/analyses/" + name + "/export.xlsx" + query)

    def test_dashboard_counts_needs_and_covered_shortage(self):
        p = self.demandable_product(physical=0)
        self.buy([p], [20])
        d = statistics.dashboard(self.admin)
        self.assertEqual(
            (d["counts"]["needs"], d["counts"]["shortages"], d["counts"]["incomplete"]), (0, 1, 1)
        )
        self.assertEqual(d["shortages"][0]["waiting"], 20)
        self.assertContains(self.admin_client.get("/tableau-de-bord"), "Rupture de stock")

    def test_deferred_shortage_is_not_active_need(self):
        p = self.demandable_product(physical=0)
        self.rec_action(p, "defer")
        d = statistics.dashboard(self.admin)
        self.assertEqual((d["counts"]["needs"], d["counts"]["shortages"]), (0, 1))

    def test_dashboard_distinct_cases_despite_notifications(self):
        self.unknown()
        before = statistics.dashboard(self.admin)["counts"]
        self.process()
        self.process()
        self.assertEqual(statistics.dashboard(self.admin)["counts"], before)
        self.assertEqual(before["reports"], 1)

    def test_incomplete_order_count_once_for_two_lines(self):
        p = self.demandable_product(code="A")
        p2 = self.demandable_product(code="B")
        self.buy([p, p2], [4, 5])
        self.assertEqual(statistics.dashboard(self.admin)["counts"]["incomplete"], 1)
        self.assertEqual(len(self.data("orderlines", state="incomplete")), 2)

    def test_every_dataset_screen_executes(self):
        self.demandable_product()
        for name in selectors.SCHEMAS:
            r = self.admin_client.get("/analyses/" + name)
            self.assertEqual(r.status_code, 200, (name, r.content[:200]))

    def test_current_stock_prd_and_unknown(self):
        p = self.demandable_product(physical=10)
        self.send([p], [4])
        self.create_product(code="NOT-COUNTED")
        data = self.data("stocks")
        known = next(r for r in data if r["product_id"] == str(p.pk))
        self.assertEqual(known["cells"][4:7], [10, 4, 6])
        unknown = next(r for r in data if r["product_id"] != str(p.pk))
        self.assertIsNone(unknown["cells"][4])
        self.assertIsNone(unknown["cells"][6])

    def test_request_is_not_delivery(self):
        self.requested((3,))
        self.assertEqual(self.data("requests")[0]["cells"][8:12], [3, 0, 0, 3])
        self.assertEqual(self.data("deliveries"), [])

    def test_request_role_and_floor_snapshot(self):
        request, ps = self.requested((3,))
        User.objects.filter(pk=self.assistant.pk).update(role="DOCTOR", floor=self.other_floor)
        r = self.data("requests")[0]["cells"]
        self.assertEqual((r[3], r[4]), ("Demandeur — profil A", request.floor_name))
        self.assertEqual(len(self.data("requests", floor=str(self.floor.pk), role="ASSISTANT")), 1)
        self.assertEqual(self.data("requests", floor=str(self.other_floor.pk)), [])

    def test_doctor_request_once(self):
        User.objects.filter(pk=self.assistant.pk).update(role="DOCTOR")
        self.assistant.refresh_from_db()
        self.user_client = self.logged(self.assistant)
        self.requested((3,))
        r = self.data("requests")
        self.assertEqual((len(r), r[0]["cells"][3]), (1, "Demandeur — profil B"))

    def test_manual_adjustment_no_invented_floor(self):
        p = self.demandable_product()
        self.adjust([p], [8])
        rows = self.data("movements")
        self.assertTrue(any(r["cells"][4] == "Ajustement ordinaire" for r in rows))
        self.assertEqual(self.data("movements", floor=str(self.floor.pk)), [])

    def test_delivery_successive_corrections_and_original_retained(self):
        _, _, d, c = self.completed()
        self.correct(d, c, [8])
        self.assertEqual(self.data("deliveries")[0]["cells"][7:9], [10, 8])
        c.refresh_from_db()
        self.post_json(
            f"/api/v1/cases/{c.pk}/review",
            {
                "expected_version": c.version,
                "verification": True,
                "message": "Nouvelle vérification fictive.",
            },
        )
        self.correct(d, c, [9])
        self.assertEqual(self.data("deliveries")[0]["cells"][7:9], [10, 9])
        self.assertEqual(d.lines.get().handed, 10)
        self.assertEqual(
            statistics.summarize("deliveries", self.data("deliveries"))[0]["quantity"], 9
        )

    def test_correction_year_boundary_origin_and_audit_day(self):
        with patch(
            "apps.core.idempotency.database_now",
            return_value=datetime(2025, 12, 31, 22, tzinfo=UTC),
        ):
            _, _, d, c = self.completed()
        self.correct(d, c, [8])
        r = self.data("deliveries", start="2025-12-31", end="2026-01-01")
        self.assertEqual(r[0]["cells"][8], 8)
        self.assertEqual(self.data("deliveries", start="2026-01-01", end="2026-02-01"), [])
        self.assertTrue(any(r["cells"][3] == "Correction confirmée" for r in self.data("history")))

    def test_receipt_correction_delta_and_surplus_separate(self):
        p = self.demandable_product(physical=0)
        o = self.buy([p], [10])
        self.receive(o, [10])
        receipt = Receipt.objects.get()
        case = self.receipt_case(receipt)
        body = ReceivingFixtures.correction_body(self, receipt, case, [8])
        preview = self.post_json("/api/v1/corrections/preview", body)
        self.assertEqual(preview.status_code, 200, preview.content)
        body["preview_hash"] = preview.json()["preview"]["preview_hash"]
        self.assertEqual(self.post_json("/api/v1/corrections/confirm", body).status_code, 200)
        self.assertEqual(self.data("receipts")[0]["cells"][7:9], [10, 8])
        self.assertEqual(self.data("surplus"), [])

    def test_faw_and_optional_money_not_zero(self):
        p = self.demandable_product(physical=0)
        o = self.buy([p], [20])
        self.receive(o, [12])
        self.cancel_remainder(o, 8)
        self.assertEqual(self.data("orderlines")[0]["cells"][6:10], [20, 12, 8, 0])
        self.assertIsNone(self.data("purchases")[0]["cells"][4])

    def test_different_currencies_not_merged(self):
        p = self.demandable_product()
        self.buy([p], [1], external_reference="ONE", amount="12.30", currency="EUR")
        self.buy([p], [2], external_reference="TWO", amount="14.00", currency="USD")
        summary = statistics.summarize("purchases", self.data("purchases"))
        self.assertEqual(
            {x["currency"]: x["amount"] for x in summary},
            {"EUR": Decimal("12.30"), "USD": Decimal("14.00")},
        )

    def test_product_units_not_summed(self):
        r, ps = self.prepare((2, 3))
        self.deliver(r)
        groups = statistics.summarize("deliveries", self.data("deliveries"))
        self.assertEqual(len(groups), 2)
        self.assertEqual(sorted(x["quantity"] for x in groups), [2, 3])
        self.assertEqual(
            statistics.chart("deliveries", self.data("deliveries"), self.filters()), []
        )

    def test_product_selected_chart_has_correct_values(self):
        r, ps = self.prepare((3,))
        self.deliver(r)
        f = self.filters(product=str(ps[0].pk))
        chart = statistics.chart("deliveries", self.data("deliveries"), f)
        self.assertEqual(chart[0]["quantity"], 3)

    def test_timezone_and_dst_intervals(self):
        for day, end, hours in [("2026-03-29", "2026-03-30", 23), ("2026-10-25", "2026-10-26", 25)]:
            f = self.filters(start=day, end=end)
            self.assertEqual(
                (f.upper.astimezone(UTC) - f.lower.astimezone(UTC)).total_seconds() / 3600, hours
            )
        self.assertEqual(
            datetime(2026, 1, 31, 23, 30, tzinfo=UTC).astimezone(PARIS).date(), date(2026, 2, 1)
        )

    def test_exclusive_end(self):
        with patch(
            "apps.core.idempotency.database_now", return_value=datetime(2026, 1, 31, 23, tzinfo=UTC)
        ):
            self.completed()
        self.assertEqual(self.data("deliveries", start="2026-01-01", end="2026-02-01"), [])
        self.assertEqual(len(self.data("deliveries", start="2026-02-01", end="2026-02-02")), 1)

    def test_filters_reject_unknown_duplicate_invalid_and_excessive(self):
        for query in [
            "secret=1",
            "start=2025-01-01&end=2026-10-01",
            "page=999999",
            "product=bad",
            "q=x&q=y",
            "state=INVENTED",
            "floor=" + str(uuid.uuid4()),
        ]:
            self.assertEqual(
                self.admin_client.get("/analyses/stocks?" + query).status_code, 400, query
            )

    def test_all_three_non_admin_roles_denied_pages_and_exports(self):
        for role in ["ASSISTANT", "DOCTOR", "REPLENISHER"]:
            u = User.objects.create_user(
                "role-" + role,
                "fictive-password-long",
                role=role,
                display_name=role,
                must_change_password=False,
                floor=self.floor if role != "REPLENISHER" else None,
            )
            client = self.logged(u)
            for path in ["/tableau-de-bord", "/analyses/stocks", "/analyses/history"]:
                self.assertEqual(client.get(path).status_code, 403, (role, path))
            for name in selectors.SCHEMAS:
                self.assertEqual(client.post("/analyses/" + name + "/export.xlsx").status_code, 403)
            with self.assertRaises(BusinessError):
                list(selectors.rows(u, "stocks", self.filters()))

    def test_personal_requester_no_other_accounts_or_stock(self):
        request, ps = self.requested((3,))
        page = self.user_client.get("/historique-personnel")
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, request.reference)
        self.assertNotContains(page, "Stock avant")
        self.assertNotContains(page, "Exporter ces données")
        self.assertEqual(
            self.user_client.get("/historique-personnel?actor=" + str(self.admin.pk)).status_code,
            400,
        )

    def test_personal_replenisher_own_only(self):
        self.demandable_product()
        data = list(selectors.rows(self.replenisher, "history", self.filters(), True))
        self.assertTrue(data)
        self.assertTrue(all(r["cells"][1] == self.replenisher.display_name for r in data))
        self.assertTrue(all(r["cells"][5] == "" for r in data))

    def test_read_queries_no_stock_or_events_mutated(self):
        p = self.demandable_product()
        before = (self.stock(p), Operation.objects.count(), Outbox.objects.count())
        self.admin_client.get("/tableau-de-bord")
        self.admin_client.get("/analyses/stocks")
        self.assertEqual((self.stock(p), Operation.objects.count(), Outbox.objects.count()), before)

    def test_snapshot_is_readonly_repeatable_with_distinct_connection(self):
        p = self.demandable_product(physical=10)

        def mutate():
            close_old_connections()
            try:
                with connection.cursor() as c:
                    c.execute("SELECT pg_backend_pid()")
                    pid = c.fetchone()[0]
                response = self.adjust([p], [8])
                return pid, response.status_code
            finally:
                connection.close()

        with snapshot():
            first = self.data("stocks")[0]["cells"][4]
            with connection.cursor() as c:
                c.execute("SELECT pg_backend_pid()")
                reader = c.fetchone()[0]
            with ThreadPoolExecutor(max_workers=1) as pool:
                writer, status = pool.submit(mutate).result(timeout=20)
            self.assertNotEqual(reader, writer)
            self.assertEqual(status, 200)
            self.assertEqual(self.data("stocks")[0]["cells"][4], first)
        self.assertEqual(self.data("stocks")[0]["cells"][4], 8)
        (ROOT / "snapshot-concurrency.json").write_text(
            __import__("json").dumps(
                {
                    "reader_pid": reader,
                    "writer_pid": writer,
                    "snapshot_physical": first,
                    "after_commit": 8,
                }
            )
        )

    def test_readonly_database_prevents_writes(self):
        with self.assertRaises(DatabaseError), snapshot():
            User.objects.filter(pk=self.admin.pk).update(display_name="Forbidden")

    def test_export_real_xlsx_metadata_dates_numbers_null(self):
        self.demandable_product(physical=10)
        response = self.export()
        self.assertEqual(response.status_code, 200, response.content[:200])
        root = sheet(response.content)
        self.assertEqual(root.findall(".//s:f", NS), [])
        self.assertIn("Europe/Paris", response.content and "".join(root.itertext()))
        self.assertEqual(
            root.findall("s:sheetData/s:row", NS)[7].findall("s:c", NS)[4].find("s:v", NS).text,
            "10",
        )
        (ROOT / "stocks-exemple.xlsx").write_bytes(response.content)
        self.assertEqual(len(selectors.SCHEMAS["stocks"][1]), 10)

    def test_every_export_valid_and_typed_no_secret(self):
        self.completed()
        for name in selectors.SCHEMAS:
            r = self.export(name)
            self.assertEqual(r.status_code, 200, (name, r.content[:200]))
            folder = ROOT / "export-samples"
            folder.mkdir(exist_ok=True)
            (folder / (name + ".xlsx")).write_bytes(r.content)
            xml = sheet(r.content)
            self.assertEqual(xml.findall(".//s:f", NS), [])
            text = "".join(xml.itertext())
            for secret in ["password_hash", "session_key", "payload_hash", "vapid", "argon2$"]:
                self.assertNotIn(secret, text)

    def test_export_audited_after_file_generated_no_stock(self):
        p = self.demandable_product()
        before = self.stock(p)
        count = StockEntry.objects.count()
        response = self.export()
        self.assertEqual(response.status_code, 200)
        event = AuditEvent.objects.get(operation__command_type="reporting.export")
        self.assertEqual(event.after["event"], "EXPORT_GENERATED")
        self.assertEqual(event.after["sha256"], response["X-Export-SHA256"])
        self.assertNotIn("proof", event.after)
        self.assertEqual((self.stock(p), StockEntry.objects.count()), (before, count))

    def test_export_failure_not_audited_as_success(self):
        with patch("apps.reporting.export_jobs.generate", side_effect=OSError("test failure")):
            self.assertEqual(self.export().status_code, 500)
        self.assertFalse(Operation.objects.filter(command_type="reporting.export").exists())

    def test_row_limit_rejects_entire_export(self):
        self.demandable_product(code="FIRST")
        self.demandable_product(code="SECOND")
        with override_settings(REPORTING_EXPORT_MAX_ROWS=1):
            r = self.export()
            self.assertEqual(r.status_code, 422)
        self.assertFalse(Operation.objects.filter(command_type="reporting.export").exists())

    def test_file_size_limit_rejects_without_audit(self):
        with override_settings(REPORTING_MAX_BYTES=100):
            self.assertEqual(self.export().status_code, 422)
        self.assertFalse(Operation.objects.filter(command_type="reporting.export").exists())

    def test_formula_injection_controls_and_prefixes_are_text(self):
        bad = [
            "=1+1",
            " +SUM(A1:A2)",
            "\t@cmd",
            "\ufeff-2+3",
            '\x01=HYPERLINK("evil")',
            "\u200b=cmd",
        ]
        for value in bad:
            xml = ET.fromstring(exports.cell(value))  # noqa: S314 — XML généré localement
            self.assertEqual(xml.attrib["t"], "inlineStr")
            self.assertTrue("".join(xml.itertext()).startswith("'"))
        self.assertEqual(exports.cell(None), "<c/>")
        self.assertIn("<v>-2</v>", exports.cell(-2))

    def test_export_matches_exact_product_filter(self):
        p = self.demandable_product(code="FIRST")
        self.demandable_product(code="SECOND")
        r = self.export(query="?product=" + str(p.pk))
        text = "".join(sheet(r.content).itertext())
        self.assertIn("FIRST", text)
        self.assertNotIn("SECOND", text)

    def test_old_export_remains_independent_after_correction(self):
        _, _, d, c = self.completed()
        first = self.export("deliveries").content
        self.correct(d, c, [8])
        second = self.export("deliveries").content
        self.assertNotEqual(first, second)

        def delivered(data):
            return (
                sheet(data)
                .findall("s:sheetData/s:row", NS)[7]
                .findall("s:c", NS)[8]
                .find("s:v", NS)
                .text
            )

        self.assertEqual((delivered(first), delivered(second)), ("10", "8"))

    def test_no_unauthenticated_export(self):
        from django.test import Client

        self.assertIn(Client().post("/analyses/stocks/export.xlsx").status_code, [302, 401])

    def test_forged_export_audit_is_rejected(self):
        r = self.post_json("/api/v1/operation-intents", {})
        self.assertGreaterEqual(r.status_code, 400)
        with self.assertRaises(BusinessError):
            exports.apply_export(None, None, {})

    def test_role_change_rechecked_before_file_response(self):
        # Révocation réelle intercalée après snapshot, avant audit/retour fichier.
        from apps.reporting.export_jobs import generate

        original = generate

        def revoke(*args, **kwargs):
            content = original(*args, **kwargs)
            User.objects.filter(pk=self.admin.pk).update(auth_version=self.admin.auth_version + 1)
            return content

        with patch("apps.reporting.export_jobs.generate", side_effect=revoke):
            self.assertEqual(self.export().status_code, 401)
        self.assertFalse(Operation.objects.filter(command_type="reporting.export").exists())

    def test_existing_signalements_tabs_preserved(self):
        r = self.admin_client.get("/stocks/signalements")
        self.assertContains(r, "Stock de la réserve")
        self.assertContains(r, "Remises aux unités")

    def test_surplus_acceptance_not_counted_in_regular_receipts(self):
        from apps.receiving.state import item_state

        p = self.demandable_product(physical=0)
        o = self.buy([p], [20])
        self.receive(o, [23])
        item = QuarantineItem.objects.get(kind="SURPLUS")
        r = self.post_json(
            f"/api/v1/quarantine/{item.pk}/accept-surplus",
            {
                "expected_version": item_state(item.pk)["version"],
                "quantity": 3,
                "verification": True,
                "expected_stock_version": self.stock(p)[3],
            },
        )
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(self.data("receipts")[0]["cells"][8], 20)
        self.assertEqual(self.data("surplus")[0]["cells"][4], 3)
        self.assertEqual(statistics.dashboard(self.admin)["counts"]["verify"], 0)

    def test_category_filtered_open_cases_match_counters(self):
        self.completed()
        self.unknown()
        counts = statistics.dashboard(self.admin)["counts"]
        self.assertEqual((counts["verify"], counts["reports"]), (1, 1))
        self.assertEqual(len(self.data("cases", category="TO_VERIFY", state="pending")), 1)
        self.assertEqual(len(self.data("cases", category="REPORTS", state="pending")), 1)

    def test_old_open_case_remains_in_current_dashboard_queue(self):
        with patch(
            "apps.core.idempotency.database_now", return_value=datetime(2025, 1, 1, tzinfo=UTC)
        ):
            self.unknown()
        self.assertEqual(statistics.dashboard(self.admin)["counts"]["reports"], 1)
        self.assertEqual(len(self.data("cases", category="REPORTS", state="pending")), 1)
        self.assertEqual(self.data("cases"), [])

    def test_statistics_request_counts_and_quantities_by_origin(self):
        request, products = self.prepare((3, 2))
        self.deliver(request, [1, 1], mode="LATER")
        data = self.data("requests")
        self.assertEqual(
            dict(statistics.indicators("requests", data)),
            {
                "Demandes distinctes": 1,
                "Demandes ouvertes": 1,
                "Demandes partiellement remises et ouvertes": 1,
                "Demandes closes": 0,
            },
        )
        totals = statistics.request_totals(data)
        self.assertEqual(len(totals), 2)
        self.assertTrue(
            all(r["requested"] == r["delivered"] + r["closed"] + r["reserved"] for r in totals)
        )

    def test_timeout_and_memory_limits_refuse_no_partial_file(self):
        p = self.demandable_product()
        with override_settings(REPORTING_MAX_CHARACTERS=1):
            self.assertEqual(self.export().status_code, 422)
        with patch("apps.reporting.snapshots.time.monotonic", side_effect=[0, 20]):
            with self.assertRaises(BusinessError):
                materialize(iter([{"cells": ["x"]}]))
        self.assertEqual(self.stock(p)[0], 10)

    def test_stock_hold_visible_and_snapshot_export_readonly(self):
        r, ps, c = self.conflict()
        self.assertEqual(self.data("stocks")[0]["cells"][9], "Bloqué")
        before = self.stock(ps[0])
        self.assertEqual(self.export().status_code, 200)
        self.assertEqual(self.stock(ps[0]), before)

    def test_postgresql_statement_timeout_is_enforced(self):
        with override_settings(REPORTING_TIMEOUT_MS=5):
            with self.assertRaises(BusinessError), snapshot(), connection.cursor() as cursor:
                cursor.execute("SELECT pg_sleep(0.03)")
        self.assertFalse(Operation.objects.filter(command_type="reporting.export").exists())

    def test_history_correction_shows_original_date_and_only_delta(self):
        _, _, delivery, case = self.completed()
        self.correct(delivery, case, [8])
        row = next(
            r["cells"] for r in self.data("history") if r["cells"][3] == "Correction confirmée"
        )
        self.assertIn("différence -2", row[5])
        self.assertIn("Remise du", row[6])

    def test_nonconforming_not_regular_and_not_surplus(self):
        p = self.demandable_product(physical=0)
        o = self.buy([p], [10])
        self.receive(o, [4], [6])
        cells = self.data("receipts")[0]["cells"]
        self.assertEqual((cells[8], cells[10], cells[11]), (4, 6, 0))
        self.assertEqual(self.stock(p)[0], 4)

    def test_pagination_preserves_product_search_and_period(self):
        from apps.catalog.models import Product

        products = [
            Product(code=f"PAGE-{i}", name=f"Produit page {i}", unit="boîte", minimum=0, target=0)
            for i in range(51)
        ]
        Product.objects.bulk_create(products)
        response = self.admin_client.get(
            "/analyses/stocks?q=Produit+page&start=2026-10-01&end=2026-11-01"
        )
        self.assertEqual(response.status_code, 200)
        from html import unescape

        page = unescape(response.content.decode())
        self.assertIn("page=2", page)
        self.assertIn("q=Produit+page", page)
        self.assertIn("start=2026-10-01", page)
        self.assertEqual(len(response.context["rows"]), 50)
        second = self.admin_client.get(
            "/analyses/stocks?q=Produit+page&start=2026-10-01&end=2026-11-01&page=2"
        )
        self.assertEqual(len(second.context["rows"]), 1)

    def test_same_floor_names_do_not_merge_distinct_floor_identities(self):
        from apps.catalog.models import Floor

        Floor.objects.filter(pk=self.other_floor.pk).update(name=self.floor.name)
        p = self.demandable_product(physical=20, maximum=10)
        self.rights(
            p,
            [
                {"floor_id": str(floor.pk), "visible": True, "demandable": True, "maximum": 10}
                for floor in [self.floor, self.other_floor]
            ],
        )
        self.assertEqual(self.send([p], [2]).status_code, 201)
        User.objects.filter(pk=self.assistant.pk).update(floor=self.other_floor)
        self.assertEqual(self.send([p], [3]).status_code, 201)
        totals = statistics.request_totals(self.data("requests"))
        self.assertEqual(len(totals), 2)
        self.assertEqual(
            {r["floor_code"] for r in totals}, {self.floor.code, self.other_floor.code}
        )

    def test_admin_personal_and_doctor_history_stay_in_their_scope(self):
        from apps.requests.models import InternalRequest

        request, products = self.requested((3,))
        rows = list(selectors.rows(self.admin, "history", self.filters(), True))
        self.assertTrue(all(row["cells"][1] == self.admin.display_name for row in rows))
        doctor = User.objects.create_user(
            "doctor-history",
            "fictive-password-long",
            display_name="Demandeur B fictif",
            role="DOCTOR",
            floor=self.floor,
            must_change_password=False,
        )
        client = self.logged(doctor)
        response = self.send(products, [2], client=client)
        self.assertEqual(response.status_code, 201, response.content)
        own = InternalRequest.objects.get(pk=response.json()["resource"]["id"])
        page = client.get("/historique-personnel")
        self.assertContains(page, own.reference)
        self.assertNotContains(page, request.reference)
        self.assertNotContains(page, "Exporter ces données")

    def test_history_search_reference_includes_corrections_and_purchases(self):
        request, products, delivery, case = self.completed()
        self.correct(delivery, case, [8])
        result = self.data("history", q=request.reference)
        self.assertIn("Correction confirmée", [r["cells"][3] for r in result])
        self.assertIn("Erreur de remise signalée", [r["cells"][3] for r in result])
        self.assertEqual(len(result), len({str(r["cells"][0]) + r["cells"][3] for r in result}))
        order = self.buy(products, [4])
        purchases = self.data("history", q=order.reference)
        self.assertEqual([r["cells"][3] for r in purchases], ["Achat enregistré"])
        self.assertEqual(self.receive(order, [1]).status_code, 201)
        self.assertEqual(
            self.post_json(
                f"/api/v1/orders/{order.pk}/reminders",
                {"expected_last_reminder": None},
                self.operator,
            ).status_code,
            200,
        )
        self.cancel_remainder(order, 1)
        history = self.data("history", q=order.reference)
        labels = [r["cells"][3] for r in history]
        for label in [
            "Achat enregistré",
            "Réception confirmée",
            "Rappel manuel enregistré",
            "Unités fournisseur restantes annulées",
        ]:
            self.assertIn(label, labels)
        self.assertTrue(all(r["url"] for r in history))

    def test_lookup_choice_limit_refuses_without_silent_truncation(self):
        self.demandable_product(code="LOOKUP-A")
        self.demandable_product(code="LOOKUP-B")
        with override_settings(REPORTING_MAX_CHOICES=1):
            response = self.admin_client.get("/analyses/stocks")
            self.assertEqual(response.status_code, 422)
            self.assertContains(response, "liste des choix", status_code=422)
        self.assertFalse(Operation.objects.filter(command_type="reporting.export").exists())

    def test_export_restores_registration_after_core_registry_reset(self):
        from apps.core.commands import _registry

        _registry.pop("reporting.export", None)
        response = self.export()
        self.assertEqual(response.status_code, 200, response.content[:200])
        self.assertEqual(
            AuditEvent.objects.filter(operation__command_type="reporting.export").count(), 1
        )
