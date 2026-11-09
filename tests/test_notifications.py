"""M9 : PostgreSQL réel, services M0–M8 réels, transport externe simulé seulement."""

import base64
import json
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from django.core.management import call_command
from django.db import DatabaseError, close_old_connections, connection, transaction
from django.test import Client, SimpleTestCase, TransactionTestCase, override_settings
from django.utils import timezone

from apps.accounts.models import User, UserSession
from apps.cases.models import Case
from apps.core.errors import BusinessError
from apps.core.models import Operation
from apps.core.policies import ActorContext
from apps.inventory.models import StockEntry
from apps.notifications import dispatcher, push, routing, worker
from apps.notifications.models import (
    Activation,
    Attempt,
    AttemptResult,
    CapturedEmail,
    Notification,
    Outbox,
    ProductAlertState,
    WorkItem,
)
from apps.purchasing.models import PurchaseOrder
from apps.requests.models import InternalRequest
from tests.test_corrections import CorrectionFixtures
from tests.test_receiving import ReceivingFixtures


def subscription_body():
    public = (
        ec.generate_private_key(ec.SECP256R1())
        .public_key()
        .public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    )

    def encode(value):
        return base64.urlsafe_b64encode(value).decode().rstrip("=")

    return {
        "endpoint": "https://web.push.apple.com/test/" + uuid.uuid4().hex,
        "keys": {"p256dh": encode(public), "auth": encode(uuid.uuid4().bytes)},
        "language": "fr",
    }


class NotificationFixtures(ReceivingFixtures, CorrectionFixtures):
    correction_body = CorrectionFixtures.correction_body

    def setUp(self):
        super().setUp()
        dispatcher.initialize()

    def process(self):
        for _ in range(10):
            if not worker.drain(limit=500):
                break
        failures = list(
            WorkItem.objects.filter(state__in=["RETRY", "DEAD"]).values("kind", "result_code")
        )
        self.assertEqual(failures, [])

    def notification(self, user, event_type):
        return Notification.objects.filter(recipient=user, event__event_type=event_type)

    def register_push(self):
        meta = UserSession.objects.filter(user=self.replenisher, revoked_at__isnull=True).latest(
            "created_at"
        )
        context = ActorContext(self.replenisher.pk, self.replenisher.auth_version, meta.pk)
        with override_settings(
            NOTIFICATION_PUSH_ENABLED=True,
            WEBPUSH_PUBLIC_KEY="test-key",
            WEBPUSH_PRIVATE_KEY_FILE="not-opened.pem",
            WEBPUSH_CONTACT="mailto:test@example.invalid",
        ):
            sub = push.subscribe(context, subscription_body())
        return sub, context

    def push_job(self):
        sub, context = self.register_push()
        request, products = self.requested()
        # Projeter uniquement les événements, garder l'envoi pour le test ciblé.
        dispatcher.enqueue_events(500)
        for job in WorkItem.objects.filter(kind="EVENT").order_by("pk"):
            with transaction.atomic():
                dispatcher.process(job)
        return WorkItem.objects.get(kind="PUSH"), sub, context, request, products

    def acquire(self, job):
        # Claim réel du seul job choisi sans modifier son identité.
        WorkItem.objects.filter(kind="EVENT").update(due_at=timezone.now() + timedelta(days=1))
        return worker.claim()


