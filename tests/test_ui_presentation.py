"""Présentation UI-FIX : pas de nouveau métier ou de mutation de quantité."""

from datetime import UTC, date, datetime
from types import SimpleNamespace

from django.test import RequestFactory, SimpleTestCase, TransactionTestCase

from apps.core.presentation import case_navigation
from apps.core.templatetags.presentation import ui_date, ui_datetime
from apps.requests.models import Delivery
from tests.test_corrections import CorrectionFixtures
from tests.test_receiving import ReceivingFixtures


class FrenchPresentationTests(SimpleTestCase):
    def test_datetime_utc_converted_to_paris_winter(self):
        self.assertEqual(ui_datetime("2026-10-28T16:37:54.294199+00:00"), "28 octobre 2026 17:37")

    def test_summer_and_midnight_crossing(self):
        self.assertEqual(
            ui_datetime(datetime(2026, 7, 1, 23, 30, tzinfo=UTC)), "2 juillet 2026 01:30"
        )

    def test_calendar_date_does_not_shift(self):
        self.assertEqual(ui_date("2026-10-28"), "28 octobre 2026")
        self.assertEqual(ui_datetime(date(2026, 10, 28)), "28 octobre 2026")

    def test_invalid_date_is_clear_and_safe(self):
        for value in [None, "<script>alert(1)</script>", "2026-02-31", "not-a-date"]:
            self.assertEqual(ui_datetime(value), "Date indisponible")

    def test_known_navigation_origin_only(self):
        case = SimpleNamespace(
            delivery_id=1,
            delivery=SimpleNamespace(request_id="00000000-0000-0000-0000-000000000001"),
            stock_case_id=None,
        )
        request = RequestFactory().get("/dossiers/x", {"from": "deliveries"})
        self.assertEqual(
            case_navigation(request, case)["case_back_url"], "/livraisons/signalements"
        )

    def test_external_return_url_never_used(self):
        case = SimpleNamespace(delivery_id=None, stock_case_id=1)
        for origin in ["https://evil.invalid", "//evil.invalid", "javascript:alert(1)", "unknown"]:
            result = case_navigation(RequestFactory().get("/dossiers/x", {"from": origin}), case)
            self.assertEqual(result["case_back_url"], "/stocks/signalements")


class UIPresentationIntegrationTests(ReceivingFixtures, CorrectionFixtures, TransactionTestCase):
    def test_recommendation_is_information_not_actual_quantity(self):
        p = self.demandable_product(physical=7)
        self.modify(p, minimum=10, target=100)
        response = self.admin_client.get(
            "/achats/enregistrer", {"product": p.pk, "supplier": self.supplier.pk}
        )
        self.assertEqual(response.status_code, 200)
        config = response.context["config"]["initial_product"]
        self.assertEqual(config["recommended"], 93)
        self.assertNotIn("quantity", config)
        self.assertEqual(response.context["back_url"], f"/achats/besoins/{p.pk}")

    def test_purchase_form_without_product_returns_to_purchase_list(self):
        self.assertEqual(
            self.admin_client.get("/achats/enregistrer").context["back_url"], "/achats"
        )

    def test_case_direct_origin_and_invalid_origin_are_safe(self):
        request, products = self.prepare((3,))
        self.deliver(request)
        delivery = Delivery.objects.get()
        case = self.error_case(delivery)
        for value, expected in [
            ("cases", "/dossiers"),
            ("deliveries", "/livraisons/signalements"),
            ("request", f"/demandes/{request.pk}"),
            ("//evil.invalid", "/livraisons/signalements"),
        ]:
            response = self.admin_client.get(f"/dossiers/{case.pk}", {"from": value})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.context["case_back_url"], expected)

    def test_single_waiting_quantity_is_not_sum_of_different_units(self):
        p = self.demandable_product(physical=0)
        q = self.demandable_product(code="UI-SECOND", physical=0)
        order = self.buy([p, q], [2, 3])
        response = self.admin_client.get(f"/achats/{order.pk}")
        self.assertIsNone(response.context["order"]["single_waiting_line"])
        self.assertContains(response, "Annuler les quantités restantes")

    def test_ui_helpers_do_not_expand_permissions(self):
        p = self.demandable_product()
        order = self.buy([p], [3])
        for client in [self.user_client, self.operator]:
            self.assertEqual(client.get("/achats/enregistrer", {"product": p.pk}).status_code, 403)
            self.assertEqual(client.get(f"/achats/{order.pk}").status_code, 403)

    def test_receiving_tabs_current_link_and_french_dates(self):
        p = self.demandable_product(physical=0)
        order = self.buy([p], [3])
        self.receive(order, [3])
        for path in ["/receptions", "/receptions/historique", "/receptions/signalements"]:
            response = self.operator.get(path)
            self.assertContains(response, 'aria-current="page"')
        self.assertNotContains(self.operator.get("/receptions/historique"), "T00:")
