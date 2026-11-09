"""M5 : PostgreSQL réel, API, faits immuables et courses de confirmation."""

import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from django.db import DatabaseError, close_old_connections, connection, transaction
from django.test import TransactionTestCase

from apps.accounts.models import User
from apps.audit.models import AuditEvent
from apps.core import idempotency
from apps.core.management.commands.grant_runtime import grant_runtime
from apps.core.models import Operation, OperationIntent
from apps.core.policies import ActorContext
from apps.inventory.models import StockEntry
from apps.notifications.models import BusinessEvent, Outbox
from apps.requests import delivery_services
from apps.requests.models import (
    Delivery,
    DeliveryIssue,
    DeliveryLine,
    InternalRequest,
    RequestEvent,
    RequestLine,
)
from tests.test_accounts import FICTIVE_PASSWORD
from tests.test_requests import RequestFixtures


class DeliveryFixtures(RequestFixtures):
    def requested(self, quantities=(4,), physical=10):
        products = [
            self.demandable_product(code=f"LIV-{i}", physical=physical, maximum=10)
            for i in range(len(quantities))
        ]
        response = self.send(products, quantities)
        self.assertEqual(response.status_code, 201, response.content)
        request = InternalRequest.objects.get(pk=response.json()["resource"]["id"])
        return request, products

    def claim(self, request, client=None, version=None):
        return self.post_json(
            f"/api/v1/requests/{request.pk}/claim",
            {"expected_version": request.version if version is None else version},
            client or self.operator,
        )

    def prepare(self, quantities=(4,), physical=10):
        request, products = self.requested(quantities, physical)
        self.assertEqual(self.claim(request).status_code, 200)
        request.refresh_from_db()
        return request, products

    def delivery_payload(self, request, handed=None, mode="COMPLETE", missing=None):
        lines = list(request.lines.order_by("product_code"))
        if handed is None:
            handed = [line.reservation.remaining for line in lines]
        if missing is None:
            missing = [
                line.reservation.remaining - y if mode == "SHORTAGE" else 0
                for line, y in zip(lines, handed, strict=True)
            ]
        return {
            "expected_version": request.version,
            "mode": mode,
            "lines": [
                {"request_line_id": str(line.pk), "delivered_quantity": y, "missing_quantity": m}
                for line, y, m in zip(lines, handed, missing, strict=True)
            ],
        }

    def deliver(
        self,
        request,
        handed=None,
        mode="COMPLETE",
        missing=None,
        client=None,
        identifier=None,
        key=None,
    ):
        return self.post_json(
            f"/api/v1/requests/{request.pk}/deliveries",
            self.delivery_payload(request, handed, mode, missing),
            client or self.operator,
            identifier=identifier,
            key=key,
        )

    def assert_request_consistent(self, request):
        request.refresh_from_db()
        with connection.cursor() as cursor:
            cursor.execute("SELECT reserve_assert_request(%s)", [request.pk])
            for line in request.lines.all():
                cursor.execute(
                    "SELECT reserve_assert_stock(%s),reserve_assert_reservations(%s)",
                    [line.product_id, line.product_id],
                )
                self.assertEqual(
                    line.quantity, line.delivered + line.closed + line.reservation.remaining
                )

    def race(self, items):
        barrier = threading.Barrier(len(items), timeout=10)

        def run(item):
            close_old_connections()
            try:
                context, payload, identifier, key = item
                barrier.wait()
                return idempotency.execute_intent(
                    context, intent_id=identifier, key=key, payload=payload
                )
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=len(items)) as pool:
            return list(pool.map(run, items))


