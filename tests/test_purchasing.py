"""M7 sur PostgreSQL réel : achats déclarés, A11 provisoire, A09 et équations."""

import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from django.db import DatabaseError, close_old_connections, connection, transaction
from django.test import TransactionTestCase
from django.utils import timezone

from apps.audit.models import AuditEvent
from apps.catalog.models import Supplier
from apps.core import idempotency
from apps.core.models import OperationIntent
from apps.core.policies import ActorContext
from apps.inventory.models import StockEntry
from apps.notifications.models import BusinessEvent, Outbox
from apps.purchasing.models import (
    DuplicateAcknowledgement,
    OrderLine,
    PurchaseOrder,
    Recommendation,
    RecommendationEvent,
    RemainderEvent,
)
from apps.purchasing.services import ensure_registered
from tests.test_requests import RequestFixtures


class PurchaseFixtures(RequestFixtures):
    def setUp(self):
        super().setUp()
        ensure_registered()
        response = self.post_json(
            "/api/v1/suppliers",
            {
                "code": "FOURN-FICTIF",
                "name": "Fournisseur fictif",
                "website": "https://example.invalid/catalogue",
                "active": True,
            },
        )
        self.assertEqual(response.status_code, 201, response.content)
        self.supplier = Supplier.objects.get(pk=response.json()["resource"]["id"])
        self.admin_context = ActorContext(self.admin.pk, self.admin.auth_version)

    def purchase_body(self, products, quantities, **extra):
        for product in products:
            product.refresh_from_db()
        return {
            "supplier_id": str(self.supplier.pk),
            "purchased_on": timezone.localdate().isoformat(),
            "lines": [
                {"product_id": str(p.pk), "quantity": q, "expected_product_version": p.version}
                for p, q in zip(products, quantities, strict=True)
            ],
            "purchase_confirmed": True,
            **extra,
        }

    def buy(self, products, quantities, **extra):
        response = self.post_json(
            "/api/v1/orders", self.purchase_body(products, quantities, **extra)
        )
        self.assertEqual(response.status_code, 201, response.content)
        return PurchaseOrder.objects.get(pk=response.json()["resource"]["id"])

    def need(self, product):
        response = self.admin_client.get(f"/api/v1/recommendations/{product.pk}")
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()["recommendation"]

    def rec_action(self, product, action, body=None, **headers):
        row = self.need(product)
        return self.post_json(
            f"/api/v1/recommendations/{product.pk}/{action}",
            body
            or {"expected_version": row["version"], "state_fingerprint": row["state_fingerprint"]},
            **headers,
        )

    def cancel_order(self, order, quantities=None, **extra):
        order.refresh_from_db()
        return self.post_json(
            f"/api/v1/orders/{order.pk}/cancel-remainder",
            {
                "expected_version": order.version,
                "external_verification": True,
                "lines": [
                    {
                        "order_line_id": str(line.pk),
                        "quantity": line.waiting if quantities is None else quantities[i],
                    }
                    for i, line in enumerate(order.lines.order_by("product_code"))
                ],
                **extra,
            },
        )

    def duplicate_ack(self, response):
        self.assertEqual(response.status_code, 409, response.content)
        error = response.json()["error"]
        return {
            "candidate_ids": [c["id"] for c in error["candidates"]],
            "candidate_fingerprint": error["candidate_fingerprint"],
            "verified_distinct": True,
        }

    def race(self, items):
        barrier = threading.Barrier(len(items), timeout=20)

        def run(item):
            close_old_connections()
            try:
                with connection.cursor() as c:
                    c.execute("SELECT pg_backend_pid()")
                    pid = c.fetchone()[0]
                context, payload, identifier, key = item
                barrier.wait()
                return pid, idempotency.execute_intent(
                    context, intent_id=identifier, key=key, payload=payload
                )
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=len(items)) as pool:
            results = list(pool.map(run, items))
        p = Path("docs/recette/M7/concurrency.json")
        p.parent.mkdir(parents=True, exist_ok=True)
        current = json.loads(p.read_text()) if p.exists() else {}
        current[self._testMethodName] = {
            "participants": len(items),
            "distinct_connections": len({p for p, o in results}),
            "outcomes": [o.status for p, o in results],
            "confirmed_operations": len(
                {o.body.get("operation_id") for p, o in results if o.body.get("operation_id")}
            ),
        }
        p.write_text(json.dumps(current, indent=2) + "\n")
        return [o for p, o in results]


