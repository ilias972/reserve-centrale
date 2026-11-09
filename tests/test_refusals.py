"""Refus validé par le porteur : aucun retrait physique, aucun contournement d'A01."""

import uuid
from unittest.mock import patch

from django.db import DatabaseError, transaction
from django.test import TransactionTestCase

from apps.accounts.models import User
from apps.audit.models import AuditEvent
from apps.core import idempotency
from apps.core.models import OperationIntent
from apps.core.policies import ActorContext
from apps.inventory.models import StockEntry
from apps.notifications.models import BusinessEvent, Outbox
from apps.requests import refusal
from apps.requests.models import Delivery, RequestEvent
from tests.test_accounts import FICTIVE_PASSWORD
from tests.test_deliveries import DeliveryFixtures


class RefusalTests(DeliveryFixtures, TransactionTestCase):
    def refuse(self, request, message="", client=None, **kwargs):
        return self.post_json(
            f"/api/v1/requests/{request.pk}/refuse",
            {"expected_version": request.version, "message": message},
            client or self.operator,
            **kwargs,
        )

    def test_refusal_releases_all_lines_without_physical_change_or_false_delivery(self):
        request, products = self.prepare((4, 3))
        self.assertEqual(self.send(products, [2, 1]).status_code, 201)
        response = self.refuse(request)
        self.assertEqual(response.status_code, 200, response.content)
        request.refresh_from_db()
        self.assertEqual((request.status, request.closure_reason), ("REFUSED", "REFUSAL"))
        self.assertIsNone(request.first_delivery_operation_id)
        self.assertEqual([self.stock(p)[:3] for p in products], [(10, 2, 8), (10, 1, 9)])
        self.assertFalse(Delivery.objects.exists())
        self.assertEqual(RequestEvent.objects.get(kind="REFUSED").message, "")
        self.assertTrue(
            all(
                line.closed == line.quantity and line.delivered == 0 for line in request.lines.all()
            )
        )
        entries = StockEntry.objects.filter(operation_id=response.json()["operation_id"])
        self.assertEqual(list(entries.values_list("delta_p", flat=True)), [0, 0])
        self.assertTrue(all(entry.kind == "RELEASE" for entry in entries))
        self.assert_request_consistent(request)

    def test_optional_justification_is_immutable_and_visible_only_with_request(self):
        request, _ = self.prepare()
        response = self.refuse(request, "  Explication fictive <script>non exécuté</script>  ")
        self.assertEqual(response.status_code, 200, response.content)
        event = RequestEvent.objects.get(kind="REFUSED")
        self.assertEqual(event.message, "Explication fictive <script>non exécuté</script>")
        for client in [self.operator, self.user_client, self.admin_client]:
            data = client.get(f"/api/v1/requests/{request.pk}").json()
            self.assertEqual(data["events"][0]["message"], event.message)
            page = client.get(f"/demandes/{request.pk}")
            self.assertContains(page, "&lt;script&gt;non exécuté&lt;/script&gt;")
        with self.assertRaises(DatabaseError), transaction.atomic():
            RequestEvent.objects.filter(pk=event.pk).update(message="Réécriture interdite")

    def test_refusal_forbidden_before_claim_and_after_first_partial_delivery(self):
        request, products = self.requested()
        self.assertEqual(self.refuse(request).status_code, 403)
        self.claim(request)
        request.refresh_from_db()
        self.assertEqual(self.deliver(request, [1], "LATER").status_code, 201)
        request.refresh_from_db()
        response = self.refuse(request)
        self.assertEqual(response.status_code, 409, response.content)
        self.assertEqual(response.json()["error"]["code"], "REFUSAL_FORBIDDEN")
        self.assertEqual(self.stock(products[0])[:3], (9, 3, 6))
        self.assertFalse(RequestEvent.objects.filter(kind="REFUSED").exists())
        self.assert_request_consistent(request)

    def test_admin_assistant_and_unassigned_replenisher_cannot_refuse(self):
        request, _ = self.prepare()
        other = User.objects.create_user(
            "refus-autre", FICTIVE_PASSWORD, role="REPLENISHER", must_change_password=False
        )
        for client in [self.admin_client, self.user_client, self.logged(other)]:
            self.assertEqual(self.refuse(request, client=client).status_code, 403)
        self.assertFalse(RequestEvent.objects.filter(kind="REFUSED").exists())

    def test_double_click_and_lost_response_replay_exactly_one_release(self):
        request, products = self.prepare()
        payload = {"expected_version": request.version, "message": ""}
        identifier, key = self.intention("requests.refuse", payload, resource=request.pk)
        outcomes = self.race([(self.context, payload, identifier, key)] * 20)
        self.assertEqual([outcome.status for outcome in outcomes], [200] * 20)
        self.assertEqual(len({outcome.body["operation_id"] for outcome in outcomes}), 1)
        state = self.operator.get(f"/api/v1/operation-intents/{identifier}").json()
        self.assertEqual(state["state"], "CONFIRMED")
        self.assertEqual(RequestEvent.objects.filter(kind="REFUSED").count(), 1)
        self.assertEqual(StockEntry.objects.filter(kind="RELEASE").count(), 1)
        self.assertEqual(self.stock(products[0])[:3], (10, 0, 10))
        self.assertEqual(self.refuse(request).status_code, 409)
        self.assert_request_consistent(request)

    def test_refusal_racing_delivery_or_cancellation_has_only_one_winner(self):
        # Chaque sous-cas utilise son propre produit et sa demande pour éviter les collisions de fixture.
        for index, competitor in enumerate(["requests.deliver", "requests.cancel"]):
            product = self.demandable_product(code=f"RACE-REFUS-{index}")
            created = self.send([product], [3])
            from apps.requests.models import InternalRequest

            request = InternalRequest.objects.get(pk=created.json()["resource"]["id"])
            self.claim(request)
            request.refresh_from_db()
            other = (
                self.context
                if competitor == "requests.deliver"
                else ActorContext(self.assistant.pk, self.assistant.auth_version)
            )
            refusal_payload = {"expected_version": request.version, "message": ""}
            other_payload = (
                self.delivery_payload(request)
                if competitor == "requests.deliver"
                else {"expected_version": request.version}
            )
            a = self.intention("requests.refuse", refusal_payload, resource=request.pk)
            b = self.intention(competitor, other_payload, resource=request.pk, context=other)
            results = self.race([(self.context, refusal_payload, *a), (other, other_payload, *b)])
            self.assertEqual(sum(result.status in {200, 201} for result in results), 1)
            self.assertEqual(sum(result.status == 409 for result in results), 1)
            request.refresh_from_db()
            self.assertEqual(
                self.stock(product)[:3], (7, 0, 7) if request.status == "DELIVERED" else (10, 0, 10)
            )
            self.assert_request_consistent(request)

    def test_invalid_messages_and_changed_intent_content_do_not_mutate_request(self):
        request, products = self.prepare()
        for message in [None, True, "a" * 2001, "interdit\x00"]:
            self.assertEqual(self.refuse(request, message).status_code, 400)
        identifier, key = uuid.uuid4(), uuid.uuid4().hex
        first = self.refuse(request, "Texte", identifier=identifier, key=key)
        self.assertEqual(first.status_code, 200, first.content)
        self.assertEqual(
            self.refuse(request, "Autre texte", identifier=identifier, key=key).status_code, 409
        )
        self.assertEqual(self.stock(products[0])[:3], (10, 0, 10))

    def test_interrupted_batch_and_outbox_failure_rollback_all_releases(self):
        request, products = self.prepare((4, 3))
        before = AuditEvent.objects.count()
        original = refusal.append_change

        def fail(*args, **kwargs):
            if kwargs["sequence"] == 2:
                raise RuntimeError("Interruption fictive")
            return original(*args, **kwargs)

        with patch.object(refusal, "append_change", side_effect=fail):
            self.assertEqual(self.refuse(request).status_code, 500)
        with patch.object(
            idempotency.Outbox.objects, "create", side_effect=RuntimeError("Panne fictive")
        ):
            self.assertEqual(self.refuse(request).status_code, 500)
        self.assertEqual(AuditEvent.objects.count(), before)
        self.assertFalse(RequestEvent.objects.filter(kind="REFUSED").exists())
        self.assertFalse(StockEntry.objects.filter(kind="RELEASE").exists())
        self.assertEqual([self.stock(p)[:3] for p in products], [(10, 4, 6), (10, 3, 7)])
        self.assertEqual(
            set(
                OperationIntent.objects.filter(command_type="requests.refuse").values_list(
                    "state", flat=True
                )
            ),
            {"READY"},
        )
        self.assert_request_consistent(request)

    def test_release_allowed_on_verification_hold_and_matches_audit_outbox(self):
        request, products = self.prepare()
        self.assertEqual(self.adjust(products, [0]).status_code, 409)
        response = self.refuse(request)
        self.assertEqual(response.status_code, 200, response.content)
        operation_id = response.json()["operation_id"]
        self.assertTrue(AuditEvent.objects.filter(operation_id=operation_id).exists())
        event = BusinessEvent.objects.get(operation_id=operation_id, event_type="request.refused")
        self.assertEqual(Outbox.objects.filter(event=event).count(), 1)
        self.assertEqual(self.stock(products[0])[:3], (10, 0, 10))
        self.assert_request_consistent(request)

    def test_stale_version_and_status_projection_without_facts_are_rejected(self):
        request, products = self.prepare()
        request.version -= 1
        response = self.refuse(request)
        self.assertEqual(response.status_code, 409, response.content)
        self.assertEqual(response.json()["error"]["code"], "STALE_VERSION")
        with self.assertRaises(DatabaseError), transaction.atomic():
            type(request).objects.filter(pk=request.pk).update(
                status="REFUSED", closure_reason="REFUSAL"
            )
        self.assertEqual(self.stock(products[0])[:3], (10, 4, 6))
        self.assert_request_consistent(request)

    def test_refused_dossier_is_not_visible_to_another_assistant(self):
        request, _ = self.prepare()
        self.assertEqual(self.refuse(request, "Justification privée fictive").status_code, 200)
        other = User.objects.create_user(
            "demandeur — profil A-refus-autre",
            FICTIVE_PASSWORD,
            role="ASSISTANT",
            floor=self.other_floor,
            must_change_password=False,
        )
        client = self.logged(other)
        self.assertEqual(client.get(f"/api/v1/requests/{request.pk}").status_code, 404)
        self.assertEqual(client.get(f"/demandes/{request.pk}").status_code, 404)
        self.assertEqual(client.get("/api/v1/requests/history").json()["events"], [])

    def test_refusal_works_with_restricted_runtime_database_role(self):
        import os

        from django.db import connection

        from apps.core.management.commands.grant_runtime import grant_runtime

        request, products = self.prepare()
        role = os.environ["RUNTIME_DATABASE_USER"]
        grant_runtime(role)
        original = connection.settings_dict.copy()
        connection.close()
        connection.settings_dict.update(USER=role, PASSWORD=os.environ["RUNTIME_DATABASE_PASSWORD"])
        try:
            response = self.refuse(request)
            self.assertEqual(response.status_code, 200, response.content)
        finally:
            connection.close()
            connection.settings_dict.update(original)
        self.assertEqual(self.stock(products[0])[:3], (10, 0, 10))
        self.assert_request_consistent(request)
