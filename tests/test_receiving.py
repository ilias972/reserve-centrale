"""M8 sur PostgreSQL réel : arrivées, isolation et décisions ne sont jamais confondues."""

from pathlib import Path

from django.test import TransactionTestCase

from apps.cases.models import Case
from apps.notifications.models import BusinessEvent
from apps.purchasing.models import PurchaseOrder
from apps.receiving.models import ArrivalReport, QuarantineItem, Receipt
from apps.receiving.services import ensure_registered
from apps.receiving.state import item_state
from tests.test_purchasing import PurchaseFixtures


class ReceivingFixtures(PurchaseFixtures):
    def setUp(self):
        super().setUp()
        ensure_registered()

    def receiving_body(self, order, conforming, nonconforming=None, **extra):
        order.refresh_from_db()
        lines = list(order.lines.select_related("product").order_by("product_code"))
        nonconforming = nonconforming or [0] * len(lines)
        return {
            "expected_version": order.version,
            "lines": [
                {
                    "order_line_id": str(line.pk),
                    "conforming": c,
                    "nonconforming": nc,
                    "nonconformity": "DAMAGED" if nc else "",
                    "expected_stock_version": self.stock(line.product)[3],
                }
                for line, c, nc in zip(lines, conforming, nonconforming, strict=True)
            ],
            **extra,
        }

    def receive(self, order, conforming, nonconforming=None, **extra):
        return self.post_json(
            f"/api/v1/orders/{order.pk}/receipts",
            self.receiving_body(order, conforming, nonconforming, **extra),
            self.operator,
        )

    def cancel_remainder(self, order, quantity):
        order.refresh_from_db()
        line = order.lines.get()
        response = self.post_json(
            f"/api/v1/orders/{order.pk}/cancel-remainder",
            {
                "expected_version": order.version,
                "external_verification": True,
                "lines": [{"order_line_id": str(line.pk), "quantity": quantity}],
            },
        )
        self.assertEqual(response.status_code, 200, response.content)
        return response

    def unknown(self, **body):
        response = self.post_json("/api/v1/cases/unknown-order", body, self.operator)
        self.assertEqual(response.status_code, 201, response.content)
        return ArrivalReport.objects.get(pk=response.json()["resource"]["id"])

    def link(self, report, order, quantities):
        report.case.refresh_from_db()
        lines = list(order.lines.order_by("product_code"))
        response = self.post_json(
            f"/api/v1/cases/{report.case_id}/link-order",
            {
                "order_id": str(order.pk),
                "expected_version": report.case.version,
                "verification": True,
                "lines": [
                    {"order_line_id": str(line.pk), "quantity": q}
                    for line, q in zip(lines, quantities, strict=True)
                ],
            },
        )
        self.assertEqual(response.status_code, 200, response.content)
        return response

    def receipt_case(self, receipt):
        response = self.post_json(
            f"/api/v1/receipts/{receipt.pk}/reports",
            {"message": "Vérification de recette fictive"},
            self.operator,
        )
        self.assertEqual(response.status_code, 201, response.content)
        return Case.objects.get(pk=response.json()["resource"]["id"])

    def correction_body(self, receipt, case, conforming):
        receipt.refresh_from_db()
        case.refresh_from_db()
        order = PurchaseOrder.objects.get(pk=receipt.event.order_id)
        lines = list(
            receipt.lines.select_related("order_line__product").order_by("order_line__product_code")
        )
        from django.db.models import Q

        items = list(
            QuarantineItem.objects.filter(
                Q(receipt_line__receipt=receipt) | Q(consumptions__line__receipt=receipt)
            ).distinct()
        )
        return {
            "kind": "RECEIPT",
            "source_id": str(receipt.pk),
            "case_id": str(case.pk),
            "expected_versions": {
                "order": order.version,
                "receipt": receipt.version,
                "case": case.version,
                "stocks": {
                    str(line.order_line.product_id): self.stock(line.order_line.product)[3]
                    for line in lines
                },
                "quarantine": {str(item.pk): item_state(item.pk)["version"] for item in items},
            },
            "corrected_lines": [
                {
                    "receipt_line_id": str(line.pk),
                    "conforming": q,
                    "nonconforming": 0,
                    "nonconformity": "",
                }
                for line, q in zip(lines, conforming, strict=True)
            ],
            "verification": True,
            "message": "Quantités réellement vérifiées dans les données fictives.",
        }


