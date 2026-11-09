"""M6 : faits correctifs réels, invariants PostgreSQL et décisions réservées Gestionnaire."""

import uuid
from unittest.mock import patch

from django.db import DatabaseError, transaction
from django.test import TransactionTestCase

from apps.audit.models import AuditEvent
from apps.cases.models import Case, CaseEvent, StockHold, StockReconciliation
from apps.inventory.models import StockEntry
from apps.notifications.models import Outbox
from apps.requests.models import (
    Delivery,
    DeliveryCorrection,
    DeliveryLine,
    InternalRequest,
)
from tests.test_deliveries import DeliveryFixtures


class CorrectionFixtures(DeliveryFixtures):
    def error_case(self, delivery):
        response = self.post_json(
            f"/api/v1/deliveries/{delivery.pk}/report-error",
            {"message": "Quantité à vérifier, données fictives."},
            self.operator,
        )
        self.assertEqual(response.status_code, 201, response.content)
        return Case.objects.get(pk=response.json()["resource"]["id"])

    def correction_body(self, delivery, case, quantities, disposition=None, reopen=None):
        request = delivery.request
        request.refresh_from_db()
        case.refresh_from_db()
        lines = list(delivery.lines.order_by("line__product_code"))
        stocks = {
            str(line_item.product_id): self.stock(line_item.product)[3]
            for line_item in request.lines.select_related("product")
        }
        result = {
            "kind": "DELIVERY",
            "source_id": str(delivery.pk),
            "case_id": str(case.pk),
            "expected_versions": {
                "request": request.version,
                "case": case.version,
                "stocks": stocks,
            },
            "corrected_lines": [
                {"delivery_line_id": str(line_item.pk), "quantity": q}
                for line_item, q in zip(lines, quantities, strict=True)
            ],
            "verification": True,
            "message": "Comptage et remise réelle vérifiés dans la recette fictive.",
        }
        if disposition:
            result["disposition"] = disposition
        if reopen is not None:
            result["reopen_quantities"] = [
                {"request_line_id": str(line_item.pk), "quantity": q}
                for line_item, q in zip(request.lines.order_by("product_code"), reopen, strict=True)
            ]
        return result

    def preview_correct(self, payload, client=None):
        return self.post_json("/api/v1/corrections/preview", payload, client or self.admin_client)

    def correct(self, delivery, case, quantities, disposition=None, reopen=None):
        payload = self.correction_body(delivery, case, quantities, disposition, reopen)
        response = self.preview_correct(payload)
        self.assertEqual(response.status_code, 200, response.content)
        payload["preview_hash"] = response.json()["preview"]["preview_hash"]
        response = self.post_json("/api/v1/corrections/confirm", payload, self.admin_client)
        self.assertEqual(response.status_code, 200, response.content)
        return response, payload

    def completed(self, quantity=10, physical=10):
        request, products = self.prepare((quantity,), physical)
        self.assertEqual(self.deliver(request).status_code, 201)
        delivery = Delivery.objects.get(request=request)
        return request, products, delivery, self.error_case(delivery)

    def conflict(self):
        request, products = self.requested((8,))
        response = self.adjust(products, [6])
        self.assertEqual(response.status_code, 409, response.content)
        return request, products, Case.objects.get(pk=response.json()["case_id"])

    def reconciliation_body(self, request, product, case, release=2, count=6):
        request.refresh_from_db()
        case.refresh_from_db()
        line = request.lines.get()
        return {
            "case_id": str(case.pk),
            "expected_case_version": case.version,
            "product_counts": [
                {
                    "product_id": str(product.pk),
                    "expected_stock_version": self.stock(product)[3],
                    "counted_quantity": count,
                }
            ],
            "commitments": [
                {
                    "request_id": str(request.pk),
                    "request_line_id": str(line.pk),
                    "expected_request_version": request.version,
                    "release_quantity": release,
                }
            ],
            "verification": True,
            "message": "Stock fictif recompté et engagements choisis personnellement.",
        }

    def reconcile(self, payload):
        response = self.post_json(
            "/api/v1/stock-reconciliations/preview", payload, self.admin_client
        )
        self.assertEqual(response.status_code, 200, response.content)
        payload = {**payload, "preview_hash": response.json()["preview"]["preview_hash"]}
        result = self.post_json("/api/v1/stock-reconciliations/confirm", payload, self.admin_client)
        self.assertEqual(result.status_code, 200, result.content)
        return result, payload