class NotificationTests(NotificationFixtures, TransactionTestCase):
    def test_new_request_targets_replenishers_not_responsible_email(self):
        request, _ = self.requested()
        self.process()
        n = self.notification(self.replenisher, "request.created").get()
        self.assertEqual(n.target_id, request.pk)
        self.assertEqual(routing.destination(n, self.replenisher)[0], f"/demandes/{request.pk}")
        self.assertFalse(self.notification(self.admin, "request.created").exists())
        self.assertFalse(CapturedEmail.objects.exists())

    def test_cancel_unassigned_has_honest_queue_fallback(self):
        req, products = self.requested()
        self.cancel_request(req)
        self.process()
        n = self.notification(self.replenisher, "request.cancelled").get()
        self.assertEqual(
            routing.destination(n, self.replenisher), ("/demandes", "Voir les demandes à préparer")
        )
        self.assertEqual(self.stock(products[0])[:3], (10, 0, 10))

    def test_claim_delivery_and_refusal_use_real_owner(self):
        req, _ = self.prepare()
        self.deliver(req)
        self.process()
        self.assertEqual(self.notification(self.assistant, "request.claimed").count(), 1)
        self.assertEqual(self.notification(self.assistant, "request.delivered").count(), 1)
        self.assertEqual(self.notification(self.replenisher, "request.claimed").count(), 1)

    def test_correction_targets_owner_and_operator_not_other_requester(self):
        req, _, delivery, case = self.completed()
        self.correct(delivery, case, [8])
        self.process()
        self.assertEqual(self.notification(self.assistant, "request.corrected").count(), 1)
        self.assertEqual(self.notification(self.replenisher, "request.corrected").count(), 1)
        self.assertEqual(self.notification(self.admin, "delivery.error.reported").count(), 1)
        self.assertEqual(
            routing.destination(
                self.notification(self.admin, "delivery.error.reported").get(), self.admin
            )[0],
            f"/dossiers/{case.pk}",
        )
        req.refresh_from_db()
        self.assertEqual(req.status, "PARTIAL_CLOSED")

    def test_reconciliation_informs_owner_and_reporter(self):
        req, products, case = self.conflict()
        self.reconcile(self.reconciliation_body(req, products[0], case))
        self.process()
        self.assertTrue(self.notification(self.assistant, "request.reconciled").exists())
        self.assertTrue(self.notification(self.replenisher, "case.updated").exists())
        self.assertTrue(self.notification(self.admin, "stock.conflict.opened").exists())

    def test_stock_case_relation_does_not_assume_equal_identifiers(self):
        _, _, case = self.conflict()
        # Le schéma autorise des identifiants différents ; seules les relations font foi.
        from apps.notifications.routing import project

        event = Outbox.objects.get(event__event_type="stock.conflict.opened").event
        with patch.object(Case.objects, "filter", wraps=Case.objects.filter) as query:
            project(event)
        query.assert_called_with(stock_case_id=event.aggregate_id)
        self.assertEqual(case.stock_case_id, event.aggregate_id)

    def test_supplier_exceptions_and_partial_receipt_link_real_cases(self):
        p = self.demandable_product()
        order = self.buy([p], [20])
        self.receive(order, [23], [2])
        self.process()
        cases = Case.objects.filter(kind__in=["SURPLUS", "NONCONFORMITY"])
        self.assertEqual(cases.count(), 2)
        for case in cases:
            n = Notification.objects.get(
                recipient=self.admin, target_kind="CASE", target_id=case.pk
            )
            self.assertEqual(
                routing.destination(n, self.admin)[0], f"/receptions/dossiers/{case.pk}"
            )
        self.assertEqual(CapturedEmail.objects.count(), 2)

    def test_unknown_order_has_notification_no_purchase_email(self):
        report = self.unknown(message="Colis fictif sans commande")
        self.process()
        self.assertTrue(
            Notification.objects.filter(recipient=self.admin, target_id=report.case_id).exists()
        )
        self.assertFalse(CapturedEmail.objects.exists())
        self.assertFalse(PurchaseOrder.objects.exists())

    def test_partial_receipt_and_manual_reminder_no_additional_email(self):
        p = self.demandable_product()
        order = self.buy([p], [20])
        self.receive(order, [12])
        self.post_json(
            f"/api/v1/orders/{order.pk}/reminders", {"expected_last_reminder": None}, self.operator
        )
        self.process()
        self.assertTrue(self.notification(self.admin, "purchase.received").exists())
        self.assertTrue(self.notification(self.admin, "purchase.manual_reminder").exists())
        self.assertEqual(CapturedEmail.objects.count(), 2)

    def test_read_does_not_resolve_case_or_change_stock(self):
        _, products, case = self.conflict()
        self.process()
        n = Notification.objects.get(recipient=self.admin, target_id=case.pk)
        before = self.stock(products[0])
        self.assertEqual(self.admin_client.post(f"/notifications/{n.pk}/lire").status_code, 302)
        n.refresh_from_db()
        case.refresh_from_db()
        self.assertIsNotNone(n.read_at)
        self.assertEqual(case.status, "OPEN")
        self.assertEqual(self.stock(products[0]), before)

    def test_read_is_idempotent_and_other_recipient_forbidden(self):
        self.requested()
        self.process()
        n = self.notification(self.replenisher, "request.created").get()
        self.assertEqual(self.user_client.post(f"/notifications/{n.pk}/lire").status_code, 404)
        self.operator.post(f"/notifications/{n.pk}/lire")
        n.refresh_from_db()
        stamp = n.read_at
        self.operator.post(f"/notifications/{n.pk}/lire")
        n.refresh_from_db()
        self.assertEqual(n.read_at, stamp)

    def test_read_all_cutoff_keeps_new_notifications_unread(self):
        req, _ = self.requested()
        self.process()
        cutoff = timezone.now().isoformat()
        self.claim(req)
        self.process()
        self.operator.post("/notifications/lire-tout", {"cutoff": cutoff})
        self.assertFalse(
            self.notification(self.replenisher, "request.created")
            .filter(read_at__isnull=True)
            .exists()
        )
        self.assertTrue(
            self.notification(self.replenisher, "request.claimed")
            .filter(read_at__isnull=True)
            .exists()
        )

    def test_notification_counts_do_not_change_pending_request_count(self):
        self.requested()
        self.process()
        before = self.operator.get("/api/v1/requests/pending-count").content
        self.operator.post("/notifications/lire-tout", {"cutoff": timezone.now().isoformat()})
        self.assertEqual(
            self.operator.get("/api/v1/notifications/unread-count").json()["unread"], 0
        )
        self.assertEqual(self.operator.get("/api/v1/requests/pending-count").content, before)

    def test_changed_role_hides_old_role_notifications(self):
        self.requested()
        self.process()
        self.assertTrue(routing.visible(self.replenisher).exists())
        self.replenisher.role = "ASSISTANT"
        self.assertFalse(routing.visible(self.replenisher).exists())

    def test_doctor_has_requester_notifications_same_permissions(self):
        doctor = User.objects.create_user(
            "medecin-notifications",
            display_name="Demandeur B fictif",
            role="DOCTOR",
            floor=self.floor,
            must_change_password=False,
        )
        client = self.logged(doctor)
        p = self.demandable_product()
        self.send([p], [2], client=client)
        req = InternalRequest.objects.get()
        self.claim(req)
        self.process()
        n = self.notification(doctor, "request.claimed").get()
        self.assertEqual(n.scope, "REQUESTER")
        self.assertEqual(client.get(f"/notifications/{n.pk}/ouvrir").status_code, 302)
        self.assertEqual(self.user_client.get(f"/notifications/{n.pk}/ouvrir").status_code, 404)

    def test_resolved_case_link_kept_without_reopening(self):
        _, _, delivery, case = self.completed()
        self.correct(delivery, case, [8])
        self.process()
        n = Notification.objects.get(recipient=self.admin, target_id=case.pk)
        response = self.admin_client.get(f"/notifications/{n.pk}/ouvrir")
        self.assertEqual(response.url, f"/dossiers/{case.pk}")
        case.refresh_from_db()
        self.assertEqual(case.status, "RESOLVED")

    def test_email_snapshot_optional_fields_omitted_and_two_local_captures(self):
        p = self.demandable_product()
        order = self.buy([p], [7])
        self.process()
        emails = list(CapturedEmail.objects.all())
        self.assertEqual(len(emails), 2)
        for mail in emails:
            self.assertIn(order.reference, mail.body)
            self.assertIn("7 carton", mail.body)
            self.assertNotIn("Montant :", mail.body)
            self.assertNotIn("Numéro de commande chez", mail.body)
            self.assertTrue(mail.recipient.endswith(".invalid"))
        self.assertEqual(self.stock(p)[:2], (10, 0))

    def test_email_amount_reference_exact_snapshot(self):
        p = self.demandable_product()
        order = self.buy([p], [3], external_reference="FOURN-ABC", amount="45.20", currency="EUR")
        self.process()
        mail = CapturedEmail.objects.first()
        self.assertIn("FOURN-ABC", mail.body)
        self.assertIn("45.20 EUR", mail.body)
        self.assertIn(order.supplier_name, mail.body)

    @override_settings(NOTIFICATION_EMAIL_RECIPIENTS={"RESPONSIBLE": "", "ACCOUNTANT": ""})
    def test_missing_recipient_skipped_not_successfully_sent(self):
        p = self.demandable_product()
        self.buy([p], [2])
        self.process()
        self.assertFalse(CapturedEmail.objects.exists())
        self.assertEqual(
            WorkItem.objects.filter(
                kind="EMAIL", state="SKIPPED", result_code="RECIPIENT_NOT_CONFIGURED"
            ).count(),
            2,
        )

    def test_recommendation_no_email_and_single_product_alert(self):
        p = self.demandable_product(physical=2)
        self.process()
        self.assertEqual(
            Notification.objects.filter(target_kind="PRODUCT", target_id=p.pk).count(), 1
        )
        self.rec_action(p, "defer")
        self.process()
        self.rec_action(p, "reactivate")
        self.process()
        self.assertFalse(CapturedEmail.objects.exists())

    def test_shortage_alert_visible_even_if_deferred(self):
        p = self.demandable_product(physical=2)
        self.process()
        self.rec_action(p, "defer")
        self.adjust([p], [0])
        self.process()
        self.assertTrue(
            Notification.objects.filter(target_id=p.pk, title="Produit indisponible").exists()
        )

    def test_initialization_excludes_historical_events(self):
        self.requested()
        # Nouveau démarrage sur base déjà remplie : simulation par flush M9 préalable
        # dans une transaction de test autorisée ; pas d'ancienne commande envoyée.
        original = Activation.objects.get().started_at
        self.assertEqual(dispatcher.initialize().started_at, original)
        self.assertFalse(WorkItem.objects.exists())
        with patch("apps.notifications.dispatcher.Activation.objects.filter") as activation:
            activation.return_value.first.return_value = type(
                "Cutoff", (), {"started_at": timezone.now()}
            )()
            dispatcher.enqueue_events()
        self.assertFalse(WorkItem.objects.exists())

    def test_worker_repeated_runs_do_not_duplicate_notifications_or_emails(self):
        p = self.demandable_product()
        self.buy([p], [2])
        self.send([p], [1])
        self.process()
        counts = (
            Notification.objects.count(),
            CapturedEmail.objects.count(),
            StockEntry.objects.count(),
            Operation.objects.count(),
        )
        self.process()
        self.process()
        self.assertEqual(
            counts,
            (
                Notification.objects.count(),
                CapturedEmail.objects.count(),
                StockEntry.objects.count(),
                Operation.objects.count(),
            ),
        )

    def test_event_projection_rollback_is_atomic(self):
        self.requested()
        dispatcher.enqueue_events(500)
        job = WorkItem.objects.get(outbox__event__event_type="request.created")
        with patch(
            "apps.notifications.dispatcher.notify", side_effect=RuntimeError("private value")
        ):
            with transaction.atomic():
                attempt = Attempt.objects.create(work=job, number=1, token=uuid.uuid4())
                job.state = "CLAIMED"
                job.attempts = 1
                job.lease_token = attempt.token
                job.lease_until = timezone.now() + timedelta(seconds=60)
                job.save()
            worker.perform(attempt)
        self.assertFalse(self.notification(self.replenisher, "request.created").exists())
        job.refresh_from_db()
        self.assertEqual(job.state, "RETRY")
        self.assertEqual(job.result_code, "TECHNICAL_FAILURE")

    def test_lease_expiry_records_unknown_and_fences_old_worker(self):
        self.requested()
        dispatcher.enqueue_events()
        old = worker.claim()
        WorkItem.objects.filter(pk=old.work_id).update(
            lease_until=timezone.now() - timedelta(seconds=1)
        )
        new = worker.claim()
        self.assertEqual(new.work_id, old.work_id)
        self.assertFalse(worker.perform(old))
        self.assertEqual(AttemptResult.objects.get(attempt=old).code, "LEASE_EXPIRED")
        self.assertTrue(worker.perform(new))

    def test_retry_exhaustion_and_permanent_failures(self):
        self.requested()
        dispatcher.enqueue_events()
        attempt = worker.claim()
        with patch(
            "apps.notifications.dispatcher.process",
            side_effect=worker.DeliveryFailure("PERMANENT", permanent=True),
        ):
            worker.perform(attempt)
        self.assertEqual(WorkItem.objects.get(pk=attempt.work_id).state, "DEAD")

    def test_technical_retry_then_success(self):
        self.requested()
        dispatcher.enqueue_events()
        attempt = worker.claim()
        with patch(
            "apps.notifications.dispatcher.process",
            side_effect=worker.DeliveryFailure("TEMPORARY", delay=1),
        ):
            worker.perform(attempt)
        WorkItem.objects.filter(pk=attempt.work_id).update(
            due_at=timezone.now() - timedelta(seconds=1)
        )
        self.process()
        self.assertEqual(WorkItem.objects.get(pk=attempt.work_id).state, "DONE")

    def test_database_guards_for_notification_facts(self):
        self.requested()
        self.process()
        n = self.notification(self.replenisher, "request.created").get()
        with self.assertRaises(DatabaseError), transaction.atomic():
            Notification.objects.filter(pk=n.pk).update(title="Réécrit")
        mail_count = CapturedEmail.objects.count()
        self.assertEqual(mail_count, 0)
        with self.assertRaises(DatabaseError), transaction.atomic():
            Attempt.objects.all().update(number=99)

    def test_anonymous_and_other_role_diagnostics_denied(self):
        self.assertEqual(Client().get("/notifications").status_code, 302)
        self.assertEqual(self.user_client.get("/notifications/diagnostic").status_code, 403)
        self.assertEqual(self.operator.get("/notifications/diagnostic").status_code, 403)
        self.assertEqual(self.admin_client.get("/notifications/diagnostic").status_code, 200)

    def test_worker_rejects_external_flag_without_configuration(self):
        from django.core.management.base import CommandError

        with self.assertRaises(CommandError):
            call_command("run_outbox", once=True, allow_external=True)