class ReceivingTests(ReceivingFixtures, TransactionTestCase):
    def test_regular_full_receipt_adds_only_physical_and_f(self):
        product = self.demandable_product(physical=0)
        order = self.buy([product], [20])
        response = self.receive(order, [20])
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(self.stock(product)[:3], (20, 0, 20))
        line = order.lines.get()
        order.refresh_from_db()
        self.assertEqual(
            (line.received, line.cancelled, line.waiting, order.status), (20, 0, 0, "RECEIVED")
        )
        self.assertEqual(Receipt.objects.count(), 1)

    def test_surplus_23_for_20_isolated_then_n_accepts_once(self):
        product = self.demandable_product(physical=0)
        order = self.buy([product], [20])
        response = self.receive(order, [23])
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(self.stock(product)[:3], (20, 0, 20))
        item = QuarantineItem.objects.get(kind="SURPLUS")
        current = item_state(item.pk)
        self.assertEqual(current["remaining"], 3)
        response = self.post_json(
            f"/api/v1/quarantine/{item.pk}/accept-surplus",
            {
                "expected_version": current["version"],
                "quantity": 3,
                "verification": True,
                "expected_stock_version": self.stock(product)[3],
            },
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self.stock(product)[:3], (23, 0, 23))
        self.assertEqual(order.lines.get().received, 20)
        item.case.refresh_from_db()
        self.assertEqual(item.case.status, "RESOLVED")

    def test_nonconforming_stays_outside_and_replacement_arrives_regularly(self):
        product = self.demandable_product(physical=0)
        order = self.buy([product], [20])
        response = self.receive(order, [12], [8])
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(self.stock(product)[0], 12)
        self.assertEqual(order.lines.get().waiting, 8)
        self.assertEqual(item_state(QuarantineItem.objects.get().pk)["remaining"], 8)
        response = self.receive(order, [8])
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(self.stock(product)[0], 20)
        self.assertEqual(item_state(QuarantineItem.objects.get().pk)["remaining"], 8)

    def test_unknown_unidentified_link_and_real_receipt_consume_the_parcel(self):
        product = self.demandable_product(physical=0)
        report = self.unknown()
        self.assertEqual(self.stock(product)[0], 0)
        order = self.buy([product], [5])
        self.link(report, order, [5])
        report.case.refresh_from_db()
        response = self.receive(
            order,
            [5],
            unknown_delivery_id=str(report.pk),
            expected_source_version=report.case.version,
        )
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(self.stock(product)[0], 5)
        self.assertEqual(item_state(QuarantineItem.objects.get().pk)["remaining"], 0)
        report.case.refresh_from_db()
        self.assertEqual(report.case.status, "RESOLVED")

    def test_late_remainder_requires_separate_reinstatement_and_receipt(self):
        product = self.demandable_product(physical=0)
        order = self.buy([product], [20])
        self.assertEqual(self.receive(order, [12]).status_code, 201)
        self.cancel_remainder(order, 8)
        order.refresh_from_db()
        line = order.lines.get()
        response = self.post_json(
            f"/api/v1/orders/{order.pk}/late-delivery-reports",
            {
                "expected_version": order.version,
                "lines": [{"order_line_id": str(line.pk), "observed_quantity": 8}],
            },
            self.operator,
        )
        self.assertEqual(response.status_code, 201, response.content)
        report = ArrivalReport.objects.get()
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
        self.assertEqual(self.stock(product)[0], 12)
        report.case.refresh_from_db()
        response = self.receive(
            order,
            [3],
            late_delivery_report_id=str(report.pk),
            expected_source_version=report.case.version,
        )
        self.assertEqual(response.status_code, 201, response.content)
        line.refresh_from_db()
        self.assertEqual((line.received, line.cancelled, line.waiting), (15, 5, 0))
        self.assertEqual(self.stock(product)[0], 15)
        self.assertEqual(BusinessEvent.objects.filter(event_type="purchase.recorded").count(), 1)

    def test_correction_receipt_10_to_8_applies_only_minus_2(self):
        product = self.demandable_product(physical=0)
        order = self.buy([product], [10])
        self.assertEqual(self.receive(order, [10]).status_code, 201)
        receipt = Receipt.objects.get()
        case = self.receipt_case(receipt)
        body = self.correction_body(receipt, case, [8])
        preview = self.post_json("/api/v1/corrections/preview", body)
        self.assertEqual(preview.status_code, 200, preview.content)
        self.assertEqual(self.stock(product)[0], 10)
        body["preview_hash"] = preview.json()["preview"]["preview_hash"]
        response = self.post_json("/api/v1/corrections/confirm", body)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self.stock(product)[0], 8)
        self.assertEqual((order.lines.get().received, order.lines.get().waiting), (8, 2))

    def decision(self, item, action, client=None):
        state = item_state(item.pk)
        return self.post_json(
            f"/api/v1/quarantine/{item.pk}/{action}",
            {
                "expected_version": state["version"],
                "quantity": state["remaining"],
                "verification": True,
                "expected_stock_version": self.stock(item.receipt_line.order_line.product)[3],
            },
            client,
        )

    def correct(self, receipt, case, quantities):
        body = self.correction_body(receipt, case, quantities)
        preview = self.post_json("/api/v1/corrections/preview", body)
        self.assertEqual(preview.status_code, 200, preview.content)
        body["preview_hash"] = preview.json()["preview"]["preview_hash"]
        return self.post_json("/api/v1/corrections/confirm", body)

    def test_successive_partial_arrivals_keep_equation(self):
        p = self.demandable_product(physical=2)
        order = self.buy([p], [10])
        for q, total, status in [(3, 3, "PARTIAL"), (2, 5, "PARTIAL"), (5, 10, "RECEIVED")]:
            self.assertEqual(self.receive(order, [q]).status_code, 201)
            order.refresh_from_db()
            line = order.lines.get()
            self.assertEqual(
                (line.received, line.waiting, order.status), (total, 10 - total, status)
            )
            self.assertEqual(line.quantity, line.received + line.cancelled + line.waiting)
        self.assertEqual(self.stock(p)[0], 12)
        self.assertEqual(Receipt.objects.count(), 3)

    def test_multi_product_invalid_line_rolls_everything_back(self):
        products = [self.demandable_product(code=f"ATOMIC-{i}", physical=0) for i in range(2)]
        order = self.buy(products, [4, 5])
        body = self.receiving_body(order, [4, 5])
        body["lines"][1]["expected_stock_version"] += 1
        response = self.post_json(f"/api/v1/orders/{order.pk}/receipts", body, self.operator)
        self.assertEqual(response.status_code, 409)
        self.assertEqual([self.stock(p)[0] for p in products], [0, 0])
        self.assertEqual(Receipt.objects.count(), 0)
        self.assertEqual(sum(line.received for line in order.lines.all()), 0)

    def test_multi_product_valid_one_confirmation(self):
        products = [self.demandable_product(code=f"MULTI-{i}", physical=0) for i in range(3)]
        order = self.buy(products, [4, 5, 6])
        self.assertEqual(self.receive(order, [4, 3, 6]).status_code, 201)
        self.assertEqual([self.stock(p)[0] for p in products], [4, 3, 6])
        self.assertEqual(Receipt.objects.count(), 1)
        self.assertEqual(Receipt.objects.get().lines.count(), 3)

    def test_receipt_leaves_reservations_unchanged(self):
        p = self.demandable_product(physical=10)
        self.assertEqual(self.send([p], [4]).status_code, 201)
        order = self.buy([p], [5])
        self.assertEqual(self.receive(order, [5]).status_code, 201)
        self.assertEqual(self.stock(p)[:3], (15, 4, 11))

    def test_empty_or_invalid_quantities_and_nc_kind_refused(self):
        p = self.demandable_product(physical=0)
        order = self.buy([p], [10])
        for c, nc, kind in [
            (0, 0, ""),
            (-1, 0, ""),
            (True, 0, ""),
            (1.5, 0, ""),
            (0, 2, ""),
            (0, 2, "INVENTED"),
        ]:
            body = self.receiving_body(order, [0])
            body["lines"][0].update(conforming=c, nonconforming=nc, nonconformity=kind)
            response = self.post_json(f"/api/v1/orders/{order.pk}/receipts", body, self.operator)
            self.assertIn(response.status_code, [400, 409], response.content)
        self.assertEqual(Receipt.objects.count(), 0)

    def test_uninventoried_regular_receipt_refused_but_nc_report_allowed(self):
        p = self.create_product()
        order = self.buy([p], [3])
        self.assertEqual(self.receive(order, [3]).status_code, 409)
        self.assertEqual(self.stock(p)[0], None)
        self.assertEqual(self.receive(order, [0], [3]).status_code, 201)
        self.assertEqual(self.stock(p)[0], None)
        self.assertEqual(order.lines.get().waiting, 3)

    def test_surplus_cannot_be_accepted_twice_or_partially(self):
        p = self.demandable_product(physical=0)
        order = self.buy([p], [20])
        self.receive(order, [23])
        item = QuarantineItem.objects.get()
        s = item_state(item.pk)
        response = self.post_json(
            f"/api/v1/quarantine/{item.pk}/accept-surplus",
            {
                "expected_version": s["version"],
                "quantity": 1,
                "verification": True,
                "expected_stock_version": self.stock(p)[3],
            },
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.decision(item, "accept-surplus").status_code, 200)
        self.assertEqual(self.decision(item, "accept-surplus").status_code, 400)
        self.assertEqual(self.stock(p)[0], 23)

    def test_refusal_stays_open_until_external_treatment_verified(self):
        p = self.demandable_product(physical=0)
        order = self.buy([p], [2])
        self.receive(order, [5])
        item = QuarantineItem.objects.get()
        self.assertEqual(self.decision(item, "refuse").status_code, 200)
        item.case.refresh_from_db()
        self.assertEqual(item.case.status, "UNDER_REVIEW")
        self.assertEqual(self.stock(p)[0], 2)
        body = {
            "expected_version": item.case.version,
            "item_versions": {str(item.pk): item_state(item.pk)["version"]},
            "external_verification": True,
        }
        response = self.post_json(f"/api/v1/cases/{item.case_id}/resolve", body)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(item_state(item.pk)["remaining"], 0)
        item.case.refresh_from_db()
        self.assertEqual(item.case.status, "RESOLVED")
        self.assertEqual(self.stock(p)[0], 2)

    def test_resolve_surplus_requires_decision_and_verification(self):
        p = self.demandable_product(physical=0)
        order = self.buy([p], [2])
        self.receive(order, [3])
        item = QuarantineItem.objects.get()
        body = {
            "expected_version": 0,
            "item_versions": {str(item.pk): 0},
            "external_verification": True,
        }
        self.assertEqual(
            self.post_json(f"/api/v1/cases/{item.case_id}/resolve", body).status_code, 409
        )
        self.assertEqual(item_state(item.pk)["remaining"], 1)

    def test_nonconformity_external_resolution_no_stock(self):
        p = self.demandable_product(physical=0)
        order = self.buy([p], [3])
        self.receive(order, [0], [3])
        item = QuarantineItem.objects.get()
        body = {
            "expected_version": 0,
            "item_versions": {str(item.pk): 0},
            "external_verification": True,
        }
        response = self.post_json(f"/api/v1/cases/{item.case_id}/resolve", body)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual((self.stock(p)[0], order.lines.get().waiting), (0, 3))

    def test_unknown_optional_identification_stays_outside_stock(self):
        p = self.demandable_product(physical=0)
        report = self.unknown(
            supplier_id=str(self.supplier.pk),
            lines=[{"product_id": str(p.pk), "quantity": 3}],
            message="Colis fictif",
        )
        self.assertEqual(report.lines.get().quantity, 3)
        self.assertEqual(self.stock(p)[0], 0)
        self.assertEqual(PurchaseOrder.objects.count(), 0)
        self.assertEqual(BusinessEvent.objects.filter(event_type="purchase.recorded").count(), 0)

    def test_unknown_link_is_unique_and_receipt_cannot_consume_twice(self):
        p = self.demandable_product(physical=0)
        order = self.buy([p], [10])
        report = self.unknown()
        self.link(report, order, [5])
        report.case.refresh_from_db()
        version = report.case.version
        response = self.receive(
            order, [5], unknown_delivery_id=str(report.pk), expected_source_version=version
        )
        self.assertEqual(response.status_code, 201, response.content)
        response = self.receive(
            order, [5], unknown_delivery_id=str(report.pk), expected_source_version=version
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.stock(p)[0], 5)
        report.case.refresh_from_db()
        body = {
            "order_id": str(order.pk),
            "expected_version": report.case.version,
            "verification": True,
            "lines": [{"order_line_id": str(order.lines.get().pk), "quantity": 5}],
        }
        self.assertEqual(
            self.post_json(f"/api/v1/cases/{report.case_id}/link-order", body).status_code, 409
        )

    def test_cancelled_quantities_block_receipt_but_valid_waiting_receivable(self):
        p = self.demandable_product(physical=0)
        order = self.buy([p], [10])
        self.cancel_remainder(order, 4)
        self.assertEqual(self.receive(order, [7]).status_code, 409)
        self.assertEqual(self.stock(p)[0], 0)
        self.assertEqual(QuarantineItem.objects.count(), 0)
        self.assertEqual(self.receive(order, [6]).status_code, 201)
        self.assertEqual((order.lines.get().received, order.lines.get().cancelled), (6, 4))
        self.assertEqual(self.receive(order, [4]).status_code, 409)

    def test_late_report_does_not_reinstate_or_create_purchase(self):
        p = self.demandable_product(physical=0)
        order = self.buy([p], [5])
        self.cancel_remainder(order, 5)
        order.refresh_from_db()
        response = self.post_json(
            f"/api/v1/orders/{order.pk}/late-delivery-reports",
            {
                "expected_version": order.version,
                "lines": [{"order_line_id": str(order.lines.get().pk), "observed_quantity": 3}],
            },
            self.operator,
        )
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual((order.lines.get().cancelled, self.stock(p)[0]), (5, 0))
        self.assertEqual(item_state(QuarantineItem.objects.get().pk)["remaining"], 3)

    def test_reminder_partial_only_no_order_or_stock_change(self):
        p = self.demandable_product(physical=0)
        order = self.buy([p], [10])
        self.assertEqual(
            self.post_json(
                f"/api/v1/orders/{order.pk}/reminders",
                {"expected_last_reminder": None},
                self.operator,
            ).status_code,
            409,
        )
        self.receive(order, [3])
        order.refresh_from_db()
        before = self.stock(p)
        response = self.post_json(
            f"/api/v1/orders/{order.pk}/reminders", {"expected_last_reminder": None}, self.operator
        )
        self.assertEqual(response.status_code, 200, response.content)
        previous = order.version
        order.refresh_from_db()
        self.assertEqual(order.version, previous)
        self.assertEqual(self.stock(p), before)
        self.assertEqual(
            self.post_json(
                f"/api/v1/orders/{order.pk}/reminders",
                {"expected_last_reminder": None},
                self.operator,
            ).status_code,
            409,
        )
        self.assertEqual(
            BusinessEvent.objects.filter(event_type="purchase.manual_reminder").count(), 1
        )

    def test_correction_chain_uses_effective_quantity_and_original_immutable(self):
        p = self.demandable_product(physical=0)
        order = self.buy([p], [10])
        self.receive(order, [10])
        receipt = Receipt.objects.get()
        case = self.receipt_case(receipt)
        for q in [8, 7, 9]:
            response = self.correct(receipt, case, [q])
            self.assertEqual(response.status_code, 200, response.content)
            self.assertEqual(self.stock(p)[0], q)
        self.assertEqual(receipt.lines.get().regular, 10)
        self.assertEqual(receipt.corrections.count(), 3)
        self.assertEqual(order.lines.get().received, 9)

    def test_correction_above_order_is_isolated_not_regular(self):
        p = self.demandable_product(physical=0)
        order = self.buy([p], [10])
        self.receive(order, [10])
        receipt = Receipt.objects.get()
        case = self.receipt_case(receipt)
        response = self.correct(receipt, case, [12])
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self.stock(p)[0], 10)
        self.assertEqual(order.lines.get().received, 10)
        self.assertEqual(item_state(QuarantineItem.objects.get().pk)["remaining"], 2)

    def test_correction_cannot_retract_accepted_surplus(self):
        p = self.demandable_product(physical=0)
        order = self.buy([p], [10])
        self.receive(order, [13])
        item = QuarantineItem.objects.get()
        self.assertEqual(self.decision(item, "accept-surplus").status_code, 200)
        receipt = Receipt.objects.get()
        case = self.receipt_case(receipt)
        preview = self.post_json(
            "/api/v1/corrections/preview", self.correction_body(receipt, case, [12])
        )
        self.assertEqual(preview.status_code, 409)
        self.assertEqual(preview.json()["error"]["code"], "ISOLATION_ALREADY_DECIDED")
        self.assertEqual(self.stock(p)[0], 13)

    def test_correction_below_reservations_persists_hold_without_false_success(self):
        from apps.cases.models import StockHold

        p = self.demandable_product(physical=0, maximum=10)
        order = self.buy([p], [10])
        self.receive(order, [10])
        self.assertEqual(self.send([p], [9]).status_code, 201)
        receipt = Receipt.objects.get()
        case = self.receipt_case(receipt)
        body = self.correction_body(receipt, case, [8])
        preview = self.post_json("/api/v1/corrections/preview", body)
        self.assertEqual(preview.status_code, 409)
        self.assertEqual(StockHold.objects.count(), 0)
        response = self.post_json("/api/v1/corrections/confirm", {**body, "preview_hash": "0" * 64})
        self.assertEqual(response.status_code, 409, response.content)
        self.assertEqual(self.stock(p)[:3], (10, 9, 1))
        self.assertEqual(receipt.corrections.count(), 0)
        self.assertEqual(StockHold.objects.filter(product=p).count(), 1)
        case.refresh_from_db()
        self.assertEqual(case.status, "UNDER_REVIEW")
        conflict = Case.objects.get(kind="STOCK_CONFLICT")
        self.assertEqual(self.admin_client.get(f"/api/v1/cases/{conflict.pk}").status_code, 200)
        self.assertContains(
            self.admin_client.get(f"/dossiers/{conflict.pk}"), f"/receptions/recus/{receipt.pk}"
        )

    def test_stale_correction_preview_cannot_confirm_after_new_receipt(self):
        p = self.demandable_product(physical=0)
        order = self.buy([p], [20])
        self.receive(order, [10])
        receipt = Receipt.objects.get()
        case = self.receipt_case(receipt)
        body = self.correction_body(receipt, case, [8])
        preview = self.post_json("/api/v1/corrections/preview", body)
        body["preview_hash"] = preview.json()["preview"]["preview_hash"]
        self.receive(order, [2])
        self.assertEqual(self.post_json("/api/v1/corrections/confirm", body).status_code, 409)
        self.assertEqual(self.stock(p)[0], 12)

    def test_receipt_permissions_and_private_histories(self):
        p = self.demandable_product(physical=0)
        order = self.buy([p], [3])
        body = self.receiving_body(order, [3])
        url = f"/api/v1/orders/{order.pk}/receipts"
        for client in [self.admin_client, self.user_client]:
            self.assertEqual(self.post_json(url, body, client).status_code, 403)
        self.assertEqual(self.user_client.get("/api/v1/receiving/orders").status_code, 403)
        self.assertEqual(self.receive(order, [3]).status_code, 201)
        receipt = Receipt.objects.get()
        self.assertEqual(self.user_client.get(f"/api/v1/receipts/{receipt.pk}").status_code, 403)
        case = self.receipt_case(receipt)
        body = self.correction_body(receipt, case, [2])
        for client in [self.operator, self.user_client]:
            self.assertEqual(
                self.post_json("/api/v1/corrections/preview", body, client).status_code, 403
            )
            self.assertEqual(
                self.post_json("/api/v1/corrections/confirm", body, client).status_code, 403
            )
        self.assertEqual(self.stock(p)[0], 3)

    def test_all_new_read_pages_render_and_forms_start_at_zero(self):
        p = self.demandable_product(physical=0)
        order = self.buy([p], [20])
        self.receive(order, [3])
        receipt = Receipt.objects.get()
        case = self.receipt_case(receipt)
        for client in [self.operator, self.admin_client]:
            for path in [
                "/receptions",
                "/receptions/historique",
                "/receptions/signalements",
                f"/receptions/commandes/{order.pk}",
                f"/receptions/recus/{receipt.pk}",
                f"/receptions/dossiers/{case.pk}",
            ]:
                r = client.get(path)
                self.assertEqual(r.status_code, 200, (path, r.content[:500]))
        r = self.operator.get(f"/receptions/commandes/{order.pk}/recevoir")
        self.assertContains(r, 'data-quantity="conforming"')
        self.assertContains(r, 'value="0"')
        self.assertNotContains(r, 'value="3"')
        self.assertContains(
            self.admin_client.get(f"/receptions/dossiers/{case.pk}"),
            "Vérification de recette fictive",
        )
        self.assertEqual(
            self.admin_client.get(f"/receptions/dossiers/{case.pk}/correction").status_code, 200
        )
        self.assertEqual(
            self.operator.get(f"/receptions/dossiers/{case.pk}/correction").status_code, 403
        )

    def test_database_fact_and_balance_immutable(self):
        from django.db import DatabaseError, transaction

        p = self.demandable_product(physical=0)
        order = self.buy([p], [3])
        self.receive(order, [3])
        receipt = Receipt.objects.get()
        with self.assertRaises(DatabaseError), transaction.atomic():
            receipt.lines.update(regular=2, conforming=2)
        with self.assertRaises(DatabaseError), transaction.atomic():
            receipt.delete()
        with self.assertRaises(DatabaseError), transaction.atomic():
            order.lines.update(received=2)
        self.assertEqual(self.stock(p)[0], 3)

    def test_read_only_integrity_commands(self):
        from io import StringIO

        from django.core.management import call_command

        p = self.demandable_product(physical=0)
        order = self.buy([p], [3])
        self.receive(order, [5])
        out = StringIO()
        call_command("check_receipts", stdout=out)
        call_command("check_purchase_orders", stdout=out)
        self.assertIn("M8 cohérent", out.getvalue())
        self.assertEqual(self.stock(p)[0], 3)