class CorrectionTests(CorrectionFixtures, TransactionTestCase):
    def test_10_to_8_closed_keeps_closed_original_and_exact_delta(self):
        request, products, delivery, case = self.completed()
        original = DeliveryLine.objects.get(delivery=delivery)
        preview = self.preview_correct(self.correction_body(delivery, case, [8]))
        self.assertEqual(preview.status_code, 200, preview.content)
        self.assertEqual(self.stock(products[0])[:3], (0, 0, 0))
        self.assertEqual(DeliveryCorrection.objects.count(), 0)
        self.correct(delivery, case, [8])
        self.assertEqual(self.stock(products[0])[:3], (2, 0, 2))
        line = request.lines.get()
        self.assertEqual((line.delivered, line.closed, line.reservation.remaining), (8, 2, 0))
        original.refresh_from_db()
        self.assertEqual(original.handed, 10)
        request.refresh_from_db()
        self.assertEqual(request.status, "PARTIAL_CLOSED")
        case.refresh_from_db()
        self.assertEqual(case.status, "RESOLVED")
        self.assert_request_consistent(request)

    def test_second_correction_uses_current_effective_quantity(self):
        request, products, delivery, case = self.completed()
        self.correct(delivery, case, [8])
        self.correct(delivery, case, [7])
        self.assertEqual(self.stock(products[0])[:3], (3, 0, 3))
        self.assertEqual(
            list(
                StockEntry.objects.filter(kind="DELIVERY_CORRECTION")
                .order_by("version_after")
                .values_list("delta_p", flat=True)
            ),
            [2, 1],
        )
        self.assert_request_consistent(request)

    def test_increase_allowed_when_available_and_not_above_Q(self):
        request, products, delivery, case = self.completed()
        self.correct(delivery, case, [8])
        self.correct(delivery, case, [9])
        self.assertEqual(self.stock(products[0])[:3], (1, 0, 1))
        self.assert_request_consistent(request)

    def test_above_Q_refused_without_fact(self):
        request, products, delivery, case = self.completed()
        response = self.preview_correct(self.correction_body(delivery, case, [11]))
        self.assertEqual(response.status_code, 409, response.content)
        self.assertEqual(DeliveryCorrection.objects.count(), 0)
        self.assertEqual(self.stock(products[0])[:3], (0, 0, 0))

    def test_open_request_recalculates_U_and_R(self):
        request, products = self.prepare((10,))
        self.deliver(request, [6], "LATER")
        delivery = Delivery.objects.get(request=request)
        case = self.error_case(delivery)
        self.correct(delivery, case, [4])
        self.assertEqual(self.stock(products[0])[:3], (6, 6, 0))
        self.assert_request_consistent(request)

    def test_explicit_reopen_with_correction(self):
        request, products, delivery, case = self.completed()
        self.correct(delivery, case, [8], "REOPEN", [2])
        self.assertEqual(self.stock(products[0])[:3], (2, 2, 0))
        request.refresh_from_db()
        self.assertEqual(request.status, "PARTIAL_OPEN")
        self.assert_request_consistent(request)

    def test_correction_net_zero_does_not_restore_assistant_cancellation(self):
        request, products, delivery, case = self.completed()
        self.correct(delivery, case, [0], "REOPEN", [10])
        request.refresh_from_db()
        self.assertEqual(request.status, "PREPARING")
        self.assertEqual(self.cancel_request(request).status_code, 409)
        self.assertIsNotNone(request.first_delivery_operation_id)

    def test_stale_preview_version_and_changed_content_refused(self):
        request, products, delivery, case = self.completed()
        body = self.correction_body(delivery, case, [8])
        response = self.preview_correct(body)
        body["preview_hash"] = response.json()["preview"]["preview_hash"]
        self.correct(delivery, case, [9])
        response = self.post_json("/api/v1/corrections/confirm", body, self.admin_client)
        self.assertEqual(response.status_code, 409, response.content)
        self.assertEqual(DeliveryCorrection.objects.count(), 1)

    def test_admin_abandon_releases_only_remainder_and_distinct_cause(self):
        request, products = self.prepare((10,))
        self.deliver(request, [4], "LATER")
        request.refresh_from_db()
        response = self.post_json(
            f"/api/v1/requests/{request.pk}/close-remainder",
            {
                "expected_version": request.version,
                "reason": "Reliquat non nécessaire, vérifié.",
                "verification": True,
            },
            self.admin_client,
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self.stock(products[0])[:3], (6, 0, 6))
        request.refresh_from_db()
        self.assertEqual(request.closure_reason, "REMAINDER_ABANDONED")
        self.assertEqual(request.lines.get().delivered, 4)
        self.assert_request_consistent(request)

    def test_reopen_closed_quantities_standalone(self):
        request, products, delivery, case = self.completed()
        self.correct(delivery, case, [8])
        request.refresh_from_db()
        response = self.post_json(
            f"/api/v1/requests/{request.pk}/reopen",
            {
                "expected_version": request.version,
                "quantities": [{"request_line_id": str(request.lines.get().pk), "quantity": 2}],
                "reason": "Deux unités seront finalement remises.",
                "verification": True,
            },
            self.admin_client,
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self.stock(products[0])[:3], (2, 2, 0))
        self.assert_request_consistent(request)

    def test_reopen_impossible_no_partial_movement(self):
        request, products, delivery, case = self.completed()
        self.correct(delivery, case, [8])
        self.adjust(products, [0])
        request.refresh_from_db()
        response = self.post_json(
            f"/api/v1/requests/{request.pk}/reopen",
            {
                "expected_version": request.version,
                "quantities": [{"request_line_id": str(request.lines.get().pk), "quantity": 2}],
                "reason": "Réouverture impossible à vérifier.",
                "verification": True,
            },
            self.admin_client,
        )
        self.assertEqual(response.status_code, 409, response.content)
        self.assertEqual(self.stock(products[0])[:3], (0, 0, 0))
        self.assert_request_consistent(request)

    def test_reconciliation_targeted_releases_count_and_hold_resolution(self):
        request, products, case = self.conflict()
        self.assertTrue(StockHold.objects.filter(product=products[0]).exists())
        self.reconcile(self.reconciliation_body(request, products[0], case))
        self.assertEqual(self.stock(products[0])[:3], (6, 6, 0))
        self.assertFalse(StockHold.objects.exists())
        case.refresh_from_db()
        self.assertEqual(case.status, "RESOLVED")
        self.assert_request_consistent(request)

    def test_reconciliation_still_incompatible_keeps_case_hold_no_movement(self):
        request, products, case = self.conflict()
        before = StockEntry.objects.count()
        response = self.post_json(
            "/api/v1/stock-reconciliations/preview",
            self.reconciliation_body(request, products[0], case, release=1),
            self.admin_client,
        )
        self.assertEqual(response.status_code, 409, response.content)
        self.assertEqual(StockEntry.objects.count(), before)
        self.assertTrue(StockHold.objects.exists())
        self.assertEqual(self.stock(products[0])[:3], (10, 8, 2))

    def test_review_is_not_resolution_and_resolve_cannot_bypass_reconciliation(self):
        request, products, case = self.conflict()
        r = self.post_json(
            f"/api/v1/cases/{case.pk}/review",
            {"expected_version": 0, "verification": True, "message": "Vérification commencée."},
            self.admin_client,
        )
        self.assertEqual(r.status_code, 200, r.content)
        case.refresh_from_db()
        self.assertEqual(case.status, "UNDER_REVIEW")
        r = self.post_json(
            f"/api/v1/cases/{case.pk}/resolve",
            {
                "expected_version": 1,
                "verification": True,
                "message": "Résolution sans comptage interdite.",
                "product_counts": [],
            },
            self.admin_client,
        )
        self.assertEqual(r.status_code, 409, r.content)
        self.assertTrue(StockHold.objects.exists())

    def test_roles_cannot_mutate_m6_and_do_not_create_spurious_case(self):
        request, products, delivery, case = self.completed()
        payload = self.correction_body(delivery, case, [8])
        before = Case.objects.count()
        for client in [self.operator, self.user_client]:
            self.assertEqual(self.preview_correct(payload, client).status_code, 403)
            self.assertEqual(
                self.post_json("/api/v1/corrections/confirm", payload, client).status_code, 403
            )
            self.assertEqual(
                self.post_json(
                    f"/api/v1/cases/{case.pk}/review",
                    {"expected_version": 0, "verification": True, "message": "Interdit."},
                    client,
                ).status_code,
                403,
            )
        self.assertEqual(Case.objects.count(), before)
        self.assertEqual(DeliveryCorrection.objects.count(), 0)

    def test_double_confirmation_lost_response_same_intention(self):
        request, products, delivery, case = self.completed()
        body = self.correction_body(delivery, case, [8])
        r = self.preview_correct(body)
        body["preview_hash"] = r.json()["preview"]["preview_hash"]
        identifier = uuid.uuid4()
        key = uuid.uuid4().hex
        a = self.post_json(
            "/api/v1/corrections/confirm", body, self.admin_client, identifier=identifier, key=key
        )
        b = self.post_json(
            "/api/v1/corrections/confirm", body, self.admin_client, identifier=identifier, key=key
        )
        self.assertEqual(a.status_code, 200, a.content)
        self.assertEqual(a.json()["operation_id"], b.json()["operation_id"])
        self.assertEqual(DeliveryCorrection.objects.count(), 1)
        self.assertEqual(self.stock(products[0])[:3], (2, 0, 2))

    def test_outbox_failure_rolls_back_correction_and_case_resolution(self):
        request, products, delivery, case = self.completed()
        body = self.correction_body(delivery, case, [8])
        body["preview_hash"] = self.preview_correct(body).json()["preview"]["preview_hash"]
        with patch(
            "apps.core.idempotency.Outbox.objects.create",
            side_effect=RuntimeError("fictive failure"),
        ):
            response = self.post_json("/api/v1/corrections/confirm", body, self.admin_client)
        self.assertEqual(response.status_code, 500)
        self.assertEqual(DeliveryCorrection.objects.count(), 0)
        self.assertEqual(self.stock(products[0])[:3], (0, 0, 0))
        case.refresh_from_db()
        self.assertEqual(case.status, "OPEN")

    def test_immutable_original_correction_and_audit(self):
        request, products, delivery, case = self.completed()
        response, _ = self.correct(delivery, case, [8])
        self.assertTrue(
            AuditEvent.objects.filter(operation_id=response.json()["operation_id"]).exists()
        )
        self.assertTrue(
            Outbox.objects.filter(event__operation_id=response.json()["operation_id"]).exists()
        )
        for query, values in [
            (DeliveryLine.objects.filter(delivery=delivery), {"handed": 7}),
            (DeliveryCorrection.objects.all(), {"previous_id": None}),
            (CaseEvent.objects.all(), {"details": {}}),
        ]:
            with self.assertRaises(DatabaseError), transaction.atomic():
                query.update(**values)
        self.assert_request_consistent(request)

    def test_reassign_preserves_delivery_author_and_old_operator_forbidden(self):
        from apps.accounts.models import User
        from tests.test_accounts import FICTIVE_PASSWORD

        request, products = self.prepare((10,))
        self.deliver(request, [4], "LATER")
        request.refresh_from_db()
        source = Delivery.objects.get(request=request)
        other = User.objects.create_user(
            "autre-reappro-fictif", FICTIVE_PASSWORD, role="REPLENISHER", must_change_password=False
        )
        response = self.post_json(
            f"/api/v1/requests/{request.pk}/reassign",
            {
                "expected_version": request.version,
                "new_operator_id": str(other.pk),
                "verification": True,
            },
            self.admin_client,
        )
        self.assertEqual(response.status_code, 200, response.content)
        request.refresh_from_db()
        self.assertEqual(request.assigned_to_id, other.pk)
        source.refresh_from_db()
        self.assertEqual(source.operator_id, self.replenisher.pk)
        self.assertEqual(self.deliver(request, [6]).status_code, 403)
        self.assertEqual(self.deliver(request, [6], client=self.logged(other)).status_code, 201)
        self.assert_request_consistent(request)
        self.assertEqual(self.operator.get(f"/api/v1/requests/{request.pk}").status_code, 200)

    def test_reassign_inactive_or_wrong_role_refused_and_no_admin_delivery(self):
        request, products = self.prepare()
        body = {
            "expected_version": request.version,
            "new_operator_id": str(self.assistant.pk),
            "verification": True,
        }
        self.assertEqual(
            self.post_json(
                f"/api/v1/requests/{request.pk}/reassign", body, self.admin_client
            ).status_code,
            400,
        )
        self.assertEqual(self.deliver(request, client=self.admin_client).status_code, 403)
        self.assert_request_consistent(request)

    def test_successive_delivery_after_reopen_uses_effective_historical_remainder(self):
        request, products, delivery, case = self.completed()
        self.correct(delivery, case, [8], "REOPEN", [2])
        request.refresh_from_db()
        response = self.deliver(request, [2])
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(self.stock(products[0])[:3], (0, 0, 0))
        self.assertEqual(request.lines.get().delivered, 10)
        self.assert_request_consistent(request)

    def test_future_delivery_preserves_abandonment_cause(self):
        request, products = self.prepare((10,))
        self.deliver(request, [4], "LATER")
        request.refresh_from_db()
        self.post_json(
            f"/api/v1/requests/{request.pk}/close-remainder",
            {
                "expected_version": request.version,
                "reason": "Reste abandonné.",
                "verification": True,
            },
            self.admin_client,
        )
        request.refresh_from_db()
        self.post_json(
            f"/api/v1/requests/{request.pk}/reopen",
            {
                "expected_version": request.version,
                "quantities": [{"request_line_id": str(request.lines.get().pk), "quantity": 2}],
                "reason": "Deux unités à apporter.",
                "verification": True,
            },
            self.admin_client,
        )
        request.refresh_from_db()
        response = self.deliver(request, [2])
        self.assertEqual(response.status_code, 201, response.content)
        request.refresh_from_db()
        self.assertEqual(request.closure_reason, "REMAINDER_ABANDONED")
        self.assert_request_consistent(request)

    def test_correction_blocked_by_other_reservations_creates_hold_only_on_confirmation(self):
        request, products, delivery, case = self.completed()
        self.correct(delivery, case, [8])
        self.send(products, [1])
        body = self.correction_body(delivery, case, [10])
        before = StockEntry.objects.count()
        preview = self.preview_correct(body)
        self.assertEqual(preview.status_code, 409, preview.content)
        self.assertFalse(StockHold.objects.exists())
        body["preview_hash"] = "unavailable-due-to-conflict"
        identifier = uuid.uuid4()
        key = uuid.uuid4().hex
        response = self.post_json(
            "/api/v1/corrections/confirm", body, self.admin_client, identifier=identifier, key=key
        )
        self.assertEqual(response.status_code, 409, response.content)
        self.assertIn("case_id", response.json())
        self.assertTrue(StockHold.objects.exists())
        self.assertEqual(StockEntry.objects.count(), before)
        repeated = self.post_json(
            "/api/v1/corrections/confirm", body, self.admin_client, identifier=identifier, key=key
        )
        self.assertEqual(response.json()["case_id"], repeated.json()["case_id"])
        self.assertEqual(self.stock(products[0])[:3], (2, 1, 1))

    def test_under_hold_incompatible_ops_refused_then_allowed_after_reconciliation(self):
        request, products, case = self.conflict()
        self.assertEqual(self.send(products, [1]).status_code, 409)
        self.assertEqual(self.adjust(products, [9]).status_code, 409)
        self.reconcile(self.reconciliation_body(request, products[0], case, release=3))
        self.assertEqual(self.send(products, [1]).status_code, 201)
        self.assertEqual(self.stock(products[0])[:3], (6, 6, 0))

    def test_reconciliation_multiple_requests_user_selects_only_one(self):
        request, products = self.requested((4,))
        other_response = self.send(products, [4])
        other = InternalRequest.objects.get(pk=other_response.json()["resource"]["id"])
        response = self.adjust(products, [6])
        case = Case.objects.get(pk=response.json()["case_id"])
        body = self.reconciliation_body(request, products[0], case, release=2)
        self.reconcile(body)
        self.assertEqual(request.lines.get().reservation.remaining, 2)
        self.assertEqual(other.lines.get().reservation.remaining, 4)
        self.assertEqual(self.stock(products[0])[:3], (6, 6, 0))
        self.assert_request_consistent(request)
        self.assert_request_consistent(other)

    def test_reconciliation_invalid_line_rolls_back_whole_batch(self):
        request, products, case = self.conflict()
        body = self.reconciliation_body(request, products[0], case)
        body["commitments"][0]["release_quantity"] = 9
        response = self.post_json("/api/v1/stock-reconciliations/confirm", body, self.admin_client)
        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(self.stock(products[0])[:3], (10, 8, 2))
        self.assertEqual(StockReconciliation.objects.count(), 0)
        self.assertTrue(StockHold.objects.exists())

    def test_concurrent_same_correction_intention_exactly_once(self):
        from apps.core.policies import ActorContext

        request, products, delivery, case = self.completed()
        body = self.correction_body(delivery, case, [8])
        body["preview_hash"] = self.preview_correct(body).json()["preview"]["preview_hash"]
        context = ActorContext(self.admin.pk, self.admin.auth_version)
        binding = self.intention("corrections.confirm", body, resource=delivery.pk, context=context)
        outcomes = self.race([(context, body, *binding)] * 20)
        self.assertEqual({o.status for o in outcomes}, {200})
        self.assertEqual(len({o.body["operation_id"] for o in outcomes}), 1)
        self.assertEqual(DeliveryCorrection.objects.count(), 1)
        self.assert_request_consistent(request)

    def test_concurrent_distinct_corrections_one_version_wins(self):
        from apps.core.policies import ActorContext

        request, products, delivery, case = self.completed()
        context = ActorContext(self.admin.pk, self.admin.auth_version)
        items = []
        for quantity in [8, 7]:
            body = self.correction_body(delivery, case, [quantity])
            body["preview_hash"] = self.preview_correct(body).json()["preview"]["preview_hash"]
            items.append(
                (
                    context,
                    body,
                    *self.intention(
                        "corrections.confirm", body, resource=delivery.pk, context=context
                    ),
                )
            )
        results = self.race(items)
        self.assertEqual(sorted(o.status for o in results), [200, 409])
        self.assertEqual(DeliveryCorrection.objects.count(), 1)
        self.assert_request_consistent(request)

    def test_reconciliation_races_with_new_request_no_overreservation(self):
        from apps.core.policies import ActorContext

        request, products, case = self.conflict()
        body = self.reconciliation_body(request, products[0], case)
        body["preview_hash"] = self.post_json(
            "/api/v1/stock-reconciliations/preview", body, self.admin_client
        ).json()["preview"]["preview_hash"]
        admin = ActorContext(self.admin.pk, self.admin.auth_version)
        assistant = ActorContext(self.assistant.pk, self.assistant.auth_version)
        selection = {"lines": [{"product_id": str(products[0].pk), "quantity": 1}]}
        items = [
            (
                admin,
                body,
                *self.intention(
                    "stock_reconciliations.confirm", body, resource=case.pk, context=admin
                ),
            ),
            (
                assistant,
                selection,
                *self.intention("requests.create", selection, context=assistant),
            ),
        ]
        results = self.race(items)
        self.assertEqual(results[0].status, 200)
        self.assertEqual(results[1].status, 409)
        self.assertEqual(self.stock(products[0])[:3], (6, 6, 0))
        self.assert_request_consistent(request)

    def test_reassignment_and_delivery_concurrent_one_old_version_succeeds(self):
        from apps.accounts.models import User
        from apps.core.policies import ActorContext
        from tests.test_accounts import FICTIVE_PASSWORD

        request, products = self.prepare()
        other = User.objects.create_user(
            "reappro-course", FICTIVE_PASSWORD, role="REPLENISHER", must_change_password=False
        )
        admin = ActorContext(self.admin.pk, self.admin.auth_version)
        change = {
            "expected_version": request.version,
            "new_operator_id": str(other.pk),
            "verification": True,
        }
        hand = self.delivery_payload(request)
        items = [
            (
                admin,
                change,
                *self.intention("requests.reassign", change, resource=request.pk, context=admin),
            ),
            (
                self.context,
                hand,
                *self.intention(
                    "requests.deliver", hand, resource=request.pk, context=self.context
                ),
            ),
        ]
        results = self.race(items)
        self.assertEqual(sum(o.status in [200, 201] for o in results), 1)
        self.assertEqual(sum(o.status == 409 for o in results), 1)
        self.assert_request_consistent(request)

    def test_reconciliation_stale_preview_after_allowed_stock_entry(self):
        request, products, case = self.conflict()
        body = self.reconciliation_body(request, products[0], case)
        body["preview_hash"] = self.post_json(
            "/api/v1/stock-reconciliations/preview", body, self.admin_client
        ).json()["preview"]["preview_hash"]
        self.adjust(products, [11])
        r = self.post_json("/api/v1/stock-reconciliations/confirm", body, self.admin_client)
        self.assertEqual(r.status_code, 409, r.content)
        self.assertTrue(StockHold.objects.exists())
        self.assertEqual(self.stock(products[0])[:3], (11, 8, 3))

    def test_admin_opens_delivery_verification_and_read_does_not_resolve(self):
        request, products = self.prepare()
        self.deliver(request)
        delivery = Delivery.objects.get(request=request)
        response = self.post_json(
            "/api/v1/cases",
            {
                "kind": "DELIVERY_ERROR",
                "source_id": str(delivery.pk),
                "verification": True,
                "message": "Contrôle Gestionnaire fictif.",
            },
            self.admin_client,
        )
        self.assertEqual(response.status_code, 200, response.content)
        case = Case.objects.get(pk=response.json()["resource"]["id"])
        self.assertEqual(self.admin_client.get(f"/api/v1/cases/{case.pk}").status_code, 200)
        case.refresh_from_db()
        self.assertEqual(case.status, "OPEN")
        self.assertEqual(DeliveryLine.objects.get(delivery=delivery).handed, 4)

    def test_invalid_reopen_quantities_types_rejected_as_input(self):
        request, products, delivery, case = self.completed()
        for value in [None, 3, {}, True]:
            body = self.correction_body(delivery, case, [8], "REOPEN")
            body["reopen_quantities"] = value
            response = self.preview_correct(body)
            self.assertEqual(response.status_code, 400, response.content)

    def test_cancel_reduced_request_before_first_delivery(self):
        request, products, case = self.conflict()
        self.reconcile(self.reconciliation_body(request, products[0], case))
        request.refresh_from_db()
        response = self.cancel_request(request)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self.stock(products[0])[:3], (6, 0, 6))
        self.assert_request_consistent(request)

    def test_refuse_reduced_request_before_first_delivery(self):
        request, products, case = self.conflict()
        self.reconcile(self.reconciliation_body(request, products[0], case))
        request.refresh_from_db()
        self.assertEqual(self.claim(request).status_code, 200)
        request.refresh_from_db()
        r = self.post_json(
            f"/api/v1/requests/{request.pk}/refuse",
            {"expected_version": request.version, "message": ""},
            self.operator,
        )
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(self.stock(products[0])[:3], (6, 0, 6))
        self.assert_request_consistent(request)

    def test_reconcile_verification_of_unchanged_stock_has_no_fake_movement(self):
        request, products, case = self.conflict()
        before = StockEntry.objects.count()
        self.reconcile(self.reconciliation_body(request, products[0], case, release=0, count=10))
        self.assertEqual(StockEntry.objects.count(), before)
        self.assertFalse(StockHold.objects.exists())
        self.assertEqual(self.stock(products[0])[:3], (10, 8, 2))

    def test_second_conflict_after_effective_resolution_preserves_both_histories(self):
        request, products, case = self.conflict()
        self.reconcile(self.reconciliation_body(request, products[0], case))
        r = self.adjust(products, [5])
        self.assertEqual(r.status_code, 409, r.content)
        self.assertNotEqual(r.json()["case_id"], str(case.pk))
        self.assertEqual(Case.objects.filter(kind="STOCK_CONFLICT").count(), 2)
        case.refresh_from_db()
        self.assertEqual(case.status, "RESOLVED")

    def test_shortage_case_can_resolve_only_after_explicit_matching_count(self):
        request, products = self.prepare()
        self.deliver(request, [0], "SHORTAGE")
        case = Case.objects.get(kind="SHORTAGE")
        product = products[0]
        body = {
            "expected_version": 0,
            "verification": True,
            "message": "Écart initial vérifié, recomptage réel fictif.",
            "product_counts": [
                {
                    "product_id": str(product.pk),
                    "expected_stock_version": self.stock(product)[3],
                    "counted_quantity": 6,
                }
            ],
        }
        bad = {**body, "product_counts": [{**body["product_counts"][0], "counted_quantity": 5}]}
        self.assertEqual(
            self.post_json(f"/api/v1/cases/{case.pk}/resolve", bad, self.admin_client).status_code,
            409,
        )
        before = StockEntry.objects.count()
        r = self.post_json(f"/api/v1/cases/{case.pk}/resolve", body, self.admin_client)
        self.assertEqual(r.status_code, 200, r.content)
        case.refresh_from_db()
        self.assertEqual(case.status, "RESOLVED")
        self.assertEqual(StockEntry.objects.count(), before)
        self.assert_request_consistent(request)

    def test_request_action_preview_invalidated_by_stock_change(self):
        request, products, delivery, case = self.completed()
        self.correct(delivery, case, [8])
        request.refresh_from_db()
        body = {
            "expected_version": request.version,
            "quantities": [{"request_line_id": str(request.lines.get().pk), "quantity": 1}],
            "reason": "Une unité à remettre après vérification.",
            "verification": True,
        }
        r = self.post_json(f"/api/v1/requests/{request.pk}/reopen/preview", body, self.admin_client)
        self.assertEqual(r.status_code, 200, r.content)
        body["preview_hash"] = r.json()["preview"]["preview_hash"]
        self.adjust(products, [3])
        r = self.post_json(f"/api/v1/requests/{request.pk}/reopen", body, self.admin_client)
        self.assertEqual(r.status_code, 409, r.content)
        self.assertEqual(self.stock(products[0])[:3], (3, 0, 3))

    def test_sql_rejects_forged_correction_before_snapshot(self):
        from apps.requests.models import DecisionLine

        request, products, delivery, case = self.completed()
        body = self.correction_body(delivery, case, [8])
        body["preview_hash"] = self.preview_correct(body).json()["preview"]["preview_hash"]
        original = DecisionLine.objects.create

        def forge(**kwargs):
            kwargs["delivered_before"] -= 1
            kwargs["delivered_after"] -= 1
            kwargs["closed_before"] += 1
            kwargs["closed_after"] += 1
            return original(**kwargs)

        with patch("apps.requests.corrections.DecisionLine.objects.create", side_effect=forge):
            r = self.post_json("/api/v1/corrections/confirm", body, self.admin_client)
        self.assertEqual(r.status_code, 503, r.content)
        self.assertEqual(DeliveryCorrection.objects.count(), 0)
        self.assertEqual(self.stock(products[0])[:3], (0, 0, 0))

    def test_sql_rejects_forged_reconciliation_reserved_snapshot(self):
        from apps.cases.models import ReconciledStock

        request, products, case = self.conflict()
        body = self.reconciliation_body(request, products[0], case, count=10)
        r = self.post_json("/api/v1/stock-reconciliations/preview", body, self.admin_client)
        body["preview_hash"] = r.json()["preview"]["preview_hash"]
        original = ReconciledStock.objects.create

        def forge(**kwargs):
            kwargs["reserved_before"] += 1
            kwargs["reserved_after"] += 1
            return original(**kwargs)

        with patch(
            "apps.inventory.reconciliation.ReconciledStock.objects.create", side_effect=forge
        ):
            r = self.post_json("/api/v1/stock-reconciliations/confirm", body, self.admin_client)
        self.assertEqual(r.status_code, 503, r.content)
        self.assertEqual(self.stock(products[0])[:3], (10, 8, 2))
        self.assertTrue(StockHold.objects.exists())

    def test_reconciliation_races_with_compatible_physical_increase(self):
        from apps.core.policies import ActorContext

        request, products, case = self.conflict()
        body = self.reconciliation_body(request, products[0], case)
        body["preview_hash"] = self.post_json(
            "/api/v1/stock-reconciliations/preview", body, self.admin_client
        ).json()["preview"]["preview_hash"]
        admin = ActorContext(self.admin.pk, self.admin.auth_version)
        update = self.adjustment_payload(products, [11])
        binding = self.intention(
            "stock_reconciliations.confirm", body, resource=case.pk, context=admin
        )
        # Une mutation concurrente via HTTP utilise sa connexion distincte.
        import threading
        from concurrent.futures import ThreadPoolExecutor

        from django.db import close_old_connections, connection

        from apps.core.idempotency import execute_intent

        barrier = threading.Barrier(2, timeout=10)

        def action(which):
            close_old_connections()
            try:
                barrier.wait()
                if which == 0:
                    return execute_intent(
                        admin, intent_id=binding[0], key=binding[1], payload=body
                    ).status
                return self.post_json(
                    "/api/v1/stocks/adjustments", update, self.operator
                ).status_code
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            statuses = list(pool.map(action, [0, 1]))
        self.assertTrue(all(s in [200, 409] for s in statuses), statuses)
        self.assertEqual(sorted(statuses), [200, 409])
        self.assert_request_consistent(request)

    def test_runtime_database_role_can_correct_reconcile_and_cannot_delete_facts(self):
        import os

        from django.db import connection

        from apps.core.management.commands.grant_runtime import grant_runtime

        request, products, delivery, case = self.completed()
        product_runtime = self.demandable_product(
            code="RUNTIME-RECONCILIATION", physical=10, maximum=10
        )
        created = self.send([product_runtime], [8])
        request_runtime = InternalRequest.objects.get(pk=created.json()["resource"]["id"])
        conflict_response = self.adjust([product_runtime], [6])
        case_runtime = Case.objects.get(pk=conflict_response.json()["case_id"])
        reconciliation_payload = self.reconciliation_body(
            request_runtime, product_runtime, case_runtime
        )
        grant_runtime(os.environ["RUNTIME_DATABASE_USER"])
        original = connection.settings_dict.copy()
        connection.close()
        connection.settings_dict.update(
            USER=os.environ["RUNTIME_DATABASE_USER"],
            PASSWORD=os.environ["RUNTIME_DATABASE_PASSWORD"],
        )
        try:
            self.correct(delivery, case, [8])
            self.assertEqual(self.stock(products[0])[:3], (2, 0, 2))
            self.reconcile(reconciliation_payload)
            self.assertEqual(self.stock(product_runtime)[:3], (6, 6, 0))
            self.assert_request_consistent(request_runtime)
            for sql in [
                "DELETE FROM requests_deliverycorrection",
                "UPDATE requests_requestdecision SET verification=false",
                "DELETE FROM cases_caseevent",
            ]:
                with (
                    self.assertRaises(DatabaseError),
                    transaction.atomic(),
                    connection.cursor() as cursor,
                ):
                    cursor.execute(sql)
        finally:
            connection.close()
            connection.settings_dict.update(original)

    def test_reconciliation_two_requests_one_operation_audit_and_two_notifications(self):
        from apps.requests.models import RequestEvent

        request, products = self.requested((4,))
        created = self.send(products, [4])
        other = InternalRequest.objects.get(pk=created.json()["resource"]["id"])
        refusal = self.adjust(products, [6])
        case = Case.objects.get(pk=refusal.json()["case_id"])
        body = self.reconciliation_body(request, products[0], case, release=1)
        body["commitments"].append(
            {
                "request_id": str(other.pk),
                "request_line_id": str(other.lines.get().pk),
                "expected_request_version": other.version,
                "release_quantity": 1,
            }
        )
        result, _ = self.reconcile(body)
        op = result.json()["operation_id"]
        self.assertEqual(RequestEvent.objects.filter(operation_id=op, kind="RECONCILED").count(), 2)
        self.assertEqual(AuditEvent.objects.filter(operation_id=op).count(), 1)
        self.assertEqual(self.stock(products[0])[:3], (6, 6, 0))
        self.assert_request_consistent(request)
        self.assert_request_consistent(other)

    def test_reconciliation_multi_product_complete_atomic_lot(self):
        request, products = self.requested((8, 8))
        refusal = self.adjust(products, [6, 6])
        case = Case.objects.get(pk=refusal.json()["case_id"])
        rows = list(request.lines.order_by("product_code"))
        body = {
            "case_id": str(case.pk),
            "expected_case_version": 0,
            "product_counts": [
                {
                    "product_id": str(p.pk),
                    "expected_stock_version": self.stock(p)[3],
                    "counted_quantity": 6,
                }
                for p in products
            ],
            "commitments": [
                {
                    "request_id": str(request.pk),
                    "request_line_id": str(line.pk),
                    "expected_request_version": request.version,
                    "release_quantity": 2,
                }
                for line in rows
            ],
            "verification": True,
            "message": "Les deux produits et engagements ont été vérifiés.",
        }
        self.reconcile(body)
        self.assertEqual([self.stock(p)[:3] for p in products], [(6, 6, 0)] * 2)
        self.assertFalse(StockHold.objects.exists())
        self.assert_request_consistent(request)

    def test_concurrent_case_opening_is_business_conflict_without_duplicate(self):
        from apps.core.policies import ActorContext

        request, products = self.prepare()
        self.deliver(request)
        delivery = Delivery.objects.get(request=request)
        admin = ActorContext(self.admin.pk, self.admin.auth_version)
        body = {
            "kind": "DELIVERY_ERROR",
            "source_id": str(delivery.pk),
            "verification": True,
            "message": "Vérification administrative fictive simultanée.",
        }
        items = [
            (admin, body, *self.intention("cases.open", body, context=admin)) for _ in range(2)
        ]
        results = self.race(items)
        self.assertEqual(sorted(r.status for r in results), [200, 409])
        self.assertEqual(Case.objects.filter(delivery=delivery).count(), 1)

    def test_all_m6_routes_enforce_roles_and_own_case_reads(self):
        request, products, delivery, case = self.completed()
        for client in [self.operator, self.user_client]:
            for suffix, body in [
                (
                    "close-remainder",
                    {
                        "expected_version": request.version,
                        "reason": "Interdit",
                        "verification": True,
                    },
                ),
                (
                    "reopen",
                    {
                        "expected_version": request.version,
                        "quantities": [],
                        "reason": "Interdit",
                        "verification": True,
                    },
                ),
                (
                    "reassign",
                    {
                        "expected_version": request.version,
                        "new_operator_id": str(self.replenisher.pk),
                        "verification": True,
                    },
                ),
            ]:
                self.assertEqual(
                    self.post_json(
                        f"/api/v1/requests/{request.pk}/{suffix}", body, client
                    ).status_code,
                    403,
                )
            self.assertEqual(client.get(f"/dossiers/{case.pk}/corriger").status_code, 403)
            self.assertEqual(
                self.post_json(
                    f"/api/v1/cases/{case.pk}/resolve",
                    {
                        "expected_version": 0,
                        "verification": True,
                        "message": "Interdit",
                        "product_counts": [],
                    },
                    client,
                ).status_code,
                403,
            )
            self.assertEqual(
                self.post_json(
                    "/api/v1/stock-reconciliations/preview", {"case_id": str(case.pk)}, client
                ).status_code,
                403,
            )
        self.assertEqual(self.user_client.get(f"/api/v1/cases/{case.pk}").status_code, 403)
        self.assertEqual(self.operator.get(f"/api/v1/cases/{case.pk}").status_code, 200)
        self.assertEqual(DeliveryCorrection.objects.count(), 0)

    def test_impossible_negative_projection_is_only_diagnostic_and_opens_hold(self):
        request, products, delivery, case = self.completed()
        self.correct(delivery, case, [6])
        self.adjust(products, [2])
        body = self.correction_body(delivery, case, [9])
        body["preview_hash"] = "not-confirmable"
        before = StockEntry.objects.count()
        r = self.post_json("/api/v1/corrections/confirm", body, self.admin_client)
        self.assertEqual(r.status_code, 409, r.content)
        self.assertIn("case_id", r.json())
        conflict = Case.objects.get(pk=r.json()["case_id"])
        record = conflict.stock_case.products.get()
        self.assertTrue(record.is_calculated)
        self.assertEqual(record.attempted_physical, -1)
        case.refresh_from_db()
        self.assertEqual(case.status, "UNDER_REVIEW")
        self.assertEqual(self.stock(products[0])[:3], (2, 0, 2))
        self.assertEqual(StockEntry.objects.count(), before)
        self.assertTrue(StockHold.objects.exists())
        self.assertEqual(self.send(products, [1]).status_code, 409)
        body = self.reconciliation_body(request, products[0], conflict, release=0, count=2)
        body["commitments"] = []
        self.reconcile(body)
        self.assertFalse(StockHold.objects.exists())
        self.assert_request_consistent(request)
