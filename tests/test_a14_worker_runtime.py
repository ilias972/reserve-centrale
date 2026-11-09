"""Régression découverte A14 : worker sous les véritables droits SQL applicatifs."""

import os
from contextlib import contextmanager
from copy import deepcopy
from unittest.mock import patch

from django.db import connection
from django.test import TransactionTestCase, override_settings

from apps.core.management.commands.grant_runtime import grant_runtime
from apps.core.models import Operation
from apps.inventory.models import StockEntry
from apps.notifications import dispatcher, worker
from apps.notifications.models import CapturedEmail, Notification, WorkItem
from tests.test_notifications import NotificationFixtures


class A14WorkerRuntimeTests(NotificationFixtures, TransactionTestCase):
    @contextmanager
    def runtime(self):
        if not connection.settings_dict["NAME"].startswith("test_"):
            raise RuntimeError("Base de test obligatoire")
        grant_runtime(os.environ["RUNTIME_DATABASE_USER"])
        original = deepcopy(connection.settings_dict)
        connection.close()
        connection.settings_dict.update(
            USER=os.environ["RUNTIME_DATABASE_USER"],
            PASSWORD=os.environ["RUNTIME_DATABASE_PASSWORD"],
        )
        try:
            with connection.cursor() as c:
                for table in [
                    "notifications_outbox",
                    "notifications_businessevent",
                    "core_operation",
                ]:
                    c.execute("SELECT has_table_privilege(current_user,%s,'UPDATE')", [table])
                    self.assertFalse(c.fetchone()[0])
            yield
        finally:
            connection.close()
            connection.settings_dict.clear()
            connection.settings_dict.update(original)

    def test_real_runtime_worker_projects_without_locking_immutable_facts(self):
        request, products = self.requested()
        before = (Operation.objects.count(), StockEntry.objects.count(), self.stock(products[0]))
        with self.runtime():
            self.process()
            self.assertTrue(
                Notification.objects.filter(
                    recipient=self.replenisher,
                    event__event_type="request.created",
                    target_id=request.pk,
                ).exists()
            )
            self.assertFalse(
                WorkItem.objects.filter(state__in=["RETRY", "DEAD", "CLAIMED"]).exists()
            )
        self.assertEqual(
            before, (Operation.objects.count(), StockEntry.objects.count(), self.stock(products[0]))
        )

    def test_real_runtime_worker_records_temporary_failure_without_extra_grants(self):
        self.requested()
        dispatcher.enqueue_events(100)
        before = (Operation.objects.count(), StockEntry.objects.count())
        with self.runtime():
            attempt = worker.claim()
            self.assertIsNotNone(attempt)
            with patch.object(
                dispatcher, "process", side_effect=worker.DeliveryFailure("A14_TEMPORARY", delay=1)
            ):
                worker.perform(attempt)
            job = WorkItem.objects.get(pk=attempt.work_id)
            self.assertEqual((job.state, job.result_code), ("RETRY", "A14_TEMPORARY"))
            self.assertFalse(worker.perform(attempt))
        self.assertEqual(before, (Operation.objects.count(), StockEntry.objects.count()))

    def test_real_runtime_email_capture_uses_only_job_lock(self):
        product = self.demandable_product()
        self.buy([product], [12])
        with (
            override_settings(
                NOTIFICATION_EXTERNAL_ALLOWED=False,
                NOTIFICATION_PUSH_ENABLED=False,
                NOTIFICATION_EMAIL_MODE="CAPTURE",
                NOTIFICATION_EMAIL_RECIPIENTS={
                    "RESPONSIBLE": "responsable-a14@example.invalid",
                    "ACCOUNTANT": "comptable-a14@example.invalid",
                },
                NOTIFICATION_EMAIL_FROM="prototype-a14@example.invalid",
            ),
            self.runtime(),
        ):
            self.process()
            self.assertEqual(CapturedEmail.objects.count(), 2)