class PushTests(NotificationFixtures, TransactionTestCase):
    def test_default_disabled_no_permission_or_external_request(self):
        self.assertFalse(push.enabled())
        self.assertContains(self.operator.get("/notifications"), "Web Push est désactivé")
        with patch("apps.notifications.senders.push_transport") as transport:
            self.requested()
            self.process()
        transport.assert_not_called()

    def test_inbox_explains_when_device_subscription_is_saved(self):
        with override_settings(
            NOTIFICATION_PUSH_ENABLED=True,
            WEBPUSH_PUBLIC_KEY="test-key",
            WEBPUSH_PRIVATE_KEY_FILE="not-opened.pem",
            WEBPUSH_CONTACT="mailto:test@example.invalid",
        ):
            inactive = self.operator.get("/notifications")
            self.assertContains(inactive, "Activer les notifications")
            self.assertNotContains(inactive, "Notifications activées sur cet appareil")

            sub, _ = self.register_push()
            active = self.operator.get("/notifications")
            self.assertContains(active, "Notifications activées sur cet appareil")
            self.assertContains(active, "Votre inscription est enregistrée")
            self.assertContains(
                active, "<strong>Notifications activées sur cet appareil</strong>", html=True
            )
            self.assertContains(active, "Désactiver cet abonnement")
            self.assertNotContains(active, 'id="enable-push"')
            self.assertNotContains(active, sub.endpoint)

    def test_subscription_valid_idempotent_owned_and_revocable(self):
        sub, context = self.register_push()
        self.assertEqual(sub.user_id, self.replenisher.pk)
        with override_settings(
            NOTIFICATION_PUSH_ENABLED=True,
            WEBPUSH_PUBLIC_KEY="key",
            WEBPUSH_PRIVATE_KEY_FILE="private",
            WEBPUSH_CONTACT="mailto:test@example.invalid",
        ):
            again = push.subscribe(
                context,
                {
                    "endpoint": sub.endpoint,
                    "keys": {"p256dh": sub.p256dh, "auth": sub.auth},
                    "language": "ar",
                },
            )
        self.assertEqual(again.pk, sub.pk)
        self.assertEqual(again.language, "fr")
        self.assertEqual(push.revoke(context, sub.pk), 1)
        self.assertEqual(push.revoke(context, sub.pk), 0)
        with override_settings(
            NOTIFICATION_PUSH_ENABLED=True,
            WEBPUSH_PUBLIC_KEY="key",
            WEBPUSH_PRIVATE_KEY_FILE="private",
            WEBPUSH_CONTACT="mailto:test@example.invalid",
        ):
            renewed = push.subscribe(
                context,
                {
                    "endpoint": sub.endpoint,
                    "keys": {"p256dh": sub.p256dh, "auth": sub.auth},
                    "language": "ar",
                },
            )
        self.assertNotEqual(renewed.pk, sub.pk)
        self.assertEqual(renewed.language, "ar")

    def test_subscription_rejects_unsupported_notification_language(self):
        sub, context = self.register_push()
        with override_settings(
            NOTIFICATION_PUSH_ENABLED=True,
            WEBPUSH_PUBLIC_KEY="key",
            WEBPUSH_PRIVATE_KEY_FILE="private",
            WEBPUSH_CONTACT="mailto:test@example.invalid",
        ):
            with self.assertRaises(BusinessError) as caught:
                push.subscribe(
                    context,
                    {
                        "endpoint": sub.endpoint,
                        "keys": {"p256dh": sub.p256dh, "auth": sub.auth},
                        "language": "es",
                    },
                )
        self.assertEqual(caught.exception.code, "INVALID_SUBSCRIPTION")

    def test_push_payload_is_generic_and_localized_to_subscription(self):
        from types import SimpleNamespace

        from apps.notifications.senders import localized_push_payload

        notification = SimpleNamespace(pk="00000000-0000-0000-0000-000000000001")
        english = localized_push_payload(notification, "en")
        arabic = localized_push_payload(notification, "ar")
        self.assertEqual(english["title"], "Central stock")
        self.assertEqual(arabic["title"], "المخزون المركزي")
        self.assertEqual(english["body"], "You have a new update in the app.")
        self.assertEqual(arabic["body"], "لديك تحديث جديد في التطبيق.")
        self.assertEqual(english["notification_id"], arabic["notification_id"])
        self.assertEqual(set(english), {"title", "body", "notification_id"})

    def test_a10_test_mode_allows_only_one_active_device_subscription(self):
        sub, context = self.register_push()
        with override_settings(
            PUSH_SINGLE_TARGET_TEST_MODE=True,
            NOTIFICATION_PUSH_ENABLED=True,
            WEBPUSH_PUBLIC_KEY="key",
            WEBPUSH_PRIVATE_KEY_FILE="private",
            WEBPUSH_CONTACT="mailto:test@example.invalid",
        ):
            again = push.subscribe(
                context,
                {
                    "endpoint": sub.endpoint,
                    "keys": {"p256dh": sub.p256dh, "auth": sub.auth},
                    "language": sub.language,
                },
            )
            self.assertEqual(again.pk, sub.pk)
            with self.assertRaises(BusinessError) as caught:
                push.subscribe(context, subscription_body())
        self.assertEqual(caught.exception.code, "PUSH_TEST_SUBSCRIPTION_LIMIT")

    def test_subscription_does_not_leak_keys_and_other_roles_denied(self):
        sub, _ = self.register_push()
        response = self.operator.get("/api/v1/push-subscriptions")
        self.assertNotIn(sub.endpoint, response.content.decode())
        self.assertNotIn(sub.auth, response.content.decode())
        self.assertEqual(self.user_client.get("/api/v1/push-subscriptions").status_code, 403)

    def test_push_subscription_api_uses_the_configured_public_key(self):
        configured_key = "test-public-key-for-current-settings"
        with override_settings(
            NOTIFICATION_PUSH_ENABLED=True,
            WEBPUSH_PUBLIC_KEY=configured_key,
            WEBPUSH_PRIVATE_KEY_FILE="not-opened.pem",
            WEBPUSH_CONTACT="mailto:test@example.invalid",
        ):
            response = self.operator.get("/api/v1/push-subscriptions")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["public_key"], configured_key)

    def test_ssrf_endpoints_and_invalid_crypto_rejected(self):
        for endpoint in [
            "https://127.0.0.1/x",
            "http://web.push.apple.com/x",
            "https://web.push.apple.com.evil.invalid/x",
            "https://user@web.push.apple.com/x",
            "https://web.push.apple.com:444/x",
        ]:
            with self.subTest(endpoint=endpoint), self.assertRaises(BusinessError):
                push.valid_endpoint(endpoint)
        with self.assertRaises(BusinessError):
            push.decode_key("invalid", 65)

    def test_a10_accepts_only_apple_push_endpoints(self):
        with override_settings(PUSH_SINGLE_TARGET_TEST_MODE=True):
            self.assertEqual(
                push.valid_endpoint("https://web.push.apple.com/test/token"),
                "https://web.push.apple.com/test/token",
            )
            with self.assertRaises(BusinessError):
                push.valid_endpoint("https://fcm.googleapis.com/fcm/send/test")

    def test_no_push_without_explicit_external_flag(self):
        job, _, _, _, _ = self.push_job()
        attempt = self.acquire(job)
        with patch("apps.notifications.senders.push_transport") as transport:
            worker.perform(attempt)
        transport.assert_not_called()
        job.refresh_from_db()
        self.assertEqual(job.result_code, "EXTERNAL_DISABLED")

    def test_disabled_account_or_revoked_session_excluded(self):
        job, sub, _, _, _ = self.push_job()
        UserSession.objects.filter(pk=sub.session_id).update(revoked_at=timezone.now())
        attempt = self.acquire(job)
        with patch("apps.notifications.senders.push_transport") as transport:
            worker.perform(attempt, allow_external=True)
        transport.assert_not_called()
        sub.refresh_from_db()
        self.assertIsNotNone(sub.revoked_at)

    @override_settings(
        NOTIFICATION_EXTERNAL_ALLOWED=True,
        NOTIFICATION_PUSH_ENABLED=True,
        WEBPUSH_PUBLIC_KEY="key",
        WEBPUSH_PRIVATE_KEY_FILE="private",
        WEBPUSH_CONTACT="mailto:test@example.invalid",
    )
    def test_adapter_success_and_gone_subscription(self):
        job, sub, _, _, _ = self.push_job()
        attempt = self.acquire(job)
        with patch(
            "apps.notifications.senders.push_transport", return_value="SUBSCRIPTION_GONE"
        ) as transport:
            worker.perform(attempt, allow_external=True)
        transport.assert_called_once()
        sub.refresh_from_db()
        self.assertIsNotNone(sub.revoked_at)

    def test_a10_push_without_allowlist_is_blocked(self):
        job, sub, _, _, _ = self.push_job()
        attempt = self.acquire(job)
        with (
            override_settings(
                NOTIFICATION_EXTERNAL_ALLOWED=True,
                NOTIFICATION_PUSH_ENABLED=True,
                WEBPUSH_PUBLIC_KEY="key",
                WEBPUSH_PRIVATE_KEY_FILE="private",
                WEBPUSH_CONTACT="mailto:test@example.invalid",
                PUSH_SINGLE_TARGET_TEST_MODE=True,
            ),
            patch("apps.notifications.senders.push_transport") as transport,
        ):
            worker.perform(attempt, allow_external=True)
        transport.assert_not_called()
        job.refresh_from_db()
        self.assertEqual(job.state, "SKIPPED")
        self.assertEqual(job.result_code, "PUSH_TEST_TARGET_BLOCKED")

    def test_a10_push_accepts_only_exact_allowlisted_subscription_and_user(self):
        job, sub, _, _, _ = self.push_job()
        attempt = self.acquire(job)
        with (
            override_settings(
                NOTIFICATION_EXTERNAL_ALLOWED=True,
                NOTIFICATION_PUSH_ENABLED=True,
                WEBPUSH_PUBLIC_KEY="key",
                WEBPUSH_PRIVATE_KEY_FILE="private",
                WEBPUSH_CONTACT="mailto:test@example.invalid",
                PUSH_SINGLE_TARGET_TEST_MODE=True,
                PUSH_TEST_ALLOWED_SUBSCRIPTION_ID=str(sub.pk),
                PUSH_TEST_ALLOWED_USER_ID=str(sub.user_id),
            ),
            patch("apps.notifications.senders.push_transport", return_value=None) as transport,
        ):
            worker.perform(attempt, allow_external=True)
        transport.assert_called_once()
        job.refresh_from_db()
        self.assertEqual(job.state, "DONE")
        self.assertEqual(job.result_code, "SENT")

    def test_a10_push_rejects_subscription_id_or_user_mismatch(self):
        job, _, _, _, _ = self.push_job()
        attempt = self.acquire(job)
        with (
            override_settings(
                NOTIFICATION_EXTERNAL_ALLOWED=True,
                NOTIFICATION_PUSH_ENABLED=True,
                WEBPUSH_PUBLIC_KEY="key",
                WEBPUSH_PRIVATE_KEY_FILE="private",
                WEBPUSH_CONTACT="mailto:test@example.invalid",
                PUSH_SINGLE_TARGET_TEST_MODE=True,
                PUSH_TEST_ALLOWED_SUBSCRIPTION_ID="not-this-subscription",
                PUSH_TEST_ALLOWED_USER_ID="not-this-user",
            ),
            patch("apps.notifications.senders.push_transport") as transport,
        ):
            worker.perform(attempt, allow_external=True)
        transport.assert_not_called()
        job.refresh_from_db()
        self.assertEqual(job.result_code, "PUSH_TEST_TARGET_BLOCKED")

    def test_a10_push_blocks_if_any_second_subscription_is_active(self):
        job, sub, context, _, _ = self.push_job()
        with override_settings(
            NOTIFICATION_PUSH_ENABLED=True,
            WEBPUSH_PUBLIC_KEY="key",
            WEBPUSH_PRIVATE_KEY_FILE="private",
            WEBPUSH_CONTACT="mailto:test@example.invalid",
        ):
            push.subscribe(context, subscription_body())
        attempt = self.acquire(job)
        with (
            override_settings(
                NOTIFICATION_EXTERNAL_ALLOWED=True,
                NOTIFICATION_PUSH_ENABLED=True,
                WEBPUSH_PUBLIC_KEY="key",
                WEBPUSH_PRIVATE_KEY_FILE="private",
                WEBPUSH_CONTACT="mailto:test@example.invalid",
                PUSH_SINGLE_TARGET_TEST_MODE=True,
                PUSH_TEST_ALLOWED_SUBSCRIPTION_ID=str(sub.pk),
                PUSH_TEST_ALLOWED_USER_ID=str(sub.user_id),
            ),
            patch("apps.notifications.senders.push_transport") as transport,
        ):
            worker.perform(attempt, allow_external=True)
        transport.assert_not_called()
        job.refresh_from_db()
        self.assertEqual(job.result_code, "PUSH_TEST_TARGET_BLOCKED")

    def test_a10_external_worker_requires_one_targeted_push_job(self):
        from django.core.management.base import CommandError

        job, sub, _, _, _ = self.push_job()
        event = WorkItem.objects.filter(kind="EVENT").first()
        self.assertIsNotNone(event)
        with (
            override_settings(
                NOTIFICATION_EXTERNAL_ALLOWED=True,
                NOTIFICATION_PUSH_ENABLED=True,
                WEBPUSH_PUBLIC_KEY="key",
                WEBPUSH_PRIVATE_KEY_FILE="private",
                WEBPUSH_CONTACT="mailto:test@example.invalid",
                PUSH_SINGLE_TARGET_TEST_MODE=True,
                PUSH_TEST_ALLOWED_SUBSCRIPTION_ID=str(sub.pk),
                PUSH_TEST_ALLOWED_USER_ID=str(sub.user_id),
            ),
            patch("apps.notifications.senders.push_transport") as transport,
        ):
            with self.assertRaises(CommandError):
                call_command("run_outbox", once=True, allow_external=True)
            with self.assertRaises(CommandError):
                call_command(
                    "run_outbox",
                    once=True,
                    allow_external=True,
                    work_item=str(event.pk),
                )
        transport.assert_not_called()
        job.refresh_from_db()
        self.assertEqual(job.state, "PENDING")

    def test_a10_targeted_event_projection_keeps_push_pending_without_transport(self):
        from django.core.management.base import CommandError

        self.register_push()
        self.requested()
        dispatcher.enqueue_events(500)
        event_job = WorkItem.objects.filter(
            kind="EVENT", outbox__event__event_type="request.created"
        ).first()
        self.assertIsNotNone(event_job)
        with (
            override_settings(
                PUSH_SINGLE_TARGET_TEST_MODE=True,
                NOTIFICATION_EXTERNAL_ALLOWED=False,
                NOTIFICATION_PUSH_ENABLED=True,
                WEBPUSH_PUBLIC_KEY="key",
                WEBPUSH_PRIVATE_KEY_FILE="private",
                WEBPUSH_CONTACT="mailto:test@example.invalid",
            ),
            patch("apps.notifications.senders.push_transport") as transport,
        ):
            call_command("run_outbox", once=True, work_item=str(event_job.pk), project_only=True)
            with self.assertRaises(CommandError):
                call_command(
                    "run_outbox",
                    once=True,
                    allow_external=True,
                    work_item=str(event_job.pk),
                    project_only=True,
                )
        transport.assert_not_called()
        push_job = WorkItem.objects.get(kind="PUSH")
        self.assertEqual(push_job.state, "PENDING")

    @override_settings(
        NOTIFICATION_EXTERNAL_ALLOWED=True,
        NOTIFICATION_PUSH_ENABLED=True,
        WEBPUSH_PUBLIC_KEY="key",
        WEBPUSH_PRIVATE_KEY_FILE="private",
        WEBPUSH_CONTACT="mailto:test@example.invalid",
    )
    def test_ack_lost_can_repeat_external_push_not_business(self):
        job, _, _, _, products = self.push_job()
        attempt = self.acquire(job)
        before = (Operation.objects.count(), StockEntry.objects.count(), self.stock(products[0]))
        accepted = []

        def lost(*args):
            accepted.append(True)
            raise worker.DeliveryFailure("ACK_UNKNOWN", delay=1)

        with patch("apps.notifications.senders.push_transport", side_effect=lost):
            worker.perform(attempt, allow_external=True)
        job.refresh_from_db()
        self.assertEqual(job.state, "RETRY")
        WorkItem.objects.filter(pk=job.pk).update(due_at=timezone.now() - timedelta(seconds=1))
        next_attempt = worker.claim()
        with patch(
            "apps.notifications.senders.push_transport",
            side_effect=lambda *args: accepted.append(True),
        ):
            worker.perform(next_attempt, allow_external=True)
        self.assertEqual(len(accepted), 2)
        self.assertEqual(
            before, (Operation.objects.count(), StockEntry.objects.count(), self.stock(products[0]))
        )

    def test_worker_service_script_has_no_offline_cache_and_same_origin_targets(self):
        response = Client().get("/service-worker.js")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Service-Worker-Allowed"], "/")
        self.assertNotIn('addEventListener("fetch"', response.content.decode())
        self.assertIn("notification_id", response.content.decode())