class DeliveryTests(DeliveryFixtures, TransactionTestCase):
    def test_claim_changes_state_and_keeps_quantities(self):
        request, products = self.requested()
        response = self.claim(request)
        self.assertEqual(response.status_code, 200, response.content)
        request.refresh_from_db()
        self.assertEqual(
            (request.status, request.assigned_to_id, request.version),
            ("PREPARING", self.replenisher.pk, 1),
        )
        self.assertEqual(self.stock(products[0])[:3], (10, 4, 6))
        self.assert_request_consistent(request)

    def test_concurrent_claims_have_single_winner(self):
        request, _ = self.requested()
        other = User.objects.create_user(
            "autre-reappro", FICTIVE_PASSWORD, role="REPLENISHER", must_change_password=False
        )
        contexts = [self.context, ActorContext(other.pk, other.auth_version)]
        payload = {"expected_version": 0}
        bindings = [
            self.intention("requests.claim", payload, resource=request.pk, context=c)
            for c in contexts
        ]
        outcomes = self.race([(c, payload, *b) for c, b in zip(contexts, bindings, strict=True)])
        self.assertEqual(sorted(x.status for x in outcomes), [200, 409])
        self.assertEqual(RequestEvent.objects.filter(kind="CLAIMED").count(), 1)
        self.assert_request_consistent(request)

    def test_complete_delivery_deducts_once_no_stocks_adjustment(self):
        request, products = self.prepare((3, 2))
        response = self.deliver(request)
        self.assertEqual(response.status_code, 201, response.content)
        request.refresh_from_db()
        self.assertEqual(request.status, "DELIVERED")
        self.assertEqual([self.stock(p)[:3] for p in products], [(7, 0, 7), (8, 0, 8)])
        self.assertEqual(StockEntry.objects.filter(kind="DELIVER").count(), 2)
        self.assertFalse(StockEntry.objects.filter(kind="MANUAL_ADJUSTMENT").exists())
        self.assert_request_consistent(request)

    def test_partial_delivery_keeps_remainder_reserved(self):
        request, products = self.prepare()
        response = self.deliver(request, [1], "LATER")
        self.assertEqual(response.status_code, 201, response.content)
        request.refresh_from_db()
        self.assertEqual(request.status, "PARTIAL_OPEN")
        self.assertEqual(self.stock(products[0])[:3], (9, 3, 6))
        self.assertEqual(request.lines.get().reservation.remaining, 3)
        self.assert_request_consistent(request)

    def test_successive_deliveries_and_no_over_delivery(self):
        request, products = self.prepare()
        self.assertEqual(self.deliver(request, [1], "LATER").status_code, 201)
        request.refresh_from_db()
        self.assertEqual(self.deliver(request, [4]).status_code, 409)
        self.assertEqual(self.deliver(request, [3]).status_code, 201)
        self.assertEqual(Delivery.objects.count(), 2)
        self.assertEqual(self.stock(products[0])[:3], (6, 0, 6))
        self.assert_request_consistent(request)

    def test_shortage_does_not_empty_reserve_or_other_reservations(self):
        request, products = self.prepare()
        self.assertEqual(self.send(products, [3]).status_code, 201)
        response = self.deliver(request, [2], "SHORTAGE")
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(self.stock(products[0])[:3], (6, 3, 3))
        request.refresh_from_db()
        self.assertEqual((request.status, request.closure_reason), ("PARTIAL_CLOSED", "SHORTAGE"))
        line = request.lines.get()
        self.assertEqual((line.delivered, line.closed, line.reservation.remaining), (2, 2, 0))
        self.assertEqual(DeliveryIssue.objects.get().kind, "SHORTAGE")
        self.assertEqual(StockEntry.objects.get(kind="SHORTAGE").delta_p, -2)
        self.assert_request_consistent(request)

    def test_zero_handed_shortage_is_closure_not_positive_delivery(self):
        request, products = self.prepare()
        response = self.deliver(request, [0], "SHORTAGE")
        self.assertEqual(response.status_code, 201, response.content)
        request.refresh_from_db()
        self.assertEqual(response.json()["resource"]["status_label"], "Clôturée — manque")
        self.assertEqual(self.stock(products[0])[:3], (6, 0, 6))
        self.assertFalse(StockEntry.objects.filter(kind="DELIVER").exists())
        self.assertEqual(request.lines.get().delivered, 0)
        self.assert_request_consistent(request)

    def test_multiline_homogeneous_shortage_explicit_each_line(self):
        request, products = self.prepare((4, 3))
        response = self.deliver(request, [2, 0], "SHORTAGE")
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual([self.stock(p)[:3] for p in products], [(6, 0, 6), (7, 0, 7)])
        self.assert_request_consistent(request)

    def test_mixed_remainders_and_implicit_closure_refused_atomically(self):
        request, products = self.prepare((4, 3))
        response = self.deliver(request, [2, 0], "SHORTAGE", [2, 0])
        self.assertEqual(response.status_code, 409, response.content)
        self.assertEqual(response.json()["error"]["code"], "MIXED_REMAINDERS_NOT_SUPPORTED")
        payload = self.delivery_payload(request, [2, 0], "SHORTAGE")
        payload["lines"].pop()
        self.assertEqual(
            self.post_json(
                f"/api/v1/requests/{request.pk}/deliveries", payload, self.operator
            ).status_code,
            400,
        )
        self.assertFalse(Delivery.objects.exists())
        self.assertEqual([self.stock(p)[:3] for p in products], [(10, 4, 6), (10, 3, 7)])

    def test_assistant_can_cancel_preparing_but_never_after_first_delivery(self):
        request, products = self.prepare()
        self.assertEqual(self.cancel_request(request).status_code, 200)
        self.assertEqual(self.stock(products[0])[:3], (10, 0, 10))
        request2 = self.send(products, [2])
        row = InternalRequest.objects.get(pk=request2.json()["resource"]["id"])
        self.claim(row)
        row.refresh_from_db()
        self.deliver(row, [1], "LATER")
        row.refresh_from_db()
        self.assertEqual(self.cancel_request(row).status_code, 409)
        self.assertEqual(self.stock(products[0])[:3], (9, 1, 8))

    def test_cancellation_and_delivery_concurrent_only_one_commits(self):
        request, products = self.prepare()
        assistant = ActorContext(self.assistant.pk, self.assistant.auth_version)
        cancel = {"expected_version": request.version}
        delivery = self.delivery_payload(request)
        bindings = [
            self.intention(kind, payload, resource=request.pk, context=c)
            for kind, payload, c in [
                ("requests.cancel", cancel, assistant),
                ("requests.deliver", delivery, self.context),
            ]
        ]
        outcomes = self.race(
            [
                (c, payload, *b)
                for c, payload, b in zip(
                    [assistant, self.context], [cancel, delivery], bindings, strict=True
                )
            ]
        )
        self.assertEqual(
            sorted(x.status for x in outcomes), [200 if not Delivery.objects.exists() else 201, 409]
        )
        self.assertEqual(
            self.stock(products[0])[:3], (6, 0, 6) if Delivery.objects.exists() else (10, 0, 10)
        )
        self.assert_request_consistent(request)

    def test_twenty_same_intention_confirmations_make_one_delivery(self):
        request, products = self.prepare()
        payload = self.delivery_payload(request)
        identifier, key = self.intention("requests.deliver", payload, resource=request.pk)
        outcomes = self.race([(self.context, payload, identifier, key)] * 20)
        self.assertEqual([x.status for x in outcomes], [201] * 20)
        self.assertEqual(len({x.body["operation_id"] for x in outcomes}), 1)
        self.assertEqual(Delivery.objects.count(), 1)
        self.assertEqual(StockEntry.objects.filter(kind="DELIVER").count(), 1)
        self.assertEqual(self.stock(products[0])[:3], (6, 0, 6))

    def test_lost_response_read_and_replay_same_delivery(self):
        request, products = self.prepare()
        identifier, key = uuid.uuid4(), uuid.uuid4().hex
        first = self.deliver(request, [4], identifier=identifier, key=key)
        self.assertEqual(first.status_code, 201, first.content)
        state = self.operator.get(f"/api/v1/operation-intents/{identifier}")
        self.assertEqual(state.json()["state"], "CONFIRMED")
        replay = self.deliver(request, [4], identifier=identifier, key=key)
        self.assertEqual(first.json(), replay.json())
        self.assertEqual(self.stock(products[0])[:3], (6, 0, 6))

    def test_old_version_or_changed_idempotency_content_refused(self):
        request, _ = self.prepare()
        identifier, key = uuid.uuid4(), uuid.uuid4().hex
        self.assertEqual(
            self.deliver(request, [1], "LATER", identifier=identifier, key=key).status_code, 201
        )
        self.assertEqual(
            self.deliver(request, [2], "LATER", identifier=identifier, key=key).status_code, 409
        )
        self.assertEqual(self.deliver(request, [1], "LATER").status_code, 409)
        self.assertEqual(Delivery.objects.count(), 1)

    def test_nonassigned_and_admin_assistant_cannot_deliver(self):
        request, _ = self.prepare()
        other = User.objects.create_user(
            "non-attribue", FICTIVE_PASSWORD, role="REPLENISHER", must_change_password=False
        )
        for client in [self.logged(other), self.user_client, self.admin_client]:
            self.assertEqual(self.deliver(request, client=client).status_code, 403)
        self.assertFalse(Delivery.objects.exists())

    def test_product_on_hold_blocks_whole_multiline_delivery(self):
        request, products = self.prepare((4, 3))
        result = self.adjust([products[1]], [0])
        self.assertEqual(result.status_code, 409)
        result = self.deliver(request)
        self.assertEqual(result.status_code, 409, result.content)
        self.assertFalse(Delivery.objects.exists())
        self.assertEqual([self.stock(p)[:3] for p in products], [(10, 4, 6), (10, 3, 7)])

    def test_inventory_discrepancy_larger_than_own_missing_creates_separate_hold(self):
        request, products = self.prepare()
        self.send(products, [3])
        self.assertEqual(self.deliver(request, [2], "SHORTAGE").status_code, 201)
        result = self.adjust(products, [1])
        self.assertEqual(result.status_code, 409)
        self.assertIn("case_id", result.json())
        self.assertEqual(self.stock(products[0])[:3], (6, 3, 3))
        self.assertEqual(Delivery.objects.count(), 1)
        self.assert_request_consistent(request)

    def test_error_report_preserves_original_and_is_idempotent(self):
        request, products = self.prepare()
        self.deliver(request)
        delivery = Delivery.objects.get()
        before = self.stock(products[0])
        identifier, key = uuid.uuid4(), uuid.uuid4().hex
        path = f"/api/v1/deliveries/{delivery.pk}/report-error"
        first = self.post_json(
            path,
            {"message": "Déclaration fictive à vérifier"},
            self.operator,
            identifier=identifier,
            key=key,
        )
        self.assertEqual(first.status_code, 201, first.content)
        self.assertEqual(
            self.post_json(
                path,
                {"message": "Déclaration fictive à vérifier"},
                self.operator,
                identifier=identifier,
                key=key,
            ).json(),
            first.json(),
        )
        self.assertEqual(self.post_json(path, {"message": "Autre"}, self.operator).status_code, 409)
        self.assertEqual(DeliveryIssue.objects.count(), 1)
        self.assertEqual(self.stock(products[0]), before)
        self.assertEqual(DeliveryLine.objects.get().handed, 4)

    def test_issue_permissions_queue_and_personal_history(self):
        request, _ = self.prepare()
        self.deliver(request, [0], "SHORTAGE")
        self.assertEqual(self.admin_client.get("/api/v1/delivery-issues").status_code, 200)
        self.assertEqual(len(self.operator.get("/api/v1/delivery-issues").json()["issues"]), 1)
        self.assertEqual(self.user_client.get("/api/v1/delivery-issues").status_code, 403)
        other = User.objects.create_user(
            "lecteur-autre", FICTIVE_PASSWORD, role="REPLENISHER", must_change_password=False
        )
        client = self.logged(other)
        self.assertEqual(client.get(f"/api/v1/requests/{request.pk}").status_code, 404)
        self.assertEqual(client.get("/api/v1/delivery-issues").json()["issues"], [])
        self.assertEqual(client.get("/api/v1/requests/history").json()["events"], [])

    def test_technical_failure_after_first_line_rolls_back_and_same_intent_retries(self):
        request, products = self.prepare((3, 2))
        identifier, key = uuid.uuid4(), uuid.uuid4().hex
        original = delivery_services.append_change

        def fail(*args, **kwargs):
            if kwargs["sequence"] == 2:
                raise RuntimeError("panne fictive")
            return original(*args, **kwargs)

        with patch.object(delivery_services, "append_change", side_effect=fail):
            response = self.deliver(request, identifier=identifier, key=key)
        self.assertEqual(response.status_code, 500)
        self.assertFalse(Delivery.objects.exists())
        self.assertEqual([self.stock(p)[:3] for p in products], [(10, 3, 7), (10, 2, 8)])
        self.assertEqual(OperationIntent.objects.get(pk=identifier).state, "READY")
        self.assertEqual(self.deliver(request, identifier=identifier, key=key).status_code, 201)
        self.assert_request_consistent(request)

    def test_audit_outbox_and_sql_immutable_facts_match(self):
        request, _ = self.prepare()
        response = self.deliver(request)
        self.assertEqual(response.status_code, 201, response.content)
        op = Operation.objects.get(pk=response.json()["operation_id"])
        self.assertTrue(AuditEvent.objects.filter(operation=op).exists())
        self.assertTrue(
            BusinessEvent.objects.filter(operation=op, event_type="request.delivered").exists()
        )
        self.assertEqual(Outbox.objects.filter(event__operation=op).count(), 2)
        for action in [
            lambda: Delivery.objects.update(mode="LATER"),
            lambda: RequestLine.objects.update(delivered=0),
        ]:
            with self.assertRaises(DatabaseError), transaction.atomic():
                action()
        self.assert_request_consistent(request)

    def test_restricted_sql_runtime_can_claim_deliver_and_report(self):
        request, products = self.requested()
        role = os.environ["RUNTIME_DATABASE_USER"]
        grant_runtime(role)
        original = connection.settings_dict.copy()
        connection.close()
        connection.settings_dict.update(USER=role, PASSWORD=os.environ["RUNTIME_DATABASE_PASSWORD"])
        try:
            self.assertEqual(self.claim(request).status_code, 200)
            request.refresh_from_db()
            response = self.deliver(request)
            self.assertEqual(response.status_code, 201, response.content)
            delivery_id = response.json()["resource"]["delivery_id"]
            report = self.post_json(
                f"/api/v1/deliveries/{delivery_id}/report-error",
                {"message": "Erreur fictive"},
                self.operator,
            )
            self.assertEqual(report.status_code, 201, report.content)
        finally:
            connection.close()
            connection.settings_dict.update(original)
        self.assertEqual(self.stock(products[0])[:3], (6, 0, 6))

    def test_outbox_failure_rolls_back_delivery_and_audit(self):
        request, products = self.prepare()
        count = AuditEvent.objects.count()
        with patch.object(
            idempotency.Outbox.objects, "create", side_effect=RuntimeError("panne fictive")
        ):
            response = self.deliver(request)
        self.assertEqual(response.status_code, 500)
        self.assertFalse(Delivery.objects.exists())
        self.assertEqual(AuditEvent.objects.count(), count)
        self.assertEqual(self.stock(products[0])[:3], (10, 4, 6))
        self.assert_request_consistent(request)

    def test_archived_product_keeps_existing_delivery_engagement(self):
        request, products = self.prepare()
        product = products[0]
        product.refresh_from_db()
        response = self.post_json(
            f"/api/v1/products/{product.pk}/archive",
            {"expected_version": product.version, "confirmed": True},
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self.deliver(request).status_code, 201)
        self.assert_request_consistent(request)

    def test_report_other_operator_and_assistant_forbidden(self):
        request, _ = self.prepare()
        self.deliver(request)
        delivery = Delivery.objects.get()
        other = User.objects.create_user(
            "erreur-autre", FICTIVE_PASSWORD, role="REPLENISHER", must_change_password=False
        )
        response = self.post_json(
            f"/api/v1/deliveries/{delivery.pk}/report-error", {"message": ""}, self.logged(other)
        )
        self.assertEqual(response.status_code, 404)
        for client in [self.admin_client, self.user_client]:
            self.assertEqual(
                self.post_json(
                    f"/api/v1/deliveries/{delivery.pk}/report-error", {"message": ""}, client
                ).status_code,
                403,
            )
        self.assertFalse(DeliveryIssue.objects.exists())

    def test_invalid_line_quantity_and_duplicate_ids_leave_no_delivery(self):
        request, products = self.prepare()
        for value in [-1, 1.5, True, 1000000001]:
            payload = self.delivery_payload(request)
            payload["lines"][0]["delivered_quantity"] = value
            response = self.post_json(
                f"/api/v1/requests/{request.pk}/deliveries", payload, self.operator
            )
            self.assertEqual(response.status_code, 400, response.content)
        payload = self.delivery_payload(request)
        payload["lines"].append(payload["lines"][0])
        self.assertEqual(
            self.post_json(
                f"/api/v1/requests/{request.pk}/deliveries", payload, self.operator
            ).status_code,
            400,
        )
        self.assertFalse(Delivery.objects.exists())
        self.assertEqual(self.stock(products[0])[:3], (10, 4, 6))