class ReceivingConcurrencyTests(ReceivingFixtures, TransactionTestCase):
    def race_receiving(self, items):
        import json
        import threading
        from concurrent.futures import ThreadPoolExecutor

        from django.db import close_old_connections, connection

        from apps.core.idempotency import execute_intent

        barrier = threading.Barrier(len(items), timeout=30)

        def run(item):
            close_old_connections()
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_backend_pid()")
                    pid = cursor.fetchone()[0]
                context, body, identifier, key = item
                barrier.wait()
                return pid, execute_intent(context, intent_id=identifier, key=key, payload=body)
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=len(items)) as pool:
            result = list(pool.map(run, items))
        path = Path("docs/recette/M8/concurrency.json")
        observations = json.loads(path.read_text()) if path.exists() else {}
        observations[self._testMethodName] = {
            "connections": len({pid for pid, _ in result}),
            "statuses": [o.status for _, o in result],
            "operations": len(
                {o.body.get("operation_id") for _, o in result if o.body.get("operation_id")}
            ),
        }
        path.write_text(json.dumps(observations, indent=2) + "\n")
        return [o for _, o in result]

    def test_twenty_simultaneous_confirmations_one_receipt(self):
        p = self.demandable_product(physical=0)
        order = self.buy([p], [10])
        body = self.receiving_body(order, [10])
        binding = self.intention("receipts.create", body, resource=order.pk)
        outcomes = self.race_receiving([(self.context, body, *binding)] * 20)
        self.assertEqual({o.status for o in outcomes}, {201})
        self.assertEqual(Receipt.objects.count(), 1)
        self.assertEqual(self.stock(p)[0], 10)

    def test_distinct_concurrent_receipts_cannot_credit_same_waiting_twice(self):
        p = self.demandable_product(physical=0)
        order = self.buy([p], [10])
        body = self.receiving_body(order, [10])
        outcomes = self.race_receiving(
            [
                (self.context, body, *self.intention("receipts.create", body, resource=order.pk))
                for _ in range(2)
            ]
        )
        self.assertEqual(sorted(o.status for o in outcomes), [201, 409])
        self.assertEqual(self.stock(p)[0], 10)

    def test_receipt_and_cancel_remainder_race_one_wins(self):
        p = self.demandable_product(physical=0)
        order = self.buy([p], [10])
        arrival = self.receiving_body(order, [10])
        cancel = {
            "expected_version": order.version,
            "external_verification": True,
            "lines": [{"order_line_id": str(order.lines.get().pk), "quantity": 10}],
        }
        outcomes = self.race_receiving(
            [
                (
                    self.context,
                    arrival,
                    *self.intention("receipts.create", arrival, resource=order.pk),
                ),
                (
                    self.admin_context,
                    cancel,
                    *self.intention(
                        "orders.cancel_remainder",
                        cancel,
                        resource=order.pk,
                        context=self.admin_context,
                    ),
                ),
            ]
        )
        self.assertEqual(
            sorted(o.status for o in outcomes), [200 if outcomes[1].status == 200 else 201, 409]
        )
        line = order.lines.get()
        self.assertEqual(line.quantity, line.received + line.cancelled + line.waiting)
        self.assertEqual(self.stock(p)[0], line.received)

    def test_lost_response_replay_identical_and_changed_body_rejected(self):
        import uuid

        p = self.demandable_product(physical=0)
        order = self.buy([p], [10])
        body = self.receiving_body(order, [10])
        identifier, key = uuid.uuid4(), uuid.uuid4().hex
        url = f"/api/v1/orders/{order.pk}/receipts"
        first = self.post_json(url, body, self.operator, identifier=identifier, key=key)
        self.assertEqual(first.status_code, 201)
        again = self.post_json(url, body, self.operator, identifier=identifier, key=key)
        self.assertEqual(first.json(), again.json())
        body["lines"][0]["conforming"] = 9
        self.assertEqual(
            self.post_json(url, body, self.operator, identifier=identifier, key=key).status_code,
            409,
        )
        self.assertEqual(Receipt.objects.count(), 1)
        self.assertEqual(self.stock(p)[0], 10)

    def test_outbox_failure_rolls_back_stock_receipt_and_audit(self):
        from unittest.mock import patch

        from apps.audit.models import AuditEvent
        from apps.notifications.models import Outbox

        p = self.demandable_product(physical=0)
        order = self.buy([p], [10])
        audit = AuditEvent.objects.count()
        with patch.object(
            Outbox.objects, "create", side_effect=RuntimeError("Échec fictif outbox")
        ):
            response = self.receive(order, [10])
        self.assertEqual(response.status_code, 500, response.content)
        self.assertEqual(Receipt.objects.count(), 0)
        self.assertEqual(self.stock(p)[0], 0)
        self.assertEqual(order.lines.get().received, 0)
        self.assertEqual(AuditEvent.objects.count(), audit)

    def test_stock_ledger_audit_and_outbox_share_one_confirmed_operation(self):
        from apps.audit.models import AuditEvent
        from apps.inventory.models import StockEntry
        from apps.notifications.models import Outbox

        p = self.demandable_product(physical=0)
        order = self.buy([p], [10])
        response = self.receive(order, [10])
        self.assertEqual(response.status_code, 201)
        receipt = Receipt.objects.get()
        op = receipt.event.operation
        self.assertEqual(op.intent.state, "CONFIRMED")
        self.assertEqual(StockEntry.objects.filter(operation=op, kind="RECEIPT_REGULAR").count(), 1)
        self.assertEqual(AuditEvent.objects.filter(operation=op).count(), 1)
        event = BusinessEvent.objects.get(operation=op, event_type="purchase.received")
        self.assertTrue(Outbox.objects.filter(event=event).exists())

    def test_correction_and_new_receipt_race_preserves_order_and_stock(self):
        p = self.demandable_product(physical=0)
        order = self.buy([p], [20])
        self.receive(order, [10])
        receipt = Receipt.objects.get()
        case = self.receipt_case(receipt)
        correction = self.correction_body(receipt, case, [8])
        preview = self.post_json("/api/v1/corrections/preview", correction)
        correction["preview_hash"] = preview.json()["preview"]["preview_hash"]
        arrival = self.receiving_body(order, [10])
        outcomes = self.race_receiving(
            [
                (
                    self.context,
                    arrival,
                    *self.intention("receipts.create", arrival, resource=order.pk),
                ),
                (
                    self.admin_context,
                    correction,
                    *self.intention(
                        "corrections.confirm",
                        correction,
                        resource=receipt.pk,
                        context=self.admin_context,
                    ),
                ),
            ]
        )
        self.assertEqual(sum(o.status < 300 for o in outcomes), 1)
        self.assertEqual(sum(o.status == 409 for o in outcomes), 1)
        line = order.lines.get()
        self.assertEqual(self.stock(p)[0], line.received)
        self.assertEqual(line.quantity, line.received + line.cancelled + line.waiting)

    def test_reinstatement_and_valid_receipt_race_rechecks_order_version(self):
        p = self.demandable_product(physical=0)
        order = self.buy([p], [10])
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
        reinstate = {
            "expected_version": order.version,
            "report_id": str(report.pk),
            "expected_report_version": 0,
            "external_verification": True,
            "lines": [{"order_line_id": str(line.pk), "quantity": 3}],
        }
        arrival = self.receiving_body(order, [5])
        outcomes = self.race_receiving(
            [
                (
                    self.context,
                    arrival,
                    *self.intention("receipts.create", arrival, resource=order.pk),
                ),
                (
                    self.admin_context,
                    reinstate,
                    *self.intention(
                        "orders.reinstate_remainder",
                        reinstate,
                        resource=order.pk,
                        context=self.admin_context,
                    ),
                ),
            ]
        )
        self.assertEqual(sum(o.status < 300 for o in outcomes), 1)
        self.assertEqual(sum(o.status == 409 for o in outcomes), 1)
        line.refresh_from_db()
        self.assertEqual(line.quantity, line.received + line.cancelled + line.waiting)
        self.assertEqual(self.stock(p)[0], line.received)


