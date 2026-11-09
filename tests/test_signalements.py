"""Consultation et intégration M6 des signalements, faits fictifs réels en PostgreSQL."""

import uuid
from unittest.mock import patch

from django.test import Client, TransactionTestCase

from apps.accounts.models import User
from apps.cases.models import Case, CaseEvent, StockHold
from apps.core.models import Operation
from apps.inventory.models import StockEntry
from apps.requests.models import Delivery
from tests.test_accounts import FICTIVE_PASSWORD
from tests.test_corrections import CorrectionFixtures
from tests.test_receiving import ReceivingFixtures


class SignalementFixtures(CorrectionFixtures):
    def shortage(self, quantities=(4,)):
        request, products = self.prepare(quantities)
        self.assertEqual(self.deliver(request, [1] * len(products), "SHORTAGE").status_code, 201)
        return request, products, Case.objects.get(kind="SHORTAGE")

    def resolve_body(self, case):
        response = self.admin_client.get(f"/dossiers/{case.pk}/verifier-manque")
        self.assertEqual(response.status_code, 200)
        cfg = response.context["config"]
        return {
            "expected_version": cfg["expected_version"],
            "verification": True,
            "message": "Comptage fictif vérifié, aucun écart restant.",
            "product_counts": [
                {**row, "counted_quantity": self.stock_product(row["product_id"])}
                for row in cfg["product_counts"]
            ],
        }

    @staticmethod
    def stock_product(identifier):
        from apps.inventory.models import StockBalance

        return StockBalance.objects.get(product_id=identifier).physical


