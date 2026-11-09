"""Décisions A11 validées : opérations réelles et lectures sans mutation, PostgreSQL."""

import json
import uuid
from datetime import UTC, date, datetime
from pathlib import Path
from unittest.mock import patch

from django.db import DatabaseError, connection, transaction
from django.test import TransactionTestCase

from apps.accounts.models import User
from apps.audit.models import AuditEvent
from apps.cases import classification, reading
from apps.cases.models import Case
from apps.core.errors import BusinessError
from apps.notifications.models import BusinessEvent, Notification, Outbox
from apps.purchasing import navigation, recommendations
from apps.purchasing.models import RecommendationEvent
from apps.receiving.models import QuarantineItem, Receipt
from apps.receiving.selectors import effective_receipts
from apps.receiving.state import item_state
from apps.requests.models import Delivery, InternalRequest
from apps.requests.selectors import effective_deliveries
from tests.test_accounts import FICTIVE_PASSWORD
from tests.test_notifications import NotificationFixtures
from tests.test_receiving import ReceivingFixtures

ROOT = Path("docs/recette/A11-FINAL")


class A11Tests(NotificationFixtures, TransactionTestCase):
    def reported(self, physical=10):
        p = self.demandable_product(physical=physical, maximum=100)
        self.assertEqual(self.modify(p, minimum=10, target=20).status_code, 200)
        self.assertEqual(self.rec_action(p, "defer").status_code, 200)
        return p

    def triggers(self, p):
        return BusinessEvent.objects.filter(
            event_type=recommendations.STOCK_REACTIVATION, aggregate_id=p.pk
        )

    def receipt_correct(self, receipt, case, quantity):
        payload = ReceivingFixtures.correction_body(self, receipt, case, [quantity])
        preview = self.post_json("/api/v1/corrections/preview", payload)
        self.assertEqual(preview.status_code, 200, preview.content)
        payload["preview_hash"] = preview.json()["preview"]["preview_hash"]
        response = self.post_json("/api/v1/corrections/confirm", payload)
        self.assertEqual(response.status_code, 200, response.content)
        return response

    def review(self, case):
        case.refresh_from_db()
        response = self.post_json(
            f"/api/v1/cases/{case.pk}/review",
            {
                "expected_version": case.version,
                "verification": True,
                "message": "Vérification fictive A11.",
            },
        )
        self.assertEqual(response.status_code, 200, response.content)
        case.refresh_from_db()

    def test_report_memorizes_available_not_physical(self):
        p = self.demandable_product(physical=10)
        self.assertEqual(self.modify(p, minimum=10, target=20).status_code, 200)
        self.send([p], [2])
        self.rec_action(p, "defer")
        event = RecommendationEvent.objects.get()
        self.assertEqual(
            (event.available, event.before["physical"], event.before["reserved"]), (8, 10, 2)
        )

    def test_drop_ten_to_nine_reactivates_positive_need(self):
        p = self.reported()
        self.adjust([p], [9])
        self.assertEqual(self.need(p)["state"], "ACTIVE")
        event = self.triggers(p).get()
        self.assertEqual(
            (
                event.payload["available_at_report"],
                event.payload["available_after"],
                event.payload["recommended"],
            ),
            (10, 9, 11),
        )
        self.assertEqual(RecommendationEvent.objects.count(), 1)

    def test_covered_drop_does_not_reactivate(self):
        p = self.reported()
        self.buy([p], [20])
        self.adjust([p], [9])
        self.assertEqual((self.need(p)["state"], self.need(p)["recommended"]), ("COVERED", 0))
        self.assertFalse(self.triggers(p).exists())
        self.assertTrue(self.need(p)["deferred"])

    def test_w_only_after_covered_drop_keeps_report(self):
        p = self.reported()
        order = self.buy([p], [20])
        self.adjust([p], [9])
        self.cancel_order(order)
        self.assertEqual(self.need(p)["state"], "DEFERRED")
        self.assertFalse(self.triggers(p).exists())
        self.assertEqual(self.rec_action(p, "reactivate").status_code, 200)
        self.assertEqual(self.need(p)["state"], "ACTIVE")

    def test_report_at_zero_has_no_loop_or_events_on_get(self):
        p = self.reported(0)
        before = (
            BusinessEvent.objects.count(),
            AuditEvent.objects.count(),
            Notification.objects.count(),
        )
        for _ in range(5):
            self.assertEqual(self.need(p)["state"], "DEFERRED")
            self.admin_client.get("/achats/besoins")
        self.assertEqual(
            before,
            (
                BusinessEvent.objects.count(),
                AuditEvent.objects.count(),
                Notification.objects.count(),
            ),
        )
        self.assertFalse(self.triggers(p).exists())

    def test_zero_to_two_to_zero_even_unseen_reactivates(self):
        p = self.reported(0)
        self.adjust([p], [2])
        self.adjust([p], [0])
        self.assertEqual(self.need(p)["state"], "ACTIVE")
        self.assertEqual(self.triggers(p).count(), 1)

    def test_same_d_stock_version_is_not_new_zero_transition(self):
        p = self.reported(0)
        order = self.buy([p], [2])
        self.receive(order, [2])
        # Réservation réelle vers zéro constitue la vraie baisse, pas le changement de version.
        self.send([p], [2])
        self.assertEqual(self.triggers(p).count(), 1)
        req = InternalRequest.objects.get()
        self.claim(req)
        req.refresh_from_db()
        self.deliver(req)
        self.assertEqual(self.triggers(p).count(), 1)
        self.assertEqual(self.stock(p)[:3], (0, 0, 0))

    def test_manual_reactivation_audited_and_current_conditions(self):
        p = self.reported()
        self.assertEqual(self.rec_action(p, "reactivate").status_code, 200)
        event = RecommendationEvent.objects.latest("revision")
        self.assertEqual(event.action, "REACTIVATE")
        self.assertTrue(AuditEvent.objects.filter(operation=event.operation).exists())
        self.rec_action(p, "defer")
        self.buy([p], [30])
        self.assertEqual(self.rec_action(p, "reactivate").status_code, 409)
        self.assertEqual(RecommendationEvent.objects.count(), 3)

    def test_reservation_drop_is_a_real_trigger(self):
        p = self.reported()
        self.send([p], [1])
        self.assertEqual(self.triggers(p).count(), 1)
        self.assertEqual(self.stock(p)[:3], (10, 1, 9))

    def test_physical_drop_is_a_real_trigger(self):
        p = self.reported()
        self.adjust([p], [9])
        self.assertEqual(self.triggers(p).count(), 1)
        self.assertEqual(self.stock(p)[:3], (9, 0, 9))

    def test_shortage_visible_with_waiting_while_badge_zero(self):
        p = self.reported(0)
        self.buy([p], [20])
        row = self.need(p)
        self.assertEqual((row["recommended"], row["shortage"], row["waiting"]), (0, True, 20))
        self.assertEqual(navigation.pending_count(self.admin), 0)
        html = self.admin_client.get(f"/achats/besoins/{p.pk}").content.decode()
        self.assertIn("Rupture de stock — 20", html)
        self.assertIn("attendus", html)
        self.assertIn("Rupture de stock", self.admin_client.get("/achats/besoins").content.decode())

    def test_shortage_visible_deferred_and_disappears_on_real_increase(self):
        p = self.reported(0)
        self.assertTrue(self.need(p)["shortage"])
        self.assertEqual(navigation.pending_count(self.admin), 0)
        self.adjust([p], [2])
        self.assertFalse(self.need(p)["shortage"])
        self.assertEqual(self.need(p)["state"], "DEFERRED")

    def test_drop_above_threshold_does_not_become_trigger_after_w_or_parameter_change(self):
        p = self.reported()
        self.adjust([p], [15])
        self.adjust([p], [12])
        self.assertFalse(self.triggers(p).exists())
        p.refresh_from_db()
        self.modify(p, minimum=15, target=20)
        self.assertEqual(self.need(p)["state"], "DEFERRED")

    def test_trigger_is_unique_on_double_confirmation_and_worker_replay(self):
        p = self.reported()
        self.process()
        identifier, key = uuid.uuid4(), uuid.uuid4().hex
        first = self.send([p], [1], identifier=identifier, key=key)
        second = self.send([p], [1], identifier=identifier, key=key)
        self.assertEqual(first.json()["operation_id"], second.json()["operation_id"])
        self.process()
        self.process()
        trigger = self.triggers(p).get()
        self.assertEqual(
            Notification.objects.filter(event=trigger, recipient=self.admin).count(), 1
        )
        self.assertEqual(
            Notification.objects.filter(
                operation=trigger.operation, recipient=self.admin, target_id=p.pk
            ).count(),
            1,
        )

    def test_delayed_worker_does_not_duplicate_reactivation_as_generic_need(self):
        p = self.reported()
        self.adjust([p], [9])
        self.process()
        notes = Notification.objects.filter(recipient=self.admin, target_id=p.pk)
        self.assertEqual(notes.count(), 1)
        self.assertEqual(notes.get().event_id, self.triggers(p).get().pk)

    def test_new_report_requires_new_actual_drop(self):
        p = self.reported()
        self.adjust([p], [9])
        self.rec_action(p, "defer")
        self.assertEqual(self.need(p)["state"], "DEFERRED")
        self.adjust([p], [8])
        self.assertEqual(self.triggers(p).count(), 2)
        self.assertEqual(self.need(p)["state"], "ACTIVE")

    def test_competing_stock_adjustments_one_trigger_and_read_recalculations_no_events(self):
        from apps.core import idempotency
        from apps.core.payloads import payload_hash

        p = self.reported()
        v = self.stock(p)[3]
        items = []
        for target in [9, 8]:
            payload = {
                "lines": [
                    {
                        "product_id": str(p.pk),
                        "expected_stock_version": v,
                        "target_physical": target,
                    }
                ]
            }
            identifier, key = uuid.uuid4(), uuid.uuid4().hex
            idempotency.register_intent(
                self.context,
                intent_id=identifier,
                key=key,
                fingerprint=payload_hash(payload),
                command_type="inventory.adjust",
                resource_id=None,
            )
            items.append((self.context, payload, identifier, key))
        outcomes = self.race(items)
        self.assertEqual(sorted(o.status for o in outcomes), [200, 409])
        self.assertEqual(self.triggers(p).count(), 1)
        before = BusinessEvent.objects.count()
        for _ in range(5):
            self.need(p)
        self.assertEqual(BusinessEvent.objects.count(), before)

    def test_trigger_rolls_back_with_failed_transaction(self):
        p = self.reported()
        before = self.stock(p)
        with patch(
            "apps.core.idempotency.Outbox.objects.create", side_effect=RuntimeError("Panne fictive")
        ):
            response = self.adjust([p], [9])
        self.assertEqual(response.status_code, 500)
        self.assertEqual(self.stock(p), before)
        self.assertFalse(self.triggers(p).exists())

    def test_trigger_events_are_sql_immutable(self):
        p = self.reported()
        self.adjust([p], [9])
        event = self.triggers(p).get()
        with self.assertRaises(DatabaseError), transaction.atomic():
            BusinessEvent.objects.filter(pk=event.pk).update(payload={})

    def test_category_mapping_exhaustive_and_one_primary_per_kind(self):
        expected = {
            "SURPLUS": "TO_VERIFY",
            "DELIVERY_ERROR": "TO_VERIFY",
            "RECEIPT_ERROR": "TO_VERIFY",
            "STOCK_CONFLICT": "REPORTS",
            "SHORTAGE": "REPORTS",
            "NONCONFORMITY": "REPORTS",
            "UNKNOWN_ORDER": "REPORTS",
            "LATE_CANCELLED_DELIVERY": "REPORTS",
        }
        self.assertEqual(classification.BY_KIND, expected)
        for kind, category in expected.items():
            self.assertEqual(classification.primary_category(kind), category)

    def test_case_category_stable_when_reviewed(self):
        _, _, delivery, case = self.completed()
        before = classification.open_counts(self.admin)
        self.assertEqual(reading.data(case)["primary_category"], "TO_VERIFY")
        self.review(case)
        self.assertEqual(reading.data(case)["primary_category"], "TO_VERIFY")
        self.assertEqual(classification.open_counts(self.admin), before)
        self.assertEqual(Delivery.objects.get(pk=delivery.pk).operator_id, self.replenisher.pk)

    def test_stock_conflict_has_reports_category(self):
        req, products, case = self.conflict()
        self.assertEqual(reading.data(case)["primary_category"], "REPORTS")
        self.assertEqual(classification.open_counts(self.admin), {"TO_VERIFY": 0, "REPORTS": 1})

    def test_duplicate_links_and_notifications_do_not_duplicate_case_count(self):
        _, _, delivery, case = self.completed()
        self.process()
        self.review(case)
        self.process()
        self.assertGreaterEqual(Notification.objects.filter(target_id=case.pk).count(), 2)
        self.assertEqual(classification.open_counts(self.admin)["TO_VERIFY"], 1)
        with patch("apps.cases.reading.query", return_value=Case.objects.filter(events__case=case)):
            self.assertEqual(classification.open_counts(self.admin)["TO_VERIFY"], 1)

    def test_notification_read_does_not_resolve_or_change_count(self):
        _, _, _, case = self.completed()
        self.process()
        notification = Notification.objects.filter(recipient=self.admin, target_id=case.pk).first()
        self.assertIsNotNone(notification)
        before = classification.open_counts(self.admin)
        self.assertEqual(
            self.admin_client.post(f"/notifications/{notification.pk}/lire").status_code, 302
        )
        self.assertEqual(classification.open_counts(self.admin), before)
        case.refresh_from_db()
        self.assertEqual(case.status, "OPEN")

    def test_resolved_case_leaves_open_count_but_keeps_nature(self):
        _, _, delivery, case = self.completed()
        self.correct(delivery, case, [8])
        case.refresh_from_db()
        self.assertEqual(case.status, "RESOLVED")
        self.assertEqual(classification.open_counts(self.admin)["TO_VERIFY"], 0)
        self.assertEqual(reading.data(case)["primary_category"], "TO_VERIFY")

    def test_surplus_and_nonconforming_are_distinct_real_cases(self):
        p = self.demandable_product(physical=0)
        order = self.buy([p], [2])
        self.receive(order, [3], [1])
        self.assertEqual(
            set(Case.objects.values_list("kind", flat=True)), {"SURPLUS", "NONCONFORMITY"}
        )
        self.assertEqual(classification.open_counts(self.admin), {"TO_VERIFY": 1, "REPORTS": 1})
        for case in Case.objects.all():
            self.review(case)
        self.assertEqual(classification.open_counts(self.admin), {"TO_VERIFY": 1, "REPORTS": 1})

    def test_receipt_error_category(self):
        p = self.demandable_product(physical=0)
        order = self.buy([p], [10])
        self.receive(order, [10])
        case = self.receipt_case(Receipt.objects.get())
        self.assertEqual(reading.data(case)["primary_category"], "TO_VERIFY")

    def test_unknown_order_category(self):
        report = self.unknown(message="Colis fictif non identifié")
        self.assertEqual(reading.data(report.case)["primary_category"], "REPORTS")

    def test_shortage_category_is_reports_not_correction(self):
        req, _ = self.prepare()
        self.deliver(req, [0], mode="SHORTAGE")
        case = Case.objects.get(kind="SHORTAGE")
        self.assertEqual(reading.data(case)["primary_category"], "REPORTS")
        self.assertEqual(classification.open_counts(self.admin), {"TO_VERIFY": 0, "REPORTS": 1})

    def test_late_cancelled_category_is_reports(self):
        p = self.demandable_product(physical=0)
        order = self.buy([p], [10])
        self.cancel_remainder(order, 10)
        order.refresh_from_db()
        line = order.lines.get()
        response = self.post_json(
            f"/api/v1/orders/{order.pk}/late-delivery-reports",
            {
                "expected_version": order.version,
                "lines": [{"order_line_id": str(line.pk), "observed_quantity": 2}],
            },
            self.operator,
        )
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(
            reading.data(Case.objects.get(kind="LATE_CANCELLED_DELIVERY"))["primary_category"],
            "REPORTS",
        )

    def test_incomplete_commands_count_by_order_not_lines_and_exclude_cancelled(self):
        a = self.demandable_product(code="A", physical=0)
        b = self.demandable_product(code="B", physical=0)
        order = self.buy([a, b], [10, 20])
        self.assertEqual(navigation.incomplete_count(self.admin), 1)
        self.receive(order, [5, 10])
        self.assertEqual(navigation.incomplete_count(self.admin), 1)
        self.cancel_order(order)
        self.assertEqual(navigation.incomplete_count(self.admin), 0)

    def test_existing_two_tabs_and_five_zones_remain(self):
        for path in ["/stocks/signalements", "/livraisons/signalements"]:
            response = self.admin_client.get(path)
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, "Stock de la réserve")
            self.assertContains(response, "Remises aux unités")
            self.assertContains(response, 'aria-current="page"')

    def test_delivery_ten_to_eight_at_original_day(self):
        day = datetime(2026, 10, 5, 10, tzinfo=UTC)
        with patch("apps.core.idempotency.database_now", return_value=day):
            _, _, delivery, case = self.completed()
        with patch(
            "apps.core.idempotency.database_now",
            return_value=datetime(2026, 10, 10, 10, tzinfo=UTC),
        ):
            self.correct(delivery, case, [8])
        rows = list(effective_deliveries(self.admin, date(2026, 10, 5), date(2026, 10, 5)))
        self.assertEqual(
            [(r.effective_quantity, r.origin_day) for r in rows], [(8, date(2026, 10, 5))]
        )
        self.assertFalse(
            effective_deliveries(self.admin, date(2026, 10, 10), date(2026, 10, 10)).exists()
        )
        self.assertEqual(delivery.lines.get().handed, 10)

    def test_successive_corrections_effective_nine_not_sum_states(self):
        _, _, delivery, case = self.completed()
        self.correct(delivery, case, [8])
        self.review(case)
        self.correct(delivery, case, [9])
        day = delivery.operation.confirmed_at.astimezone(
            __import__("zoneinfo").ZoneInfo("Europe/Paris")
        ).date()
        row = effective_deliveries(self.admin, day, day).get()
        self.assertEqual(row.effective_quantity, 9)
        deltas = list(
            delivery.corrections.order_by("decision__event__revision").values_list(
                "lines__effective_before", "lines__effective_after"
            )
        )
        self.assertEqual(deltas, [(10, 8), (8, 9)])

    def test_correction_crossing_month_uses_origin_month(self):
        with patch(
            "apps.core.idempotency.database_now",
            return_value=datetime(2026, 1, 31, 22, 30, tzinfo=UTC),
        ):
            _, _, delivery, case = self.completed()
        with patch(
            "apps.core.idempotency.database_now", return_value=datetime(2026, 2, 2, 12, tzinfo=UTC)
        ):
            self.correct(delivery, case, [8])
        self.assertEqual(
            effective_deliveries(self.admin, date(2026, 1, 1), date(2026, 1, 31))
            .get()
            .effective_quantity,
            8,
        )
        self.assertFalse(
            effective_deliveries(self.admin, date(2026, 2, 1), date(2026, 2, 28)).exists()
        )

    def test_receipt_correction_origin_date_and_original_immutable(self):
        p = self.demandable_product(physical=0)
        order = self.buy([p], [10])
        with patch(
            "apps.core.idempotency.database_now", return_value=datetime(2026, 9, 30, 10, tzinfo=UTC)
        ):
            self.receive(order, [10])
        receipt = Receipt.objects.get()
        case = self.receipt_case(receipt)
        self.receipt_correct(receipt, case, 8)
        row = effective_receipts(self.admin, date(2026, 9, 1), date(2026, 9, 30)).get()
        self.assertEqual(
            (row.regular, row.effective_quantity, row.origin_day), (10, 8, date(2026, 9, 30))
        )
        self.assertEqual(order.lines.get().received, 8)
        self.assertEqual(self.stock(p)[:3], (8, 0, 8))

    def test_accepted_surplus_is_not_regular_origin_receipt(self):
        p = self.demandable_product(physical=0)
        order = self.buy([p], [20])
        self.receive(order, [23])
        item = QuarantineItem.objects.get(kind="SURPLUS")
        state = item_state(item.pk)
        response = self.post_json(
            f"/api/v1/quarantine/{item.pk}/accept-surplus",
            {
                "expected_version": state["version"],
                "quantity": 3,
                "verification": True,
                "expected_stock_version": self.stock(p)[3],
            },
        )
        self.assertEqual(response.status_code, 200, response.content)
        receipt = Receipt.objects.get()
        day = receipt.event.operation.confirmed_at.astimezone(
            __import__("zoneinfo").ZoneInfo("Europe/Paris")
        ).date()
        self.assertEqual(effective_receipts(self.admin, day, day).get().effective_quantity, 20)
        self.assertEqual(self.stock(p)[:3], (23, 0, 23))

    def test_paris_date_not_utc_date(self):
        with patch(
            "apps.core.idempotency.database_now",
            return_value=datetime(2026, 6, 30, 22, 30, tzinfo=UTC),
        ):
            self.completed()
        self.assertFalse(
            effective_deliveries(self.admin, date(2026, 6, 30), date(2026, 6, 30)).exists()
        )
        self.assertEqual(
            effective_deliveries(self.admin, date(2026, 7, 1), date(2026, 7, 1)).get().origin_day,
            date(2026, 7, 1),
        )

    def test_summer_time_day_keeps_both_edges(self):
        from apps.core.models import Operation
        from apps.core.periods import origin_period

        with patch(
            "apps.core.idempotency.database_now",
            return_value=datetime(2026, 3, 28, 23, 30, tzinfo=UTC),
        ):
            _, _, delivery, _ = self.completed()
        self.assertTrue(
            origin_period(
                Operation.objects.filter(pk=delivery.operation_id),
                "confirmed_at",
                date(2026, 3, 29),
                date(2026, 3, 29),
            ).exists()
        )
        self.assertEqual(
            effective_deliveries(self.admin, date(2026, 3, 29), date(2026, 3, 29))
            .get()
            .effective_quantity,
            10,
        )

    def test_winter_time_repeated_hour_one_source_each(self):
        from apps.core.models import Operation
        from apps.core.periods import origin_period

        with patch(
            "apps.core.idempotency.database_now",
            return_value=datetime(2026, 10, 25, 0, 30, tzinfo=UTC),
        ):
            _, _, delivery, _ = self.completed()
        with patch(
            "apps.core.idempotency.database_now",
            return_value=datetime(2026, 10, 25, 1, 30, tzinfo=UTC),
        ):
            self.review(Case.objects.get(delivery=delivery))
        day = date(2026, 10, 25)
        self.assertEqual(effective_deliveries(self.admin, day, day).count(), 1)
        self.assertEqual(
            origin_period(
                Operation.objects.filter(resource_id=Case.objects.get(delivery=delivery).pk),
                "confirmed_at",
                day,
                day,
            ).count(),
            1,
        )

    def test_audit_dates_and_snapshots_unchanged_by_reading(self):
        _, _, delivery, case = self.completed()
        self.correct(delivery, case, [8])
        before = list(AuditEvent.objects.order_by("pk").values())
        stock = self.stock(delivery.lines.get().line.product)
        from django.utils import timezone

        day = timezone.localdate()
        for _ in range(3):
            list(effective_deliveries(self.admin, day, day))
            classification.open_counts(self.admin)
        self.assertEqual(list(AuditEvent.objects.order_by("pk").values()), before)
        self.assertEqual(self.stock(delivery.lines.get().line.product), stock)

    def test_admin_contracts_refuse_assistant_doctor_and_replenisher(self):
        doctor = User.objects.create_user(
            "medecin-a11",
            FICTIVE_PASSWORD,
            display_name="Demandeur B fictif",
            role="DOCTOR",
            floor=self.floor,
            must_change_password=False,
        )
        for user in [self.assistant, doctor, self.replenisher]:
            for selector in [effective_deliveries, effective_receipts]:
                with self.assertRaises(BusinessError) as error:
                    selector(user, date(2026, 1, 1), date(2026, 12, 31))
                self.assertEqual(error.exception.status, 403)
            with self.assertRaises(BusinessError):
                navigation.pending_count(user)
            with self.assertRaises(BusinessError):
                navigation.incomplete_count(user)
        for user in [self.assistant, doctor]:
            with self.assertRaises(BusinessError):
                classification.open_counts(user)

    def test_replenisher_case_count_limited_to_own_scope(self):
        _, _, _, case = self.completed()
        self.assertEqual(classification.open_counts(self.replenisher)["TO_VERIFY"], 1)
        other = User.objects.create_user(
            "autre-reap-a11", FICTIVE_PASSWORD, role="REPLENISHER", must_change_password=False
        )
        self.assertEqual(classification.open_counts(other), {"TO_VERIFY": 0, "REPORTS": 0})
        self.assertEqual(self.user_client.get(f"/api/v1/cases/{case.pk}").status_code, 403)

    def test_doctor_same_requests_and_reservations_without_admin_access(self):
        doctor = User.objects.create_user(
            "medecin-a11",
            FICTIVE_PASSWORD,
            display_name="Demandeur B fictif",
            role="DOCTOR",
            floor=self.floor,
            must_change_password=False,
        )
        client = self.logged(doctor)
        p = self.reported()
        response = self.send([p], [1], client=client)
        self.assertEqual(response.status_code, 201, response.content)
        req = InternalRequest.objects.get(pk=response.json()["resource"]["id"])
        self.assertEqual(self.cancel_request(req, client=client).status_code, 200)
        for path in ["/api/v1/recommendations", "/achats/besoins", "/stocks/signalements"]:
            self.assertEqual(client.get(path).status_code, 403)
        self.assertEqual(self.stock(p)[:3], (10, 0, 10))

    def test_parallel_read_recalculations_do_not_write_or_duplicate_events(self):
        import threading
        from concurrent.futures import ThreadPoolExecutor

        from django.db import close_old_connections

        p = self.reported()
        self.adjust([p], [9])
        before = (BusinessEvent.objects.count(), AuditEvent.objects.count(), Outbox.objects.count())
        barrier = threading.Barrier(8, timeout=15)

        def read(_):
            close_old_connections()
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_backend_pid()")
                    pid = cursor.fetchone()[0]
                barrier.wait()
                return pid, recommendations.data(recommendations.query().get(pk=p.pk))["state"]
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=8) as pool:
            rows = list(pool.map(read, range(8)))
        self.assertEqual(len({pid for pid, state in rows}), 8)
        self.assertEqual({state for pid, state in rows}, {"ACTIVE"})
        self.assertEqual(
            before,
            (BusinessEvent.objects.count(), AuditEvent.objects.count(), Outbox.objects.count()),
        )
        ROOT.mkdir(parents=True, exist_ok=True)
        (ROOT / "read-concurrency.json").write_text(
            json.dumps(
                {
                    "connections": 8,
                    "backend_pids": [pid for pid, state in rows],
                    "states": [state for pid, state in rows],
                    "before_after_counts": list(before),
                },
                indent=2,
            )
        )

    def test_zero_target_without_waiting_does_not_claim_purchase_coverage(self):
        p = self.demandable_product(physical=0)
        self.assertEqual(self.modify(p, minimum=0, target=0).status_code, 200)
        row = self.need(p)
        self.assertEqual((row["waiting"], row["recommended"], row["state"]), (0, 0, "INACTIVE"))
        self.assertTrue(row["shortage"])
        self.assertEqual(navigation.pending_count(self.admin), 0)

    def test_successive_receipt_corrections_use_effective_regular_only(self):
        p = self.demandable_product(physical=0)
        order = self.buy([p], [10])
        self.receive(order, [10])
        receipt = Receipt.objects.get()
        case = self.receipt_case(receipt)
        self.receipt_correct(receipt, case, 8)
        # Même dossier et nouvelle correction vérifiée, selon le contrat M8 existant.
        self.receipt_correct(receipt, case, 9)
        from django.utils import timezone

        row = effective_receipts(self.admin, timezone.localdate(), timezone.localdate()).get()
        self.assertEqual((row.regular, row.effective_quantity), (10, 9))
        self.assertEqual(self.stock(p)[:3], (9, 0, 9))