class ReceivingExceptionTests(ReceivingFixtures, TransactionTestCase):
    def late(self, order, q):
        order.refresh_from_db()
        response = self.post_json(
            f"/api/v1/orders/{order.pk}/late-delivery-reports",
            {
                "expected_version": order.version,
                "lines": [{"order_line_id": str(order.lines.get().pk), "observed_quantity": q}],
            },
            self.operator,
        )
        self.assertEqual(response.status_code, 201, response.content)
        return ArrivalReport.objects.get(pk=response.json()["resource"]["id"])

    def reinstate(self, order, report, q, client=None):
        order.refresh_from_db()
        report.case.refresh_from_db()
        return self.post_json(
            f"/api/v1/orders/{order.pk}/reinstate-remainder",
            {
                "expected_version": order.version,
                "report_id": str(report.pk),
                "expected_report_version": report.case.version,
                "external_verification": True,
                "lines": [{"order_line_id": str(order.lines.get().pk), "quantity": q}],
            },
            client,
        )

    def test_late_receipt_requires_grant_and_cannot_reuse_received_units(self):
        p = self.demandable_product(physical=0)
        order = self.buy([p], [10])
        self.cancel_remainder(order, 5)
        report = self.late(order, 3)
        response = self.receive(
            order, [3], late_delivery_report_id=str(report.pk), expected_source_version=0
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.reinstate(order, report, 2).status_code, 200)
        report.case.refresh_from_db()
        response = self.receive(
            order,
            [3],
            late_delivery_report_id=str(report.pk),
            expected_source_version=report.case.version,
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.stock(p)[0], 0)
        response = self.receive(
            order,
            [2],
            late_delivery_report_id=str(report.pk),
            expected_source_version=report.case.version,
        )
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(self.stock(p)[0], 2)
        self.assertEqual(self.reinstate(order, report, 2).status_code, 409)
        self.assertEqual(self.reinstate(order, report, 1).status_code, 200)

    def test_unknown_linked_to_cancelled_order_can_use_explicit_a07_without_new_parcel(self):
        p = self.demandable_product(physical=0)
        order = self.buy([p], [5])
        self.cancel_remainder(order, 5)
        report = self.unknown()
        self.link(report, order, [3])
        self.assertEqual(self.reinstate(order, report, 3).status_code, 200)
        self.assertEqual(self.stock(p)[0], 0)
        report.case.refresh_from_db()
        response = self.receive(
            order,
            [3],
            unknown_delivery_id=str(report.pk),
            expected_source_version=report.case.version,
        )
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual((self.stock(p)[0], order.lines.get().cancelled), (3, 2))
        self.assertEqual(ArrivalReport.objects.count(), 1)
        self.assertEqual(PurchaseOrder.objects.count(), 1)

    def test_reinstatement_n_only_and_separate_idempotent_fact(self):
        import uuid

        from apps.receiving.models import Reinstatement

        p = self.demandable_product(physical=0)
        order = self.buy([p], [5])
        self.cancel_remainder(order, 5)
        report = self.late(order, 3)
        self.assertEqual(self.reinstate(order, report, 3, self.operator).status_code, 403)
        self.assertEqual(self.reinstate(order, report, 3, self.user_client).status_code, 403)
        order.refresh_from_db()
        body = {
            "expected_version": order.version,
            "report_id": str(report.pk),
            "expected_report_version": 0,
            "external_verification": True,
            "lines": [{"order_line_id": str(order.lines.get().pk), "quantity": 3}],
        }
        identifier, key = uuid.uuid4(), uuid.uuid4().hex
        url = f"/api/v1/orders/{order.pk}/reinstate-remainder"
        first = self.post_json(url, body, identifier=identifier, key=key)
        self.assertEqual(first.status_code, 200, first.content)
        self.assertEqual(
            self.post_json(url, body, identifier=identifier, key=key).json(), first.json()
        )
        self.assertEqual(Reinstatement.objects.count(), 1)
        self.assertEqual(self.stock(p)[0], 0)
        self.assertEqual(Receipt.objects.count(), 0)

    def test_receipt_source_must_match_order_and_not_exceed_isolation(self):
        p = self.demandable_product(physical=0)
        order = self.buy([p], [10])
        report = self.unknown()
        self.link(report, order, [3])
        report.case.refresh_from_db()
        self.assertEqual(
            self.receive(
                order,
                [4],
                unknown_delivery_id=str(report.pk),
                expected_source_version=report.case.version,
            ).status_code,
            409,
        )
        other = self.buy([p], [5])
        self.assertEqual(
            self.receive(
                other,
                [3],
                unknown_delivery_id=str(report.pk),
                expected_source_version=report.case.version,
            ).status_code,
            409,
        )
        self.assertEqual(self.stock(p)[0], 0)

    def test_unknown_external_disposal_cannot_link_later(self):
        p = self.demandable_product(physical=0)
        order = self.buy([p], [3])
        report = self.unknown()
        response = self.post_json(
            f"/api/v1/cases/{report.case_id}/resolve",
            {"expected_version": 0, "item_versions": {}, "external_verification": True},
        )
        self.assertEqual(response.status_code, 200, response.content)
        report.case.refresh_from_db()
        body = {
            "order_id": str(order.pk),
            "expected_version": report.case.version,
            "verification": True,
            "lines": [{"order_line_id": str(order.lines.get().pk), "quantity": 3}],
        }
        self.assertEqual(
            self.post_json(f"/api/v1/cases/{report.case_id}/link-order", body).status_code, 409
        )
        self.assertEqual(self.stock(p)[0], 0)

    def test_regular_receipt_error_report_cannot_edit_original_and_is_unique(self):
        p = self.demandable_product(physical=0)
        order = self.buy([p], [3])
        self.receive(order, [3])
        receipt = Receipt.objects.get()
        case = self.receipt_case(receipt)
        self.assertEqual(
            self.post_json(f"/api/v1/receipts/{receipt.pk}/reports", {}, self.operator).status_code,
            409,
        )
        self.assertEqual(self.stock(p)[0], 3)
        self.assertEqual(receipt.lines.get().conforming, 3)
        self.assertEqual(case.kind, "RECEIPT_ERROR")
        self.assertEqual(self.operator.get(f"/receptions/dossiers/{case.pk}").status_code, 200)

    def test_correction_returns_units_to_source_isolation_not_a_second_stock(self):
        p = self.demandable_product(physical=0)
        order = self.buy([p], [10])
        report = self.unknown()
        self.link(report, order, [5])
        report.case.refresh_from_db()
        response = self.receive(
            order,
            [5],
            unknown_delivery_id=str(report.pk),
            expected_source_version=report.case.version,
        )
        self.assertEqual(response.status_code, 201, response.content)
        receipt = Receipt.objects.get()
        case = self.receipt_case(receipt)
        body = self.correction_body(receipt, case, [3])
        preview = self.post_json("/api/v1/corrections/preview", body)
        self.assertEqual(preview.status_code, 200, preview.content)
        response = self.post_json(
            "/api/v1/corrections/confirm",
            {**body, "preview_hash": preview.json()["preview"]["preview_hash"]},
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self.stock(p)[0], 3)
        self.assertEqual(item_state(QuarantineItem.objects.get().pk)["remaining"], 2)
        report.case.refresh_from_db()
        response = self.receive(
            order,
            [2],
            unknown_delivery_id=str(report.pk),
            expected_source_version=report.case.version,
        )
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(self.stock(p)[0], 5)

    def test_read_scopes_hide_money_and_forbid_assistant_and_foreign_receipt(self):

        from apps.accounts.models import User
        from tests.test_accounts import FICTIVE_PASSWORD

        p = self.demandable_product(physical=0)
        order = self.buy([p], [5], amount="50.00", currency="EUR")
        self.receive(order, [5])
        receipt = Receipt.objects.get()
        response = self.operator.get(f"/api/v1/receiving/orders/{order.pk}")
        self.assertNotIn("amount", response.json()["order"])
        other = User.objects.create_user(
            username="autre-recette-m8",
            password=FICTIVE_PASSWORD,
            role="REPLENISHER",
            display_name="Autre fictif",
            must_change_password=False,
        )
        client = self.logged(other)
        self.assertEqual(client.get(f"/api/v1/receipts/{receipt.pk}").status_code, 404)
        for url in [
            "/receptions",
            "/receptions/commande-introuvable",
            f"/receptions/commandes/{order.pk}",
        ]:
            self.assertEqual(self.user_client.get(url).status_code, 403)

    def test_historical_order_photo_authorized_without_global_catalog_permission(self):
        p = self.demandable_product(physical=0)
        order = self.buy([p], [3])
        line = order.lines.get()
        url = f"/api/v1/receiving/orders/{order.pk}/lines/{line.pk}/photo?thumbnail=1"
        response = self.operator.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "image/jpeg")
        response.close()
        self.assertEqual(self.user_client.get(url).status_code, 403)
        self.assertEqual(
            self.operator.get(
                "/api/v1/search-suggestions", {"scope": "receiving-orders", "q": order.reference}
            ).status_code,
            200,
        )

    def test_reminder_replay_is_exactly_once(self):
        import uuid

        from apps.receiving.models import ManualReminder

        p = self.demandable_product(physical=0)
        order = self.buy([p], [3])
        self.receive(order, [1])
        identifier, key = uuid.uuid4(), uuid.uuid4().hex
        body = {"expected_last_reminder": None}
        url = f"/api/v1/orders/{order.pk}/reminders"
        first = self.post_json(url, body, self.operator, identifier=identifier, key=key)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(
            self.post_json(url, body, self.operator, identifier=identifier, key=key).json(),
            first.json(),
        )
        self.assertEqual(ManualReminder.objects.count(), 1)

    def test_admin_open_error_and_review_requires_explicit_verification(self):
        p = self.demandable_product(physical=0)
        order = self.buy([p], [3])
        self.receive(order, [3])
        receipt = Receipt.objects.get()
        response = self.post_json(
            "/api/v1/cases",
            {
                "kind": "RECEIPT_ERROR",
                "source_id": str(receipt.pk),
                "verification": True,
                "message": "Comptage de contrôle fictif",
            },
        )
        self.assertEqual(response.status_code, 201, response.content)
        case = Case.objects.get(kind="RECEIPT_ERROR")
        response = self.post_json(
            f"/api/v1/cases/{case.pk}/review",
            {"expected_version": 0, "verification": True, "message": "Vérification en cours"},
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self.admin_client.get(f"/dossiers/{case.pk}").status_code, 302)

    def test_restricted_runtime_role_receives_links_decides_and_corrects_without_delete(self):
        import os

        from django.db import DatabaseError, connection, transaction

        from apps.core.management.commands.grant_runtime import grant_runtime

        p = self.demandable_product(physical=0)
        order = self.buy([p], [10])
        grant_runtime(os.environ["RUNTIME_DATABASE_USER"])
        original = connection.settings_dict.copy()
        connection.close()
        connection.settings_dict.update(
            USER=os.environ["RUNTIME_DATABASE_USER"],
            PASSWORD=os.environ["RUNTIME_DATABASE_PASSWORD"],
        )
        try:
            report = self.unknown()
            self.link(report, order, [12])
            report.case.refresh_from_db()
            response = self.receive(
                order,
                [12],
                unknown_delivery_id=str(report.pk),
                expected_source_version=report.case.version,
            )
            self.assertEqual(response.status_code, 201, response.content)
            item = QuarantineItem.objects.get(kind="SURPLUS")
            s = item_state(item.pk)
            response = self.post_json(
                f"/api/v1/quarantine/{item.pk}/accept-surplus",
                {
                    "expected_version": s["version"],
                    "quantity": 2,
                    "verification": True,
                    "expected_stock_version": self.stock(p)[3],
                },
            )
            self.assertEqual(response.status_code, 200, response.content)
            receipt = Receipt.objects.get()
            case = self.receipt_case(receipt)
            body = self.correction_body(receipt, case, [12])
            preview = self.post_json("/api/v1/corrections/preview", body)
            self.assertEqual(preview.status_code, 200, preview.content)
            response = self.post_json(
                "/api/v1/corrections/confirm",
                {**body, "preview_hash": preview.json()["preview"]["preview_hash"]},
            )
            self.assertEqual(response.status_code, 200, response.content)
            # 12 unités seulement identifiées dans le colis : aucune unité inventée.
            extra = self.correction_body(receipt, case, [13])
            self.assertEqual(self.post_json("/api/v1/corrections/preview", extra).status_code, 409)
            for query in [
                "DELETE FROM receiving_receipt",
                "UPDATE receiving_receiptline SET conforming=0",
                "UPDATE receiving_arrivalreport SET id=id",
                "UPDATE receiving_quarantineitem SET id=id",
            ]:
                with (
                    self.assertRaises(DatabaseError),
                    transaction.atomic(),
                    connection.cursor() as cursor,
                ):
                    cursor.execute(query)
        finally:
            connection.close()
            connection.settings_dict.update(original)
