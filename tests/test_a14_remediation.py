"""Remédiation A14 : verrous compatibles, atomisme et lectures des reçus bornées."""

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from django.db import close_old_connections, connection
from django.test import TransactionTestCase
from django.test.utils import CaptureQueriesContext

from apps.accounts.models import User
from apps.catalog.models import Floor
from apps.core import idempotency
from apps.core.management.commands.grant_runtime import grant_runtime
from apps.core.policies import ActorContext
from apps.inventory.models import StockEntry
from apps.receiving import selectors
from apps.receiving.models import Receipt
from apps.requests import services
from apps.requests.models import InternalRequest, RequestLine
from tests.test_accounts import FICTIVE_PASSWORD
from tests.test_receiving import ReceivingFixtures


class RequestLockTests(ReceivingFixtures, TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.doctor = User.objects.create_user(
            "medecin-remediation",
            FICTIVE_PASSWORD,
            display_name="Demandeur B fictif",
            role="DOCTOR",
            floor=self.floor,
            must_change_password=False,
        )
        self.doctor_client = self.logged(self.doctor)
        grant_runtime(os.environ["RUNTIME_DATABASE_USER"])

    def intent(self, actor, products, quantities):
        body = {
            "lines": [
                {"product_id": str(p.pk), "quantity": q}
                for p, q in zip(products, quantities, strict=True)
            ]
        }
        context = ActorContext(actor.pk, actor.auth_version)
        identifier, key = self.intention("requests.create", body, context=context)
        return context, identifier, key, body

    def execute(self, item):
        close_old_connections()
        old = connection.settings_dict
        connection.settings_dict = dict(old)
        try:
            connection.settings_dict.update(
                USER=os.environ["RUNTIME_DATABASE_USER"],
                PASSWORD=os.environ["RUNTIME_DATABASE_PASSWORD"],
            )
            context, identifier, key, body = item
            return idempotency.execute_intent(context, intent_id=identifier, key=key, payload=body)
        finally:
            connection.close()
            connection.settings_dict = old

    def concurrent(self, items):
        barrier = threading.Barrier(len(items), timeout=10)

        def run(item):
            barrier.wait()
            return self.execute(item)

        with ThreadPoolExecutor(max_workers=len(items)) as pool:
            return list(pool.map(run, items))

    def test_independent_products_same_floor_can_commit_before_other_request_releases_locks(self):
        products = [self.demandable_product(code=f"INDEP-{i}") for i in range(2)]
        held, release = threading.Event(), threading.Event()
        original = services.append_change

        def pause(execution, *a, **kw):
            result = original(execution, *a, **kw)
            if execution.actor.pk == self.assistant.pk:
                held.set()
                if not release.wait(8):
                    raise TimeoutError("barrière de test")
            return result

        first = self.intent(self.assistant, [products[0]], [2])
        second = self.intent(self.doctor, [products[1]], [2])
        with (
            patch.object(services, "append_change", side_effect=pause),
            ThreadPoolExecutor(max_workers=2) as pool,
        ):
            a = pool.submit(self.execute, first)
            self.assertTrue(held.wait(8))
            b = pool.submit(self.execute, second)
            try:
                self.assertEqual(b.result(timeout=4).status, 201)
            finally:
                release.set()
            self.assertEqual(a.result(timeout=8).status, 201)
        self.assertEqual([self.stock(p)[:3] for p in products], [(10, 2, 8), (10, 2, 8)])

    def test_same_product_sufficient_stock_both_roles_commit_once(self):
        p = self.demandable_product(physical=4)
        outcomes = self.concurrent(
            [self.intent(u, [p], [2]) for u in [self.assistant, self.doctor]]
        )
        self.assertEqual([r.status for r in outcomes], [201, 201])
        self.assertEqual(self.stock(p)[:3], (4, 4, 0))
        self.assertEqual(
            set(InternalRequest.objects.values_list("creation_operation__actor_role", flat=True)),
            {"ASSISTANT", "DOCTOR"},
        )

    def test_same_product_insufficient_stock_is_business_refusal_without_overreservation(self):
        p = self.demandable_product(physical=3)
        outcomes = self.concurrent(
            [self.intent(u, [p], [2]) for u in [self.assistant, self.doctor]]
        )
        self.assertEqual(sorted(r.status for r in outcomes), [201, 409])
        self.assertEqual(
            next(r.body["error"]["code"] for r in outcomes if r.status == 409), "REQUEST_REFUSED"
        )
        self.assertEqual(self.stock(p)[:3], (3, 2, 1))
        self.assertEqual(InternalRequest.objects.count(), 1)

    def test_reverse_multiline_input_uses_stable_lock_order_and_atomic_reservations(self):
        products = [self.demandable_product(code=f"MULTI-{i}", physical=4) for i in range(3)]
        outcomes = self.concurrent(
            [
                self.intent(self.assistant, products, [2] * 3),
                self.intent(self.doctor, products[::-1], [2] * 3),
            ]
        )
        self.assertEqual([r.status for r in outcomes], [201, 201])
        self.assertEqual([self.stock(p)[:3] for p in products], [(4, 4, 0)] * 3)
        self.assertEqual(StockEntry.objects.filter(kind="RESERVE").count(), 6)

    def test_refused_multiline_request_leaves_no_partial_reservation(self):
        products = [
            self.demandable_product(code=f"ATOM-{i}", physical=q) for i, q in enumerate([10, 1])
        ]
        result = self.execute(self.intent(self.doctor, products, [2, 2]))
        self.assertEqual(result.status, 409)
        self.assertFalse(InternalRequest.objects.exists())
        self.assertEqual([self.stock(p)[1] for p in products], [0, 0])
        self.assertFalse(StockEntry.objects.filter(kind="RESERVE").exists())

    def test_lost_response_and_concurrent_replay_recover_same_intent(self):
        p = self.demandable_product()
        item = self.intent(self.doctor, [p], [2])
        first = self.execute(item)  # Réponse volontairement non exploitée par le client.
        outcomes = self.concurrent([item, item])
        self.assertTrue(all(r.replayed for r in outcomes))
        self.assertTrue(all(r.body == first.body for r in outcomes))
        self.assertEqual(InternalRequest.objects.count(), 1)
        self.assertEqual(StockEntry.objects.filter(kind="RESERVE").count(), 1)
        self.assertEqual(self.stock(p)[:3], (10, 2, 8))

    def paused_change(self, action):
        p = self.demandable_product()
        item = self.intent(self.doctor, [p], [2])
        held, release, done = threading.Event(), threading.Event(), threading.Event()
        original = services.append_change

        def pause(*a, **kw):
            result = original(*a, **kw)
            held.set()
            if not release.wait(8):
                raise TimeoutError("barrière de test")
            return result

        def change():
            close_old_connections()
            try:
                response = action(p)
                done.set()
                return response
            finally:
                connection.close()

        with (
            patch.object(services, "append_change", side_effect=pause),
            ThreadPoolExecutor(max_workers=2) as pool,
        ):
            first = pool.submit(self.execute, item)
            self.assertTrue(held.wait(8))
            second = pool.submit(change)
            try:
                self.assertFalse(done.wait(0.25))
            finally:
                release.set()
            self.assertEqual(first.result(timeout=8).status, 201)
            response = second.result(timeout=8)
            self.assertEqual(response.status_code, 200, response.content)
        return p

    def test_floor_deactivation_waits_for_request_then_blocks_new_request(self):
        self.floor.refresh_from_db()
        p = self.paused_change(
            lambda _: self.post_json(
                f"/api/v1/floors/{self.floor.pk}",
                {"expected_version": self.floor.version, "active": False},
                method="patch",
            )
        )
        self.assertEqual(self.send([p], [1], client=self.doctor_client).status_code, 409)
        self.assertEqual(self.stock(p)[1], 2)

    def test_floor_rename_waits_and_historical_request_retains_original_floor_name(self):
        old_name = self.floor.name
        self.floor.refresh_from_db()
        self.paused_change(
            lambda _: self.post_json(
                f"/api/v1/floors/{self.floor.pk}",
                {"expected_version": self.floor.version, "name": "Nouvel unité fictif"},
                method="patch",
            )
        )
        self.assertEqual(InternalRequest.objects.get().floor_name, old_name)
        self.assertEqual(Floor.objects.get(pk=self.floor.pk).name, "Nouvel unité fictif")

    def test_permission_removal_waits_then_applies_to_subsequent_request(self):
        p = self.paused_change(lambda product: self.rights(product, []))
        self.assertEqual(self.send([p], [1], client=self.doctor_client).status_code, 409)
        self.assertEqual(RequestLine.objects.get().quantity, 2)

    def test_account_revocation_waits_then_revokes_doctor_without_rewriting_request(self):
        self.paused_change(lambda _: self.update(self.doctor, {"is_active": False}))
        response = self.doctor_client.get("/api/v1/requests")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(InternalRequest.objects.get().creation_operation.actor_role, "DOCTOR")

    def test_committed_permission_removal_is_seen_before_reserving(self):
        p = self.demandable_product()
        self.rights(p, [])
        self.assertEqual(self.execute(self.intent(self.doctor, [p], [1])).status, 409)
        self.assertEqual(self.stock(p)[1], 0)

    def test_reassignment_waits_and_preserves_original_request_floor(self):
        original_floor = self.floor.pk
        self.paused_change(
            lambda _: self.update(self.doctor, {"floor_id": str(self.other_floor.pk)})
        )
        self.doctor.refresh_from_db()
        self.assertEqual(self.doctor.floor_id, self.other_floor.pk)
        self.assertEqual(InternalRequest.objects.get().floor_id, original_floor)
        self.assertEqual(self.doctor_client.get("/api/v1/requests").status_code, 401)

    def test_concurrent_cancel_and_new_request_keep_reservations_consistent(self):
        p = self.demandable_product()
        sent = self.send([p], [2])
        old = InternalRequest.objects.get(pk=sent.json()["resource"]["id"])
        new = self.intent(self.doctor, [p], [1])
        with ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(self.execute, new)

            def cancel():
                close_old_connections()
                try:
                    return self.cancel_request(old).status_code
                finally:
                    connection.close()

            b = pool.submit(cancel)
            self.assertEqual(a.result(timeout=10).status, 201)
            self.assertEqual(b.result(timeout=10), 200)
        self.assertEqual(self.stock(p)[:3], (10, 1, 9))

    def test_request_competing_with_receipt_preserves_versions_and_order_equation(self):
        p = self.demandable_product(physical=10)
        order = self.buy([p], [20])
        body = self.receiving_body(order, [3])
        identifier, key = self.intention("receipts.create", body, resource=order.pk)
        outcomes = self.concurrent(
            [self.intent(self.doctor, [p], [2]), (self.context, identifier, key, body)]
        )
        self.assertEqual(outcomes[0].status, 201)
        self.assertIn(outcomes[1].status, [201, 409])
        if outcomes[1].status == 409:
            self.assertEqual(outcomes[1].body["error"]["code"], "STALE_VERSION")
        accepted = 3 if outcomes[1].status == 201 else 0
        self.assertEqual(self.stock(p)[:3], (10 + accepted, 2, 8 + accepted))
        line = order.lines.get()
        self.assertEqual(
            (line.received, line.cancelled, line.waiting), (accepted, 0, 20 - accepted)
        )

    def test_request_competing_with_delivery_has_one_physical_exit_and_correct_reservations(self):
        from tests.test_deliveries import DeliveryFixtures

        p = self.demandable_product(physical=10)
        response = self.send([p], [3])
        request = InternalRequest.objects.get(pk=response.json()["resource"]["id"])
        self.assertEqual(DeliveryFixtures.claim(self, request).status_code, 200)
        request.refresh_from_db()
        body = DeliveryFixtures.delivery_payload(self, request)
        identifier, key = self.intention("requests.deliver", body, resource=request.pk)
        outcomes = self.concurrent(
            [self.intent(self.doctor, [p], [1]), (self.context, identifier, key, body)]
        )
        self.assertEqual([r.status for r in outcomes], [201, 201])
        self.assertEqual(self.stock(p)[:3], (7, 1, 6))
        self.assertEqual(StockEntry.objects.filter(kind="DELIVER").count(), 1)

    def test_request_competing_with_adjustment_preserves_atomic_version_check(self):
        p = self.demandable_product(physical=10)
        body = self.adjustment_payload([p], [12])
        identifier, key = self.intention("inventory.adjust", body)
        outcomes = self.concurrent(
            [self.intent(self.doctor, [p], [2]), (self.context, identifier, key, body)]
        )
        self.assertEqual(outcomes[0].status, 201)
        self.assertIn(outcomes[1].status, [200, 409])
        if outcomes[1].status == 409:
            self.assertEqual(outcomes[1].body["error"]["code"], "STALE_VERSION")
        physical = 12 if outcomes[1].status == 200 else 10
        self.assertEqual(self.stock(p)[:3], (physical, 2, physical - 2))

    def test_admin_floor_change_committing_first_is_seen_after_lock_wait(self):
        from apps.catalog import services as catalog

        p = self.demandable_product()
        item = self.intent(self.doctor, [p], [1])
        held, release, attempting = threading.Event(), threading.Event(), threading.Event()
        original = catalog.stamped_save

        def pause(instance, *a, **kw):
            result = original(instance, *a, **kw)
            if isinstance(instance, Floor):
                held.set()
                if not release.wait(8):
                    raise TimeoutError("barrière de test")
            return result

        def change():
            close_old_connections()
            try:
                return self.post_json(
                    f"/api/v1/floors/{self.floor.pk}",
                    {"expected_version": self.floor.version, "active": False},
                    method="patch",
                ).status_code
            finally:
                connection.close()

        def request():
            attempting.set()
            return self.execute(item)

        with (
            patch.object(catalog, "stamped_save", side_effect=pause),
            ThreadPoolExecutor(max_workers=2) as pool,
        ):
            a = pool.submit(change)
            self.assertTrue(held.wait(8))
            b = pool.submit(request)
            self.assertTrue(attempting.wait(8))
            try:
                self.assertFalse(b.done())
            finally:
                release.set()
            self.assertEqual(a.result(timeout=8), 200)
            self.assertEqual(b.result(timeout=8).status, 409)
        self.assertEqual(self.stock(p)[1], 0)
        self.assertFalse(InternalRequest.objects.exists())


class ReceiptReadTests(ReceivingFixtures, TransactionTestCase):
    def arrival(self, code="RECU", quantities=(2, 3), nonconforming=None):
        products = [self.demandable_product(code=f"{code}-{i}") for i in range(len(quantities))]
        order = self.buy(products, [20] * len(products))
        response = self.receive(order, list(quantities), nonconforming)
        self.assertEqual(response.status_code, 201, response.content)
        return Receipt.objects.get(pk=response.json()["resource"]["id"])

    def test_receipt_list_data_exactly_matches_unprefetched_reference(self):
        receipt = self.arrival()
        reference = selectors.receipt_data(
            selectors.receipts(self.admin).prefetch_related(None).get(pk=receipt.pk)
        )
        with self.assertNumQueries(4):
            actual = selectors.receipt_data(selectors.get_receipt(self.admin, receipt.pk))
        self.assertEqual(actual, reference)

    def test_query_count_does_not_grow_with_receipt_count_or_line_count(self):
        self.arrival("UN")

        def read():
            with CaptureQueriesContext(connection) as queries:
                result = [selectors.receipt_data(r) for r in selectors.receipts(self.admin)[:50]]
            return result, len(queries)

        one, n1 = read()
        self.arrival("DEUX", (1, 2, 3, 4, 5))
        many, n2 = read()
        self.assertEqual((len(one), len(many)), (1, 2))
        self.assertEqual((n1, n2), (4, 4))

    def test_successive_corrections_keep_initial_fact_and_latest_effective_quantity(self):
        receipt = self.arrival(quantities=(10,))
        case = self.receipt_case(receipt)
        for quantity in [8, 9]:
            body = self.correction_body(receipt, case, [quantity])
            preview = self.post_json("/api/v1/corrections/preview", body)
            self.assertEqual(preview.status_code, 200, preview.content)
            body["preview_hash"] = preview.json()["preview"]["preview_hash"]
            response = self.post_json("/api/v1/corrections/confirm", body)
            self.assertEqual(response.status_code, 200, response.content)
        unprefetched = selectors.receipt_data(
            selectors.receipts(self.admin).prefetch_related(None).get(pk=receipt.pk)
        )
        actual = selectors.receipt_data(selectors.get_receipt(self.admin, receipt.pk))
        self.assertEqual(actual, unprefetched)
        self.assertEqual(actual["lines"][0]["initial"]["regular"], 10)
        self.assertEqual(actual["lines"][0]["effective"]["regular"], 9)
        self.assertEqual([c["revision"] for c in actual["corrections"]], [1, 2])

    def test_surplus_nonconformity_and_units_preserved_in_reading(self):
        receipt = self.arrival(quantities=(23,), nonconforming=[2])
        row = selectors.receipt_data(selectors.get_receipt(self.admin, receipt.pk))
        self.assertEqual(row["lines"][0]["effective"]["regular"], 20)
        self.assertEqual(row["lines"][0]["effective"]["surplus"], 3)
        self.assertEqual(row["lines"][0]["effective"]["nonconforming"], 2)
        self.assertEqual(row["lines"][0]["unit"], "carton")

    def test_pagination_is_sql_bounded_and_has_deterministic_secondary_order(self):
        self.arrival("PAGE1")
        self.arrival("PAGE2")
        with CaptureQueriesContext(connection) as queries:
            first = list(selectors.receipts(self.admin)[:1])
            second = list(selectors.receipts(self.admin)[1:2])
        self.assertNotEqual(first[0].pk, second[0].pk)
        main = [q["sql"] for q in queries if 'FROM "receiving_receipt"' in q["sql"]]
        self.assertEqual(len(main), 2)
        self.assertTrue(all("LIMIT 1" in q for q in main))
        self.assertIn("OFFSET 1", main[1])

    def test_receipt_list_remains_restricted_to_own_operator_and_admin(self):
        receipt = self.arrival()
        other = User.objects.create_user(
            "autre-reap-recu", FICTIVE_PASSWORD, role="REPLENISHER", must_change_password=False
        )
        self.assertFalse(selectors.receipts(other).exists())
        self.assertEqual(selectors.receipts(self.replenisher).get().pk, receipt.pk)
        self.assertEqual(self.user_client.get("/receptions/historique").status_code, 403)

    def test_render_has_same_quantities_corrections_and_link_without_stock_mutation(self):
        receipt = self.arrival()
        before = list(StockEntry.objects.values_list("pk", flat=True))
        result = self.admin_client.get("/receptions/historique")
        self.assertContains(result, receipt.reference)
        self.assertContains(result, f"/receptions/recus/{receipt.pk}")
        self.assertContains(result, "Voir le reçu")
        self.assertEqual(list(StockEntry.objects.values_list("pk", flat=True)), before)