class SignalementTests(SignalementFixtures, TransactionTestCase):
    def test_default_pending_includes_under_review_but_not_resolved(self):
        request, products, case = self.conflict()
        self.post_json(
            f"/api/v1/cases/{case.pk}/review",
            {"expected_version": 0, "verification": True, "message": "Examen fictif commencé."},
            self.admin_client,
        )
        response = self.admin_client.get("/stocks/signalements")
        self.assertEqual(response.context["cards"][0]["status"], "UNDER_REVIEW")
        self.reconcile(self.reconciliation_body(request, products[0], case))
        self.assertEqual(self.admin_client.get("/stocks/signalements").context["cards"], [])
        self.assertEqual(
            len(self.admin_client.get("/stocks/signalements?state=resolved").context["cards"]), 1
        )
        self.assertEqual(
            len(self.admin_client.get("/stocks/signalements?state=all").context["cards"]), 1
        )

    def test_search_multi_product_deduplicated_and_accents_normalized(self):
        request, products = self.requested((8, 8))
        self.assertEqual(self.modify(products[0], name="Écharpes de réserve").status_code, 200)
        self.assertEqual(self.adjust(products, [6, 5]).status_code, 409)
        response = self.admin_client.get("/stocks/signalements", {"q": "produit fictif"})
        self.assertEqual(len(response.context["cards"]), 1)
        self.assertEqual(len(response.context["cards"][0]["products"]), 2)
        self.assertEqual(
            len(self.admin_client.get("/stocks/signalements", {"q": "echarpes"}).context["cards"]),
            1,
        )
        self.assertEqual(
            self.admin_client.get("/stocks/signalements", {"q": "inexistant"}).context["cards"], []
        )

    def test_delivery_search_floor_assistant_product_reference_and_admin_opened_case(self):
        request, products = self.prepare((4,))
        self.deliver(request)
        delivery = Delivery.objects.get()
        r = self.post_json(
            "/api/v1/cases",
            {
                "kind": "DELIVERY_ERROR",
                "source_id": str(delivery.pk),
                "verification": True,
                "message": "Erreur vérifiée par la responsable.",
            },
            self.admin_client,
        )
        self.assertEqual(r.status_code, 200, r.content)
        for text in [
            request.reference,
            request.floor_name,
            request.assistant_name,
            products[0].name,
        ]:
            response = self.admin_client.get("/livraisons/signalements", {"q": text})
            self.assertEqual(len(response.context["cards"]), 1, text)
        self.assertEqual(self.operator.get("/livraisons/signalements").context["cards"], [])

    def test_filters_and_search_survive_pagination(self):
        for index in range(51):
            p = self.demandable_product(code=f"PAGE-{index:02}", maximum=10)
            self.assertEqual(self.send([p], [8]).status_code, 201)
            self.assertEqual(self.adjust([p], [6]).status_code, 409)
        response = self.admin_client.get("/stocks/signalements", {"state": "all", "q": "PAGE"})
        self.assertEqual(len(response.context["cards"]), 50)
        self.assertIn("state=all", response.context["next_url"])
        self.assertIn("q=PAGE", response.context["next_url"])
        self.assertIn("offset=50", response.context["next_url"])
        response = self.admin_client.get(response.context["next_url"])
        self.assertEqual(len(response.context["cards"]), 1)
        self.assertIn("q=PAGE", response.context["previous_url"])
        self.assertIn("offset=0", response.context["previous_url"])

    def test_invalid_filter_and_offset_rejected(self):
        for values in [{"state": "RESOLVED"}, {"offset": "-1"}, {"offset": "100001"}]:
            self.assertEqual(self.admin_client.get("/stocks/signalements", values).status_code, 400)

    def test_old_constat_url_renders_full_dossier_through_relation(self):
        def independent_projection(source):
            return Case.objects.create(
                stock_case=source, origin_operation=source.operation, kind="STOCK_CONFLICT"
            )

        _, products = self.requested((8,))
        with patch("apps.cases.workflow.stock_projection", side_effect=independent_projection):
            response = self.adjust(products, [6])
        self.assertEqual(response.status_code, 409)
        case = Case.objects.get(stock_case_id=response.json()["case_id"])
        source = case.stock_case
        self.assertNotEqual(case.pk, source.pk)
        from apps.cases.views import render_detail

        with patch("apps.cases.views.render_detail", wraps=render_detail) as renderer:
            response = self.operator.get(f"/stocks/signalements/{source.pk}")
            self.assertEqual(renderer.call_args.args[2].pk, case.pk)
        response = self.operator.get(f"/stocks/signalements/{source.pk}")
        self.assertContains(response, "Situation actuelle")
        self.assertContains(response, "Ce qui a été signalé")

    def test_original_and_current_quantities_are_distinct_and_get_has_no_effect(self):
        _, products, case = self.conflict()
        self.adjust(products, [12])
        before = (Operation.objects.count(), StockEntry.objects.count(), CaseEvent.objects.count())
        response = self.admin_client.get(f"/dossiers/{case.pk}")
        self.assertEqual(response.context["case"]["products"][0]["physical"], 10)
        self.assertEqual(response.context["current_stocks"][0]["physical"], 12)
        self.assertContains(response, "Quantité réellement comptée et déclarée")
        self.assertEqual(
            before,
            (Operation.objects.count(), StockEntry.objects.count(), CaseEvent.objects.count()),
        )

    def test_calculated_correction_is_never_a_prefilled_physical_count(self):
        request, products, delivery, case = self.completed()
        self.correct(delivery, case, [8])
        self.adjust(products, [0])
        payload = self.correction_body(delivery, case, [9])
        preview = self.preview_correct(payload)
        self.assertEqual(preview.status_code, 409)
        # La prévisualisation seule ne crée pas de fait ; le refus confirmé crée le dossier A03.
        payload["preview_hash"] = "0" * 64
        response = self.post_json("/api/v1/corrections/confirm", payload, self.admin_client)
        self.assertEqual(response.status_code, 409)
        conflict = Case.objects.filter(kind="STOCK_CONFLICT").first()
        self.assertIsNotNone(conflict, response.content)
        detail = self.admin_client.get(f"/dossiers/{conflict.pk}")
        self.assertContains(detail, "Ce résultat calculé n’est pas un comptage physique vérifié.")
        editor = self.admin_client.get(f"/dossiers/{conflict.pk}/regulariser")
        self.assertEqual(editor.context["rows"][0]["current"], "")
        self.assertTrue(StockHold.objects.filter(product=products[0]).exists())

    def test_actions_are_server_scoped_for_three_roles(self):
        _, _, case = self.conflict()
        for url in ["/stocks/signalements", "/livraisons/signalements", f"/dossiers/{case.pk}"]:
            self.assertEqual(self.user_client.get(url).status_code, 403)
            self.assertEqual(self.operator.get(url).status_code, 200)
        self.assertNotContains(self.operator.get(f"/dossiers/{case.pk}"), 'id="case-form"')
        self.assertEqual(self.operator.get(f"/dossiers/{case.pk}/regulariser").status_code, 403)
        stranger = User.objects.create_user(
            "autre-signaleur", FICTIVE_PASSWORD, role="REPLENISHER", must_change_password=False
        )
        client = Client()
        client.force_login(stranger)
        self.assertEqual(client.get(f"/dossiers/{case.pk}").status_code, 404)
        self.assertEqual(client.get(f"/stocks/signalements/{case.stock_case_id}").status_code, 404)
        self.assertEqual(client.get("/stocks/signalements").context["cards"], [])

    def test_contextual_return_is_preserved_and_external_targets_ignored(self):
        _, _, case = self.conflict()
        for suffix in ["", "/regulariser"]:
            response = self.admin_client.get(
                f"/dossiers/{case.pk}" + suffix,
                {"from": "stock", "state": "all", "q": "gants & savon", "offset": 50},
            )
            self.assertIn("state=all", response.context["case_back_url"])
            self.assertIn("offset=50", response.context["case_context_query"])
            self.assertTrue(response.context["case_back_url"].startswith("/stocks/signalements?"))
        response = self.admin_client.get(
            f"/dossiers/{case.pk}", {"from": "//evil.invalid", "next": "https://evil.invalid"}
        )
        self.assertEqual(response.context["case_back_url"], "/stocks/signalements")

    def test_shortage_form_uses_all_source_products_empty_counts_and_existing_endpoint(self):
        _, _, case = self.shortage((4, 3))
        response = self.admin_client.get(f"/dossiers/{case.pk}/verifier-manque")
        self.assertEqual(len(response.context["rows"]), 2)
        self.assertContains(response, "data-count-product=", count=2)
        self.assertNotContains(response, 'value="6"')
        self.assertEqual(response.context["config"]["url"], f"/api/v1/cases/{case.pk}/resolve")
        self.assertContains(
            self.admin_client.get(f"/dossiers/{case.pk}"), "Confirmer la vérification du manque"
        )

    def test_verified_shortage_resolution_does_not_change_stock_or_request(self):
        request, products, case = self.shortage()
        body = self.resolve_body(case)
        stock = self.stock(products[0])
        request.refresh_from_db()
        version = request.version
        before = StockEntry.objects.count()
        response = self.post_json(f"/api/v1/cases/{case.pk}/resolve", body, self.admin_client)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self.stock(products[0]), stock)
        self.assertEqual(StockEntry.objects.count(), before)
        request.refresh_from_db()
        self.assertEqual(request.version, version)
        self.assertEqual(CaseEvent.objects.get(case=case).kind, "SHORTAGE_VERIFIED")
        detail = self.admin_client.get(f"/dossiers/{case.pk}")
        self.assertFalse(detail.context["can_verify_shortage"])
        self.assertEqual(
            self.admin_client.get(f"/dossiers/{case.pk}/verifier-manque").status_code, 409
        )

    def test_shortage_conflicting_count_or_obsolete_version_has_no_fake_resolution(self):
        _, products, case = self.shortage()
        body = self.resolve_body(case)
        body["product_counts"][0]["counted_quantity"] -= 1
        self.assertEqual(
            self.post_json(f"/api/v1/cases/{case.pk}/resolve", body, self.admin_client).status_code,
            409,
        )
        body = self.resolve_body(case)
        self.adjust(products, [9])
        self.assertEqual(
            self.post_json(f"/api/v1/cases/{case.pk}/resolve", body, self.admin_client).status_code,
            409,
        )
        case.refresh_from_db()
        self.assertEqual(case.status, "OPEN")

    def test_shortage_with_hold_cannot_be_resolved_or_offered_for_resolution(self):
        _, products, case = self.shortage()
        body = self.resolve_body(case)
        self.send(products, [6])
        self.assertEqual(self.adjust(products, [5]).status_code, 409)
        self.assertFalse(
            self.admin_client.get(f"/dossiers/{case.pk}").context["can_verify_shortage"]
        )
        response = self.admin_client.get(f"/dossiers/{case.pk}/verifier-manque")
        self.assertTrue(response.context["blocked"])
        self.assertIsNone(response.context["config"])
        body["product_counts"][0]["expected_stock_version"] = self.stock(products[0])[3]
        self.assertEqual(
            self.post_json(f"/api/v1/cases/{case.pk}/resolve", body, self.admin_client).status_code,
            409,
        )

    def test_shortage_resolve_permissions_reject_without_artificial_incident(self):
        _, _, case = self.shortage()
        body = self.resolve_body(case)
        before = (Case.objects.count(), StockEntry.objects.count())
        for client in [self.operator, self.user_client]:
            self.assertEqual(client.get(f"/dossiers/{case.pk}/verifier-manque").status_code, 403)
            self.assertEqual(
                self.post_json(f"/api/v1/cases/{case.pk}/resolve", body, client).status_code, 403
            )
        self.assertEqual(before, (Case.objects.count(), StockEntry.objects.count()))

    def test_duplicate_resolution_intention_records_one_event(self):
        _, _, case = self.shortage()
        body = self.resolve_body(case)
        identifier, key = uuid.uuid4(), str(uuid.uuid4())
        for _ in range(2):
            response = self.post_json(
                f"/api/v1/cases/{case.pk}/resolve",
                body,
                self.admin_client,
                identifier=identifier,
                key=key,
            )
            self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(CaseEvent.objects.filter(case=case, kind="SHORTAGE_VERIFIED").count(), 1)

    def test_reexamine_case_does_not_reopen_closed_request(self):
        request, _, delivery, case = self.completed()
        self.correct(delivery, case, [8])
        request.refresh_from_db()
        before = (request.status, request.version)
        case.refresh_from_db()
        self.post_json(
            f"/api/v1/cases/{case.pk}/review",
            {
                "expected_version": case.version,
                "verification": True,
                "message": "Réexamen du dossier uniquement.",
            },
            self.admin_client,
        )
        request.refresh_from_db()
        self.assertEqual((request.status, request.version), before)
        self.assert_request_consistent(request)


class ReceivingNavigationIsolationTests(ReceivingFixtures, TransactionTestCase):
    def test_only_receiving_navigation_is_active_for_unknown_order_dossier(self):
        report = self.unknown()
        from html.parser import HTMLParser

        class NavigationParser(HTMLParser):
            def __init__(self, class_name):
                super().__init__()
                self.class_name = class_name
                self.in_navigation = False
                self.active = []

            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if tag == "nav":
                    self.in_navigation = self.class_name in attrs.get("class", "").split()
                if tag == "a" and self.in_navigation and attrs.get("aria-current") == "page":
                    self.active.append(attrs["href"])

            def handle_endtag(self, tag):
                if tag == "nav":
                    self.in_navigation = False

        for client, selector in [
            (self.admin_client, ".sidebar-links"),
            (self.operator, ".bottom-navigation"),
        ]:
            response = client.get(f"/receptions/dossiers/{report.case_id}")
            self.assertEqual(response.status_code, 200)
            parser = NavigationParser(selector.removeprefix("."))
            parser.feed(response.content.decode())
            self.assertEqual(parser.active, ["/receptions"])