class NotificationConcurrencyTests(NotificationFixtures, TransactionTestCase):
    def test_two_workers_do_not_double_project_or_capture(self):
        p = self.demandable_product()
        self.buy([p], [3])
        self.send([p], [2])
        dispatcher.enqueue_events(500)
        barrier = threading.Barrier(2)

        def run(_):
            close_old_connections()
            try:
                with connection.cursor() as c:
                    c.execute("SELECT pg_backend_pid()")
                    pid = c.fetchone()[0]
                barrier.wait(timeout=10)
                for _ in range(3):
                    worker.drain(limit=500)
                return pid
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            pids = list(pool.map(run, range(2)))
        self.process()
        self.assertEqual(len(set(pids)), 2)
        self.assertEqual(CapturedEmail.objects.count(), 2)
        self.assertEqual(self.notification(self.replenisher, "request.created").count(), 1)
        path = Path("docs/recette/M9/concurrence.json")
        path.write_text(
            json.dumps(
                {
                    "distinct_postgresql_connections": len(set(pids)),
                    "captures": CapturedEmail.objects.count(),
                    "request_notifications": self.notification(
                        self.replenisher, "request.created"
                    ).count(),
                },
                indent=2,
            )
            + "\n"
        )


class NotificationAdditionalTests(NotificationFixtures, TransactionTestCase):
    def test_close_and_reopen_notifications_keep_business_decisions(self):
        req, _ = self.prepare((10,))
        self.deliver(req, [4], "LATER")
        req.refresh_from_db()
        response = self.post_json(
            f"/api/v1/requests/{req.pk}/close-remainder",
            {
                "expected_version": req.version,
                "reason": "Abandon vérifié fictif.",
                "verification": True,
            },
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.process()
        self.assertTrue(self.notification(self.assistant, "request.remainder_closed").exists())
        req.refresh_from_db()
        response = self.post_json(
            f"/api/v1/requests/{req.pk}/reopen",
            {
                "expected_version": req.version,
                "quantities": [{"request_line_id": str(req.lines.get().pk), "quantity": 2}],
                "reason": "Deux unités restent nécessaires.",
                "verification": True,
            },
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.process()
        self.assertTrue(self.notification(self.assistant, "request.reopened").exists())

    def test_reassignment_notifies_previous_and_next_operator(self):
        req, _ = self.prepare()
        other = User.objects.create_user(
            "autre-operateur-fictif",
            display_name="Autre opérateur fictif",
            role="REPLENISHER",
            must_change_password=False,
        )
        response = self.post_json(
            f"/api/v1/requests/{req.pk}/reassign",
            {
                "expected_version": req.version,
                "new_operator_id": str(other.pk),
                "verification": True,
            },
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.process()
        self.assertTrue(self.notification(self.replenisher, "request.reassigned").exists())
        self.assertTrue(self.notification(other, "request.reassigned").exists())

    def test_late_cancelled_arrival_and_reinstatement_create_no_new_purchase_email(self):
        from apps.receiving.models import ArrivalReport

        p = self.demandable_product(physical=0)
        order = self.buy([p], [5])
        self.cancel_remainder(order, 5)
        order.refresh_from_db()
        line = order.lines.get()
        response = self.post_json(
            f"/api/v1/orders/{order.pk}/late-delivery-reports",
            {
                "expected_version": order.version,
                "lines": [{"order_line_id": str(line.pk), "observed_quantity": 3}],
            },
            self.operator,
        )
        self.assertEqual(response.status_code, 201, response.content)
        report = ArrivalReport.objects.get()
        self.process()
        self.assertTrue(
            Notification.objects.filter(recipient=self.admin, target_id=report.case_id).exists()
        )
        report.case.refresh_from_db()
        response = self.post_json(
            f"/api/v1/orders/{order.pk}/reinstate-remainder",
            {
                "expected_version": order.version,
                "report_id": str(report.pk),
                "expected_report_version": report.case.version,
                "external_verification": True,
                "lines": [{"order_line_id": str(line.pk), "quantity": 3}],
            },
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.process()
        self.assertEqual(CapturedEmail.objects.count(), 2)
        self.assertEqual(self.stock(p)[0], 0)

    def test_invalid_read_cutoff_is_client_error_and_csrf_is_enforced(self):
        response = self.operator.post("/notifications/lire-tout", {"cutoff": "2026-99-99T25:99:99"})
        self.assertEqual(response.status_code, 400)
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.replenisher)
        self.assertEqual(
            client.post(
                "/notifications/lire-tout", {"cutoff": timezone.now().isoformat()}
            ).status_code,
            403,
        )

    def test_capture_rollback_and_retry_never_duplicates_capture(self):
        p = self.demandable_product()
        self.buy([p], [2])
        self.process()
        before = list(CapturedEmail.objects.values_list("message_id", flat=True))
        # Le backend refuserait toute utilisation hors de la transaction du worker.
        from django.core.mail import EmailMessage

        from apps.notifications.email_backend import CaptureBackend

        with self.assertRaises(RuntimeError):
            CaptureBackend().send_messages([EmailMessage()])
        self.process()
        self.assertEqual(before, list(CapturedEmail.objects.values_list("message_id", flat=True)))

    def test_external_smtp_guard_and_transport_classification(self):
        from smtplib import SMTPResponseException

        from apps.notifications.senders import send_email

        p = self.demandable_product()
        with override_settings(NOTIFICATION_EMAIL_MODE="SMTP"):
            self.buy([p], [2])
            self.process()
        self.assertFalse(CapturedEmail.objects.exists())
        job = WorkItem.objects.filter(kind="EMAIL").first()
        with override_settings(
            NOTIFICATION_EMAIL_MODE="SMTP",
            NOTIFICATION_EXTERNAL_ALLOWED=True,
            NOTIFICATION_SMTP_HOST="smtp.example.invalid",
        ):
            with patch(
                "django.core.mail.backends.smtp.EmailBackend.send_messages",
                side_effect=SMTPResponseException(550, b"private response"),
            ):
                with self.assertRaises(worker.DeliveryFailure) as error:
                    send_email(job, allow_external=True)
        self.assertTrue(error.exception.permanent)

    def test_pywebpush_real_encryption_with_intercepted_http_no_network(self):
        import tempfile
        from pathlib import Path

        from requests import Response

        from apps.notifications.senders import push_transport

        sub, _ = self.register_push()
        self.requested()
        dispatcher.enqueue_events(500)
        for job in WorkItem.objects.filter(kind="EVENT"):
            with transaction.atomic():
                dispatcher.process(job)
        notification = self.notification(self.replenisher, "request.created").get()
        with tempfile.TemporaryDirectory() as directory:
            key = Path(directory) / "vapid.pem"
            key.write_bytes(
                ec.generate_private_key(ec.SECP256R1()).private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption(),
                )
            )
            response = Response()
            response.status_code = 201
            with override_settings(
                WEBPUSH_PRIVATE_KEY_FILE=str(key), WEBPUSH_CONTACT="mailto:test@example.invalid"
            ):
                with patch("requests.Session.request", return_value=response) as network:
                    self.assertIsNone(push_transport(sub, notification))
            args = network.call_args.kwargs
            self.assertFalse(args["allow_redirects"])
            self.assertTrue(args["verify"])
            self.assertEqual(args["timeout"], 8)
            self.assertEqual(args["headers"]["content-encoding"], "aes128gcm")
            self.assertNotIn(b"Une information", args["data"])

    def test_push_403_reason_is_allowlisted_and_response_body_is_not_persisted(self):
        from pywebpush import WebPushException
        from requests import Response

        from apps.notifications.senders import DeliveryFailure, push_transport

        sub, _ = self.register_push()
        self.requested()
        dispatcher.enqueue_events(500)
        for job in WorkItem.objects.filter(kind="EVENT"):
            with transaction.atomic():
                dispatcher.process(job)
        notification = self.notification(self.replenisher, "request.created").get()

        cases = {
            "VapidPkHashMismatch": "PUSH_HTTP_403_VAPID_KEY_MISMATCH",
            "BadJwtToken": "PUSH_HTTP_403_BAD_JWT",
            "BadAuthorizationHeader": "PUSH_HTTP_403_BAD_AUTH_HEADER",
            "BadVapidPublicKey": "PUSH_HTTP_403_BAD_VAPID_KEY",
            "NotAnAllowlistedReason": "PUSH_HTTP_403_REASON_UNRECOGNIZED",
        }
        for reason, expected_code in cases.items():
            response = Response()
            response.status_code = 403
            response._content = json.dumps(
                {"reason": reason, "endpoint": "private-endpoint", "jwt": "private-jwt"}
            ).encode()
            with (
                self.subTest(reason=reason),
                patch(
                    "pywebpush.webpush",
                    side_effect=WebPushException("private exception text", response=response),
                ),
            ):
                with self.assertRaises(DeliveryFailure) as caught:
                    push_transport(sub, notification)
            self.assertEqual(caught.exception.code, expected_code)
            self.assertTrue(caught.exception.permanent)
            for secret in ["private-endpoint", "private-jwt", "private exception text"]:
                self.assertNotIn(secret, caught.exception.code)

        for body, expected_code in [
            (b"not-json", "PUSH_HTTP_403_REASON_UNAVAILABLE"),
            (b'{"reason": null}', "PUSH_HTTP_403_REASON_UNAVAILABLE"),
        ]:
            response = Response()
            response.status_code = 403
            response._content = body
            with patch(
                "pywebpush.webpush", side_effect=WebPushException("redacted", response=response)
            ):
                with self.assertRaises(DeliveryFailure) as caught:
                    push_transport(sub, notification)
            self.assertEqual(caught.exception.code, expected_code)

        response = Response()
        response.status_code = 403
        response._content = b'{"reason":"BadJwtToken"}'
        with (
            override_settings(
                NOTIFICATION_EXTERNAL_ALLOWED=True,
                NOTIFICATION_PUSH_ENABLED=True,
                WEBPUSH_PUBLIC_KEY="test-key",
                WEBPUSH_PRIVATE_KEY_FILE="not-opened.pem",
                WEBPUSH_CONTACT="mailto:test@example.invalid",
            ),
            patch("pywebpush.webpush", side_effect=WebPushException("private", response=response)),
        ):
            job = WorkItem.objects.get(kind="PUSH")
            attempt = self.acquire(job)
            with patch("apps.notifications.worker.logger.info") as log_info:
                worker.perform(attempt, allow_external=True)
        job.refresh_from_db()
        self.assertEqual(job.result_code, "PUSH_HTTP_403_BAD_JWT")
        log_extra = log_info.call_args.kwargs["extra"]
        self.assertEqual(log_extra["attempt_id"], str(attempt.pk))
        self.assertEqual(log_extra["recipient_category"], "replenisher")
        self.assertEqual(log_extra["provider_http_status"], 403)
        self.assertNotIn("private", json.dumps(log_extra))
        import logging

        from apps.core.logging import JsonFormatter

        record = logging.LogRecord("reserve", logging.INFO, __file__, 1, "finished", (), None)
        for key, value in log_extra.items():
            setattr(record, key, value)
        formatted_log = JsonFormatter().format(record)
        self.assertIn(str(attempt.pk), formatted_log)
        self.assertIn('"recipient_category": "replenisher"', formatted_log)
        self.assertIn('"provider_http_status": 403', formatted_log)
        self.assertNotIn("private", formatted_log)

        response = Response()
        response.status_code = 410
        with patch(
            "pywebpush.webpush", side_effect=WebPushException("redacted", response=response)
        ):
            self.assertEqual(push_transport(sub, notification), "SUBSCRIPTION_GONE_410")

    def test_vapid_header_is_es256_signed_for_exact_apple_origin_without_network(self):
        from py_vapid.jwt import decode as decode_vapid_jwt
        from requests import Response

        from apps.notifications.senders import push_transport

        sub, _ = self.register_push()
        self.requested()
        dispatcher.enqueue_events(500)
        for job in WorkItem.objects.filter(kind="EVENT"):
            with transaction.atomic():
                dispatcher.process(job)
        notification = self.notification(self.replenisher, "request.created").get()

        private = ec.generate_private_key(ec.SECP256R1())
        with tempfile.TemporaryDirectory() as directory:
            key_file = Path(directory) / "vapid.pem"
            key_file.write_bytes(
                private.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption(),
                )
            )
            key_file.chmod(0o600)
            public = private.public_key().public_bytes(
                serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
            )
            public_b64 = base64.urlsafe_b64encode(public).decode().rstrip("=")
            captured = {}
            calls = []

            def capture_send(_sender, _data, headers, **_kwargs):
                calls.append(True)
                captured.update(headers)
                response = Response()
                response.status_code = 201
                return response

            with (
                override_settings(
                    WEBPUSH_PUBLIC_KEY=public_b64,
                    WEBPUSH_PRIVATE_KEY_FILE=str(key_file),
                    WEBPUSH_CONTACT="mailto:test@example.invalid",
                ),
                patch("pywebpush.WebPusher.send", new=capture_send),
            ):
                self.assertIsNone(push_transport(sub, notification))

        self.assertEqual(calls, [True])
        scheme, fields = captured["Authorization"].split(" ", 1)
        self.assertEqual(scheme, "vapid")
        auth = dict(part.split("=", 1) for part in fields.split(","))
        self.assertEqual(base64.urlsafe_b64decode(auth["k"] + "=" * (-len(auth["k"]) % 4)), public)
        claims = decode_vapid_jwt(auth["t"], public_b64)
        header = json.loads(
            base64.urlsafe_b64decode(
                auth["t"].split(".", 1)[0] + "=" * (-len(auth["t"].split(".", 1)[0]) % 4)
            )
        )
        self.assertEqual(header["alg"], "ES256")
        self.assertEqual(claims["aud"], "https://web.push.apple.com")
        self.assertEqual(claims["sub"], "mailto:test@example.invalid")
        self.assertGreater(claims["exp"], int(time.time()))
        self.assertLessEqual(claims["exp"] - int(time.time()), 24 * 60 * 60)

    def test_old_session_after_reset_or_deactivation_is_not_sent(self):
        job, sub, _, _, _ = self.push_job()
        self.assertEqual(self.update(self.replenisher, {"is_active": False}).status_code, 200)
        attempt = self.acquire(job)
        with patch("apps.notifications.senders.push_transport") as transport:
            worker.perform(attempt, allow_external=True)
        transport.assert_not_called()
        sub.refresh_from_db()
        self.assertIsNotNone(sub.revoked_at)


class NotificationSessionContractTests(NotificationFixtures, TransactionTestCase):
    def test_api_requires_authenticated_session_in_json(self):
        for path in ["/api/v1/notifications/unread-count", "/api/v1/push-subscriptions"]:
            response = Client().get(path)
            self.assertEqual(response.status_code, 401)
            self.assertEqual(response.json()["error"]["code"], "AUTHENTICATION_REQUIRED")

    def test_reset_excludes_old_push_subscription_without_resurrecting_access(self):
        job, sub, _, _, _ = self.push_job()
        response = self.reset(self.replenisher)
        self.assertEqual(response.status_code, 200, response.content)
        attempt = self.acquire(job)
        with patch("apps.notifications.senders.push_transport") as external:
            worker.perform(attempt, allow_external=True)
        external.assert_not_called()
        sub.refresh_from_db()
        self.assertIsNotNone(sub.revoked_at)

    def test_refusal_notifies_requester_without_inventory_alert_or_actor_spam(self):
        request, products = self.prepare()
        response = self.post_json(
            f"/api/v1/requests/{request.pk}/refuse",
            {"expected_version": request.version, "message": "Refus fictif."},
            self.operator,
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.process()
        self.assertTrue(self.notification(self.assistant, "request.refused").exists())
        self.assertFalse(self.notification(self.replenisher, "request.refused").exists())
        self.assertEqual(self.stock(products[0])[:3], (10, 0, 10))

    def test_smtp_recipient_rejection_is_permanent_without_logging_address(self):
        from smtplib import SMTPRecipientsRefused

        from apps.notifications.senders import send_email

        p = self.demandable_product()
        with override_settings(NOTIFICATION_EMAIL_MODE="SMTP"):
            self.buy([p], [2])
            self.process()
        job = WorkItem.objects.filter(kind="EMAIL").first()
        with override_settings(
            NOTIFICATION_EMAIL_MODE="SMTP",
            NOTIFICATION_EXTERNAL_ALLOWED=True,
            NOTIFICATION_SMTP_HOST="smtp.example.invalid",
        ):
            with patch(
                "django.core.mail.backends.smtp.EmailBackend.send_messages",
                side_effect=SMTPRecipientsRefused({"private@example.invalid": (550, b"private")}),
            ):
                with self.assertRaises(worker.DeliveryFailure) as error:
                    send_email(job, allow_external=True)
        self.assertTrue(error.exception.permanent)
        self.assertEqual(error.exception.code, "SMTP_RECIPIENT_REJECTED")


class AlertProjectionConcurrencyTests(NotificationFixtures, TransactionTestCase):
    def test_waiting_worker_reloads_need_after_metadata_lock(self):
        import time

        p = self.demandable_product(physical=3)
        self.adjust([p], [2])
        outbox = Outbox.objects.filter(
            event__event_type="operation.confirmed",
            event__operation__command_type="inventory.adjust",
        ).latest("created_at")
        job = WorkItem.objects.create(kind="EVENT", outbox=outbox, dedupe_key=f"event:{outbox.pk}")
        ProductAlertState.objects.create(product=p, need=False, shortage=False)
        pid_ready = threading.Event()
        worker_pid = []

        def project():
            close_old_connections()
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_backend_pid()")
                    worker_pid.append(cursor.fetchone()[0])
                pid_ready.set()
                with transaction.atomic():
                    dispatcher.alerts(job)
            finally:
                connection.close()

        def refill():
            close_old_connections()
            try:
                return self.adjust([p], [10]).status_code
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            with transaction.atomic():
                ProductAlertState.objects.select_for_update().get(pk=p.pk)
                future = pool.submit(project)
                self.assertTrue(pid_ready.wait(5))
                blocked = False
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    with connection.cursor() as cursor:
                        cursor.execute(
                            "SELECT wait_event_type FROM pg_stat_activity WHERE pid=%s", worker_pid
                        )
                        row = cursor.fetchone()
                    if row and row[0] == "Lock":
                        blocked = True
                        break
                    time.sleep(0.02)
                self.assertTrue(blocked, "Le deuxième worker doit attendre le verrou de projection")
                self.assertEqual(pool.submit(refill).result(timeout=10), 200)
            future.result(timeout=10)
        state = ProductAlertState.objects.get(product=p)
        self.assertFalse(
            state.need, "Un ancien snapshot ne doit pas remplacer l'état courant après attente"
        )
        self.assertEqual(self.stock(p)[0], 10)


class VapidConfigurationTests(SimpleTestCase):
    def test_vapid_pair_fingerprint_is_public_and_mismatch_is_rejected(self):
        from config.vapid import validate_vapid_key_pair

        private_a = ec.generate_private_key(ec.SECP256R1())
        private_b = ec.generate_private_key(ec.SECP256R1())

        def public_b64(private):
            public_bytes = private.public_key().public_bytes(
                serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
            )
            return base64.urlsafe_b64encode(public_bytes).decode().rstrip("=")

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "vapid.pem"
            path.write_bytes(
                private_a.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption(),
                )
            )
            path.chmod(0o600)
            fingerprint = validate_vapid_key_pair(public_b64(private_a), path)
            self.assertRegex(fingerprint, r"^[0-9a-f]{64}$")
            self.assertNotEqual(fingerprint, public_b64(private_a))
            with self.assertRaisesRegex(ValueError, "VAPID_KEY_PAIR_INVALID"):
                validate_vapid_key_pair(public_b64(private_b), path)
            with self.assertRaisesRegex(ValueError, "VAPID_KEY_PAIR_INVALID"):
                validate_vapid_key_pair("not-a-public-key", path)

            path.chmod(0o644)
            with self.assertRaisesRegex(ValueError, "VAPID_KEY_PAIR_INVALID"):
                validate_vapid_key_pair(public_b64(private_a), path)

    def test_pywebpush_renews_expired_jwt_claims_and_rejects_invalid_subject(self):
        from py_vapid import Vapid, VapidException
        from py_vapid.jwt import decode as decode_vapid_jwt
        from pywebpush import webpush
        from requests import Response

        private = ec.generate_private_key(ec.SECP256R1())
        public = private.public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
        )
        public_b64 = base64.urlsafe_b64encode(public).decode().rstrip("=")
        with self.assertRaises(VapidException):
            Vapid(private_key=private).sign(
                {"sub": "not a URI", "aud": "https://web.push.apple.com"}
            )

        with tempfile.TemporaryDirectory() as directory:
            key_file = Path(directory) / "vapid.pem"
            key_file.write_bytes(
                private.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption(),
                )
            )
            key_file.chmod(0o600)
            capture = {}

            def capture_send(_sender, _data, headers, **_kwargs):
                capture.update(headers)
                response = Response()
                response.status_code = 201
                return response

            claims = {"sub": "mailto:test@example.invalid", "exp": 1}
            with patch("pywebpush.WebPusher.send", new=capture_send):
                webpush(
                    subscription_info=subscription_body(),
                    data=None,
                    vapid_private_key=str(key_file),
                    vapid_claims=claims,
                    ttl=300,
                )

        token = capture["Authorization"].split(" t=", 1)[1].split(",k=", 1)[0]
        decoded = decode_vapid_jwt(token, public_b64)
        self.assertEqual(decoded["aud"], "https://web.push.apple.com")
        self.assertGreater(decoded["exp"], int(time.time()))
        self.assertLessEqual(decoded["exp"] - int(time.time()), 24 * 60 * 60)