class PurchaseTests(PurchaseFixtures, TransactionTestCase):
    def test_threshold_and_formula_includes_real_reservations_and_waiting(self):
        product = self.demandable_product(physical=5, maximum=10)
        product.refresh_from_db()
        self.assertEqual(self.modify(product, minimum=3, target=15).status_code, 200)
        self.assertEqual(self.send([product], [2]).status_code, 201)
        self.buy([product], [4])
        row = self.need(product)
        self.assertEqual(
            (
                row["physical"],
                row["reserved"],
                row["available"],
                row["waiting"],
                row["recommended"],
            ),
            (5, 2, 3, 4, 8),
        )
        self.assertEqual(row["state"], "ACTIVE")
        self.assertEqual(PurchaseOrder.objects.count(), 1)

    def test_under_threshold_and_covering_waiting_gives_zero(self):
        p = self.demandable_product(physical=1)
        self.assertEqual(self.need(p)["recommended"], 9)
        self.buy([p], [9])
        self.assertEqual(self.need(p)["recommended"], 0)
        self.assertEqual(self.need(p)["state"], "COVERED")

    def test_above_threshold_no_recommendation(self):
        p = self.demandable_product(physical=10)
        rows = self.admin_client.get("/api/v1/recommendations").json()["recommendations"]
        self.assertEqual(rows, [])
        self.assertEqual(self.need(p)["state"], "INACTIVE")

    def test_reservation_creates_need_without_purchase_or_physical_change(self):
        p = self.demandable_product(physical=5, maximum=5)
        self.assertEqual(self.need(p)["state"], "INACTIVE")
        self.assertEqual(self.send([p], [3]).status_code, 201)
        self.assertEqual(self.need(p)["recommended"], 8)
        self.assertEqual(self.stock(p)[:3], (5, 3, 2))
        self.assertEqual(PurchaseOrder.objects.count(), 0)

    def test_product_without_supplier_is_visible(self):
        p = self.demandable_product(physical=0)
        row = self.need(p)
        self.assertEqual(row["suppliers"], [])
        self.assertEqual(row["recommended"], 10)
        self.assertTrue(row["shortage"])

    def test_multiple_suppliers_one_need_and_alternative_group(self):
        p = self.demandable_product(physical=1)
        other = Supplier.objects.create(code="AUTRE-FICTIF", name="Autre fictif")
        p.refresh_from_db()
        response = self.post_json(
            f"/api/v1/products/{p.pk}/supplier-links",
            {
                "expected_version": p.version,
                "suppliers": [
                    {
                        "supplier_id": str(self.supplier.pk),
                        "preferred": True,
                        "reference": "F-001",
                        "website": "",
                        "indicative_price": None,
                        "currency": "",
                    },
                    {
                        "supplier_id": str(other.pk),
                        "preferred": False,
                        "reference": "ALT-001",
                        "website": "",
                        "indicative_price": None,
                        "currency": "",
                    },
                ],
            },
            method="put",
        )
        self.assertEqual(response.status_code, 200, response.content)
        rows = self.admin_client.get("/api/v1/recommendations").json()["recommendations"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(rows[0]["suppliers"]), 2)
        alternative = self.admin_client.get(
            "/api/v1/recommendations", {"supplier": str(other.pk)}
        ).json()["recommendations"]
        self.assertEqual(alternative[0]["group_supplier"]["id"], str(other.pk))

    def test_defer_then_manual_reactivate_without_stock_or_purchase(self):
        p = self.demandable_product(physical=2)
        original = self.stock(p)
        self.assertEqual(self.rec_action(p, "defer").status_code, 200)
        self.assertEqual(self.need(p)["state"], "DEFERRED")
        self.assertEqual(self.rec_action(p, "reactivate").status_code, 200)
        self.assertEqual(self.need(p)["state"], "ACTIVE")
        self.assertEqual(self.stock(p), original)
        self.assertEqual(PurchaseOrder.objects.count(), 0)
        self.assertEqual(Recommendation.objects.count(), 1)
        self.assertEqual(RecommendationEvent.objects.count(), 2)

    def test_deferred_reactivates_after_drop_even_if_then_rebounded_unseen(self):
        p = self.demandable_product(physical=2)
        self.rec_action(p, "defer")
        self.assertEqual(self.adjust([p], [1]).status_code, 200)
        self.assertEqual(self.adjust([p], [2]).status_code, 200)
        self.assertEqual(self.need(p)["state"], "ACTIVE")
        self.assertEqual(RecommendationEvent.objects.count(), 1)

    def test_zero_defer_no_loop_then_new_zero_transition_reactivates(self):
        p = self.demandable_product(physical=0)
        self.rec_action(p, "defer")
        for _ in range(3):
            self.assertEqual(self.need(p)["state"], "DEFERRED")
        self.assertTrue(self.need(p)["shortage"])
        self.adjust([p], [1])
        self.adjust([p], [0])
        self.assertEqual(self.need(p)["state"], "ACTIVE")
        self.assertEqual(RecommendationEvent.objects.count(), 1)

    def test_zero_remains_visible_when_waiting_covers_target(self):
        p = self.demandable_product(physical=0)
        self.buy([p], [10])
        row = self.need(p)
        self.assertTrue(row["shortage"])
        self.assertEqual(row["recommended"], 0)
        self.assertEqual(row["state"], "COVERED")
        self.assertEqual(
            len(self.admin_client.get("/api/v1/recommendations").json()["recommendations"]), 1
        )

    def test_old_recommendation_fingerprint_rejected_after_new_waiting(self):
        p = self.demandable_product(physical=1)
        row = self.need(p)
        self.buy([p], [2])
        response = self.rec_action(
            p,
            "defer",
            {"expected_version": row["version"], "state_fingerprint": row["state_fingerprint"]},
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(RecommendationEvent.objects.count(), 0)

    def test_buy_real_with_reference_amount_snapshots_and_no_stock_change(self):
        p = self.demandable_product()
        before = self.stock(p)
        entries = StockEntry.objects.count()
        order = self.buy([p], [7], external_reference=" COM-01 ", amount="45.50", currency="EUR")
        line = order.lines.get()
        self.assertEqual((line.quantity, line.received, line.cancelled, line.waiting), (7, 0, 0, 7))
        self.assertEqual(order.reference_key, "COM-01")
        self.assertEqual(order.external_reference, " COM-01 ")
        self.assertEqual(str(order.amount), "45.50")
        self.assertEqual(self.stock(p), before)
        self.assertEqual(StockEntry.objects.count(), entries)
        event = BusinessEvent.objects.get(
            operation=order.creation_operation, event_type="purchase.recorded"
        )
        self.assertEqual(event.payload["recipient_roles"], ["ADMIN", "ACCOUNTANT_EXTERNAL"])
        self.assertEqual(event.payload["order"]["lines"][0]["quantity"], 7)
        self.assertEqual(
            Outbox.objects.filter(event__operation=order.creation_operation).count(), 2
        )

    def test_free_purchase_non_inventoried_without_preferred_supplier(self):
        p = self.create_product()
        order = self.buy([p], [6])
        self.assertIsNone(self.stock(p)[0])
        self.assertEqual(order.lines.get().waiting, 6)
        self.assertEqual(self.need(p)["state"], "INACTIVE")

    def test_purchase_without_reference_and_optional_amount(self):
        p = self.create_product()
        order = self.buy([p], [3])
        self.assertEqual(order.external_reference, "")
        self.assertIsNone(order.amount)
        self.assertEqual(order.currency, "")

    def test_attestation_supplier_date_and_amount_validations(self):
        p = self.create_product()
        for extra in [
            {"purchase_confirmed": False},
            {"supplier_id": None},
            {"purchased_on": (timezone.localdate() + timedelta(days=1)).isoformat()},
            {"amount": "1.999", "currency": "EUR"},
            {"amount": "NaN", "currency": "EUR"},
            {"amount": 2.5, "currency": "EUR"},
            {"amount": "2.00", "currency": ""},
            {"amount": "2.00", "currency": "ABC"},
            {"currency": "EUR"},
        ]:
            with self.subTest(extra=extra):
                response = self.post_json("/api/v1/orders", self.purchase_body([p], [2], **extra))
                self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(PurchaseOrder.objects.count(), 0)

    def test_duplicate_products_invalid_quantities_and_atomic_lot(self):
        p = self.create_product()
        other = self.create_product(code="AUTRE-PROD")
        for body in [
            self.purchase_body([p, p], [1, 2]),
            self.purchase_body([p, other], [1, 0]),
            self.purchase_body([p], [True]),
            self.purchase_body([p], [1.5]),
        ]:
            response = self.post_json("/api/v1/orders", body)
            self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(OrderLine.objects.count(), 0)

    def test_reference_duplicate_warns_no_creation_then_explicit_distinct(self):
        p = self.create_product()
        first = self.buy([p], [3], external_reference="EXT-001")
        body = self.purchase_body([p], [4], external_reference=" EXT-001 ")
        response = self.post_json("/api/v1/orders", body)
        ack = self.duplicate_ack(response)
        self.assertEqual(ack["candidate_ids"], [str(first.pk)])
        self.assertEqual(PurchaseOrder.objects.count(), 1)
        second = self.post_json("/api/v1/orders", {**body, "distinct_purchase_ack": ack})
        self.assertEqual(second.status_code, 201, second.content)
        self.assertEqual(DuplicateAcknowledgement.objects.count(), 1)
        self.assertEqual(PurchaseOrder.objects.count(), 2)

    def test_conservative_normalization_distinguishes_case_punctuation_zeroes(self):
        p = self.create_product()
        for reference in ["Ab-001", "ab-001", "Ab001", "Ab-01"]:
            self.buy([p], [1], external_reference=reference)
        self.assertEqual(PurchaseOrder.objects.count(), 4)

    def test_unicode_canonical_reference_and_control_characters(self):
        p = self.create_product()
        self.buy([p], [1], external_reference="Café")
        self.assertEqual(
            self.post_json(
                "/api/v1/orders", self.purchase_body([p], [2], external_reference="Cafe\u0301")
            ).status_code,
            409,
        )
        self.assertEqual(
            self.post_json(
                "/api/v1/orders", self.purchase_body([p], [2], external_reference="X\nY")
            ).status_code,
            400,
        )

    def test_no_reference_exact_lines_ignore_order_but_not_quantities(self):
        p = self.create_product()
        other = self.create_product(code="AUTRE-PROD")
        self.buy([p, other], [2, 3])
        body = self.purchase_body([other, p], [3, 2])
        response = self.post_json("/api/v1/orders", body)
        self.assertEqual(response.status_code, 409, response.content)
        self.assertEqual(
            response.json()["error"]["code"], "DISTINCT_PURCHASE_CONFIRMATION_REQUIRED"
        )
        self.buy([p, other], [2, 4])
        self.assertEqual(PurchaseOrder.objects.count(), 2)

    def test_cancelled_order_remains_duplicate_candidate(self):
        p = self.create_product()
        o = self.buy([p], [2], external_reference="EXT-2")
        self.assertEqual(self.cancel_order(o).status_code, 200)
        response = self.post_json(
            "/api/v1/orders", self.purchase_body([p], [2], external_reference="EXT-2")
        )
        self.assertEqual(self.duplicate_ack(response)["candidate_ids"], [str(o.pk)])

    def test_new_candidate_invalidates_acknowledgement(self):
        p = self.create_product()
        self.buy([p], [2])
        body = self.purchase_body([p], [2])
        ack = self.duplicate_ack(self.post_json("/api/v1/orders", body))
        self.assertEqual(
            self.post_json("/api/v1/orders", {**body, "distinct_purchase_ack": ack}).status_code,
            201,
        )
        stale = self.post_json("/api/v1/orders", {**body, "distinct_purchase_ack": ack})
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(len(stale.json()["error"]["candidates"]), 2)
        self.assertEqual(PurchaseOrder.objects.count(), 2)

    def test_twenty_same_purchase_intention_exactly_once(self):
        p = self.create_product()
        body = self.purchase_body([p], [3])
        binding = self.intention("orders.create", body, context=self.admin_context)
        outcomes = self.race([(self.admin_context, body, *binding)] * 20)
        self.assertEqual({o.status for o in outcomes}, {201})
        self.assertEqual(len({o.body["operation_id"] for o in outcomes}), 1)
        self.assertEqual(PurchaseOrder.objects.count(), 1)

    def test_distinct_concurrent_intentions_detect_duplicate_under_supplier_lock(self):
        p = self.create_product()
        body = self.purchase_body([p], [3])
        items = [
            (
                self.admin_context,
                body,
                *self.intention("orders.create", body, context=self.admin_context),
            )
            for _ in range(2)
        ]
        outcomes = self.race(items)
        self.assertEqual(sorted(o.status for o in outcomes), [201, 409])
        self.assertEqual(PurchaseOrder.objects.count(), 1)

    def test_two_concurrent_acknowledgements_new_candidate_requires_reverification(self):
        p = self.create_product()
        self.buy([p], [3])
        body = self.purchase_body([p], [3])
        body["distinct_purchase_ack"] = self.duplicate_ack(self.post_json("/api/v1/orders", body))
        outcomes = self.race(
            [
                (
                    self.admin_context,
                    body,
                    *self.intention("orders.create", body, context=self.admin_context),
                )
                for _ in range(2)
            ]
        )
        self.assertEqual(sorted(o.status for o in outcomes), [201, 409])
        self.assertEqual(PurchaseOrder.objects.count(), 2)

    def test_lost_response_replay_and_changed_payload_binding(self):
        p = self.create_product()
        body = self.purchase_body([p], [3])
        identifier, key = uuid.uuid4(), uuid.uuid4().hex
        response = self.post_json("/api/v1/orders", body, identifier=identifier, key=key)
        self.assertEqual(response.status_code, 201)
        replay = self.post_json("/api/v1/orders", body, identifier=identifier, key=key)
        self.assertEqual(replay.json(), response.json())
        altered = self.post_json(
            "/api/v1/orders", self.purchase_body([p], [4]), identifier=identifier, key=key
        )
        self.assertEqual(altered.status_code, 409)
        self.assertEqual(PurchaseOrder.objects.count(), 1)

    def test_cancel_partial_and_full_remainders_preserve_physical_reserved(self):
        p = self.demandable_product(physical=1)
        original = self.stock(p)
        order = self.buy([p], [10])
        self.assertEqual(self.cancel_order(order, [3]).status_code, 200)
        line = order.lines.get()
        self.assertEqual(
            (line.quantity, line.received, line.cancelled, line.waiting), (10, 0, 3, 7)
        )
        self.assertEqual(self.need(p)["recommended"], 2)
        self.assertEqual(self.cancel_order(order, [7]).status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.status, "CANCELLED_NO_RECEIPT")
        self.assertEqual(self.stock(p), original)
        self.assertEqual(RemainderEvent.objects.count(), 2)
        history = self.admin_client.get(f"/api/v1/orders/{order.pk}", {"offset": 1})
        self.assertEqual(history.status_code, 200, history.content)
        self.assertEqual([e["revision"] for e in history.json()["events"]], [1])

    def test_invalid_remainder_line_and_foreign_line_atomic_rejection(self):
        p = self.create_product()
        other = self.create_product(code="AUTRE-PROD")
        order = self.buy([p, other], [3, 4])
        response = self.cancel_order(order, [1, 99])
        self.assertEqual(response.status_code, 409)
        self.assertEqual(sum(order.lines.values_list("cancelled", flat=True)), 0)
        body = {
            "expected_version": 0,
            "external_verification": True,
            "lines": [{"order_line_id": str(uuid.uuid4()), "quantity": 1}],
        }
        self.assertEqual(
            self.post_json(f"/api/v1/orders/{order.pk}/cancel-remainder", body).status_code, 400
        )
        self.assertEqual(RemainderEvent.objects.count(), 0)

    def test_remainder_verification_version_and_double_confirmation(self):
        p = self.create_product()
        order = self.buy([p], [4])
        line = order.lines.get()
        body = {
            "expected_version": 0,
            "external_verification": False,
            "lines": [{"order_line_id": str(line.pk), "quantity": 2}],
        }
        self.assertEqual(
            self.post_json(f"/api/v1/orders/{order.pk}/cancel-remainder", body).status_code, 400
        )
        body["external_verification"] = True
        identifier, key = uuid.uuid4(), uuid.uuid4().hex
        a = self.post_json(
            f"/api/v1/orders/{order.pk}/cancel-remainder", body, identifier=identifier, key=key
        )
        b = self.post_json(
            f"/api/v1/orders/{order.pk}/cancel-remainder", body, identifier=identifier, key=key
        )
        self.assertEqual(a.status_code, 200, a.content)
        self.assertEqual(a.json(), b.json())
        self.assertEqual(
            self.post_json(f"/api/v1/orders/{order.pk}/cancel-remainder", body).status_code, 409
        )
        self.assertEqual(order.lines.get().cancelled, 2)

    def test_remainder_cancellation_concurrent_one_version_wins(self):
        p = self.create_product()
        order = self.buy([p], [4])
        line = order.lines.get()
        body = {
            "expected_version": 0,
            "external_verification": True,
            "lines": [{"order_line_id": str(line.pk), "quantity": 3}],
        }
        items = [
            (
                self.admin_context,
                body,
                *self.intention(
                    "orders.cancel_remainder", body, resource=order.pk, context=self.admin_context
                ),
            )
            for _ in range(2)
        ]
        outcomes = self.race(items)
        self.assertEqual(sorted(o.status for o in outcomes), [200, 409])
        self.assertEqual(order.lines.get().waiting, 1)

    def test_original_facts_immutable_and_equation_reconstructed(self):
        p = self.create_product()
        order = self.buy([p], [5])
        self.cancel_order(order, [2])
        self.cancel_order(order, [1])
        line = order.lines.get()
        self.assertEqual(line.quantity, line.received + line.cancelled + line.waiting)
        from io import StringIO

        from django.core.management import call_command

        output = StringIO()
        call_command("check_purchase_orders", stdout=output)
        self.assertIn("1 commande(s), 1 ligne(s)", output.getvalue())
        for model, pk, change in [
            (PurchaseOrder, order.pk, {"supplier_name": "Altération"}),
            (OrderLine, line.pk, {"quantity": 99}),
            (RemainderEvent, order.events.first().pk, {"revision": 99}),
            (OrderLine, line.pk, {"cancelled": 0}),
        ]:
            with (
                self.subTest(model=model, change=change),
                self.assertRaises(DatabaseError),
                transaction.atomic(),
            ):
                model.objects.filter(pk=pk).update(**change)

    def test_sql_guard_blocks_bypassed_duplicate_detector(self):
        p = self.create_product()
        self.buy([p], [3])
        body = self.purchase_body([p], [3])
        with patch("apps.purchasing.services.check_duplicates", return_value=([], "")):
            response = self.post_json("/api/v1/orders", body)
        self.assertEqual(response.status_code, 503, response.content)
        self.assertEqual(PurchaseOrder.objects.count(), 1)

    def test_supplier_product_changes_do_not_rewrite_purchase_snapshots(self):
        p = self.create_product()
        order = self.buy([p], [3])
        original = order.lines.get().product_name
        p.refresh_from_db()
        self.assertEqual(self.modify(p, name="Nouveau nom fictif").status_code, 200)
        self.supplier.refresh_from_db()
        response = self.post_json(
            f"/api/v1/suppliers/{self.supplier.pk}",
            {
                "expected_version": self.supplier.version,
                "name": "Fournisseur renommé",
                "active": False,
            },
            method="patch",
        )
        self.assertEqual(response.status_code, 200, response.content)
        order.refresh_from_db()
        self.assertEqual(order.supplier_name, "Fournisseur fictif")
        self.assertEqual(order.lines.get().product_name, original)

    def test_roles_cannot_read_or_mutate_purchasing(self):
        p = self.demandable_product(physical=1)
        order = self.buy([p], [3])
        for client in [self.user_client, self.operator]:
            for route in [
                "/api/v1/recommendations",
                f"/api/v1/recommendations/{p.pk}",
                "/api/v1/orders",
                f"/api/v1/orders/{order.pk}",
            ]:
                self.assertEqual(client.get(route).status_code, 403, route)
            self.assertEqual(
                self.post_json("/api/v1/orders", self.purchase_body([p], [2]), client).status_code,
                403,
            )
            self.assertEqual(
                self.post_json(f"/api/v1/recommendations/{p.pk}/defer", {}, client).status_code, 403
            )
            self.assertEqual(
                self.post_json(
                    f"/api/v1/orders/{order.pk}/cancel-remainder", {}, client
                ).status_code,
                403,
            )
        self.assertEqual(PurchaseOrder.objects.count(), 1)

    def test_outbox_failure_rolls_back_purchase_and_does_not_fake_success(self):
        p = self.create_product()
        before = AuditEvent.objects.count()
        identifier, key = uuid.uuid4(), uuid.uuid4().hex
        with patch.object(Outbox.objects, "create", side_effect=RuntimeError("Panne fictive")):
            response = self.post_json(
                "/api/v1/orders", self.purchase_body([p], [3]), identifier=identifier, key=key
            )
        self.assertEqual(response.status_code, 500)
        self.assertEqual(PurchaseOrder.objects.count(), 0)
        self.assertEqual(AuditEvent.objects.count(), before)
        self.assertEqual(OperationIntent.objects.get(pk=identifier).state, "READY")

    def test_recommendation_history_immutable_and_replay_without_duplicate(self):
        p = self.demandable_product(physical=1)
        identifier, key = uuid.uuid4(), uuid.uuid4().hex
        row = self.need(p)
        body = {"expected_version": row["version"], "state_fingerprint": row["state_fingerprint"]}
        first = self.rec_action(p, "defer", body, identifier=identifier, key=key)
        second = self.rec_action(p, "defer", body, identifier=identifier, key=key)
        self.assertEqual(first.status_code, 200, first.content)
        self.assertEqual(first.json(), second.json())
        self.assertEqual(RecommendationEvent.objects.count(), 1)
        with self.assertRaises(DatabaseError), transaction.atomic():
            RecommendationEvent.objects.update(available=99)

    def test_archived_and_non_counted_products_do_not_generate_needs(self):
        p = self.create_product()
        self.assertEqual(self.need(p)["state"], "INACTIVE")
        counted = self.demandable_product(physical=1)
        counted.refresh_from_db()
        self.assertEqual(
            self.post_json(
                f"/api/v1/products/{counted.pk}/archive",
                {"expected_version": counted.version, "confirmed": True},
            ).status_code,
            200,
        )
        self.assertEqual(
            self.admin_client.get("/api/v1/recommendations").json()["recommendations"], []
        )

    def test_old_purchase_unit_never_subtracted_from_new_unit_needs(self):
        p = self.create_product()
        old = self.buy([p], [5])
        p.refresh_from_db()
        self.assertEqual(self.modify(p, unit="pièce").status_code, 200)
        self.initial([p], [1])
        self.activate(p)
        row = self.need(p)
        self.assertEqual((row["waiting"], row["recommended"]), (0, 9))
        self.assertEqual(row["incompatible_waiting"][0]["waiting"], 5)
        self.assertEqual(row["incompatible_waiting"][0]["unit"], "carton")
        self.assertEqual(old.lines.get().unit, "carton")

    def test_order_search_matches_many_lines_without_duplicate_rows(self):
        p = self.create_product(name="Compresses fictives")
        other = self.create_product(code="AUTRE-PROD", name="Compresses autres fictives")
        order = self.buy([p, other], [2, 3])
        response = self.admin_client.get("/api/v1/orders", {"q": "compresses"})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual([r["id"] for r in response.json()["orders"]], [str(order.pk)])

    def test_runtime_sql_role_can_use_m7_but_cannot_delete_facts(self):
        import os

        from apps.core.management.commands.grant_runtime import grant_runtime

        p = self.demandable_product(physical=1)
        grant_runtime(os.environ["RUNTIME_DATABASE_USER"])
        original = connection.settings_dict.copy()
        connection.close()
        connection.settings_dict.update(
            USER=os.environ["RUNTIME_DATABASE_USER"],
            PASSWORD=os.environ["RUNTIME_DATABASE_PASSWORD"],
        )
        try:
            self.assertEqual(self.rec_action(p, "defer").status_code, 200)
            self.assertEqual(self.rec_action(p, "reactivate").status_code, 200)
            order = self.buy([p], [2])
            self.assertEqual(self.cancel_order(order, [1]).status_code, 200)
            for sql in [
                "DELETE FROM purchasing_purchaseorder",
                "DELETE FROM purchasing_remainderevent",
                "UPDATE purchasing_recommendationevent SET available=99",
                "UPDATE purchasing_orderline SET received=1",
            ]:
                with (
                    self.assertRaises(DatabaseError),
                    transaction.atomic(),
                    connection.cursor() as c,
                ):
                    c.execute(sql)
        finally:
            connection.close()
            connection.settings_dict.update(original)

    def test_two_concurrent_reports_one_revision_wins(self):
        p = self.demandable_product(physical=1)
        row = self.need(p)
        body = {"expected_version": row["version"], "state_fingerprint": row["state_fingerprint"]}
        outcomes = self.race(
            [
                (
                    self.admin_context,
                    body,
                    *self.intention(
                        "recommendations.defer", body, resource=p.pk, context=self.admin_context
                    ),
                )
                for _ in range(2)
            ]
        )
        self.assertEqual(sorted(o.status for o in outcomes), [200, 409])
        self.assertEqual(RecommendationEvent.objects.count(), 1)

    def test_purchase_and_new_reservation_share_product_locks_without_stock_effect(self):
        p = self.demandable_product(physical=5, maximum=5)
        purchase = self.purchase_body([p], [3])
        request = {"lines": [{"product_id": str(p.pk), "quantity": 4}]}
        assistant_context = ActorContext(self.assistant.pk, self.assistant.auth_version)
        outcomes = self.race(
            [
                (
                    self.admin_context,
                    purchase,
                    *self.intention("orders.create", purchase, context=self.admin_context),
                ),
                (
                    assistant_context,
                    request,
                    *self.intention("requests.create", request, context=assistant_context),
                ),
            ]
        )
        self.assertEqual([o.status for o in outcomes], [201, 201])
        self.assertEqual(self.stock(p)[:3], (5, 4, 1))
        self.assertEqual(self.need(p)["recommended"], 6)

    def test_new_html_and_search_routes_refuse_other_roles(self):
        p = self.demandable_product(physical=1)
        order = self.buy([p], [2])
        for client in [self.user_client, self.operator]:
            for path in [
                "/achats",
                "/achats/besoins",
                "/achats/enregistrer",
                f"/achats/{order.pk}",
                f"/achats/{order.pk}/annuler-reliquat",
                f"/achats/besoins/{p.pk}",
                f"/achats/besoins/{p.pk}/reporter",
                f"/achats/besoins/{p.pk}/reactiver",
            ]:
                self.assertEqual(client.get(path).status_code, 403, path)
            for q in ["", order.reference]:
                self.assertEqual(
                    client.get(
                        "/api/v1/search-suggestions", {"scope": "orders", "q": q}
                    ).status_code,
                    403,
                )
        self.assertEqual(PurchaseOrder.objects.count(), 1)

    def test_purchase_refuses_stale_unit_without_changing_original_or_stock(self):
        p = self.create_product()
        body = self.purchase_body([p], [3])
        p.refresh_from_db()
        self.assertEqual(self.modify(p, unit="pièce").status_code, 200)
        response = self.post_json("/api/v1/orders", body)
        self.assertEqual(response.status_code, 409, response.content)
        self.assertEqual(response.json()["error"]["current_unit"], "pièce")
        self.assertEqual(PurchaseOrder.objects.count(), 0)
        self.assertIsNone(self.stock(p)[0])

    def test_exact_product_lookup_handles_homonyms_and_roles(self):
        p = self.create_product(name="Même nom fictif")
        other = self.create_product(code="AUTRE-FICTIF", name="Même nom fictif")
        for product in [p, other]:
            response = self.admin_client.get(
                "/api/v1/purchase-products", {"code": product.code.lower()}
            )
            self.assertEqual(response.status_code, 200, response.content)
            self.assertEqual(response.json()["product"]["id"], str(product.pk))
        for client in [self.user_client, self.operator]:
            self.assertEqual(
                client.get("/api/v1/purchase-products", {"code": p.code}).status_code, 403
            )

    def test_purchase_and_pre_inventory_unit_change_serialized_without_silent_conversion(self):
        p = self.create_product()
        body = self.purchase_body([p], [3])
        update = {"expected_version": p.version, "unit": "pièce"}
        outcomes = self.race(
            [
                (
                    self.admin_context,
                    body,
                    *self.intention("orders.create", body, context=self.admin_context),
                ),
                (
                    self.admin_context,
                    update,
                    *self.intention(
                        "catalog.product.update", update, resource=p.pk, context=self.admin_context
                    ),
                ),
            ]
        )
        self.assertEqual(outcomes[1].status, 200)
        self.assertIn(outcomes[0].status, [201, 409])
        p.refresh_from_db()
        self.assertEqual(p.unit, "pièce")
        if PurchaseOrder.objects.exists():
            self.assertEqual(OrderLine.objects.get().unit, "carton")

    def test_deferred_need_remains_consultable_above_threshold_without_positive_advice(self):
        p = self.demandable_product(physical=2)
        self.assertEqual(self.rec_action(p, "defer").status_code, 200)
        self.assertEqual(self.adjust([p], [5]).status_code, 200)
        rows = self.admin_client.get("/api/v1/recommendations", {"state": "DEFERRED"}).json()[
            "recommendations"
        ]
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["deferred"])
        self.assertEqual(rows[0]["recommended"], 0)
        self.assertEqual(rows[0]["state"], "INACTIVE")

    def test_covered_deferred_shortage_stays_in_deferred_list(self):
        p = self.demandable_product(physical=0)
        self.rec_action(p, "defer")
        self.buy([p], [10])
        rows = self.admin_client.get("/api/v1/recommendations", {"state": "DEFERRED"}).json()[
            "recommendations"
        ]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["state"], "COVERED")
        self.assertTrue(rows[0]["deferred"])
        self.assertTrue(rows[0]["shortage"])
        self.assertEqual(rows[0]["recommended"], 0)
