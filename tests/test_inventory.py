"""Régressions M3 : depuis M4, les réservations fictives passent par de vraies demandes."""

import io
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from django.core.management import call_command
from django.db import DatabaseError, IntegrityError, close_old_connections, connection, transaction
from django.test import TransactionTestCase

from apps.audit.models import AuditEvent
from apps.cases.models import StockCase, StockHold
from apps.core import commands
from apps.core.idempotency import execute_intent, register_intent, resolve_intent
from apps.core.models import Operation, OperationIntent
from apps.core.payloads import payload_hash
from apps.core.policies import ActorContext
from apps.inventory import services
from apps.inventory.models import InventoryBatch, StockBalance, StockEntry
from apps.inventory.services import ensure_registered
from apps.notifications.models import BusinessEvent, Outbox
from tests.probes.inventory import COMMAND as RESERVATION
from tests.test_catalog import CatalogFixtures


class InventoryFixtures(CatalogFixtures):
    def setUp(self):
        super().setUp()
        ensure_registered()
        self.operator = self.logged(self.replenisher)
        commands._registry[RESERVATION.name] = RESERVATION
        self.context = ActorContext(self.replenisher.pk, self.replenisher.auth_version)

    def tearDown(self):
        commands._registry.pop(RESERVATION.name, None)
        super().tearDown()

    def activate(self, product):
        product.refresh_from_db()
        self.assertEqual(self.upload(product).status_code, 200)
        product.refresh_from_db()
        response = self.post_json(
            f"/api/v1/products/{product.pk}/activate",
            {
                "expected_version": product.version,
                "inventory_verified": True,
                "photo_verified": True,
            },
        )
        self.assertEqual(response.status_code, 200, response.content)
        product.refresh_from_db()

    def reserve(self, product, delta, *, version=None):
        # M4 impose R=sum(U) : les anciennes fixtures techniques utilisent
        # désormais de vrais engagements, jamais un solde parallèle.
        from apps.catalog.models import FloorProduct
        from apps.core.idempotency import Outcome
        from apps.requests.models import InternalRequest
        from apps.requests.services import ensure_registered as register_requests

        register_requests()
        if version is not None and version != StockBalance.objects.get(product=product).version:
            raise ValueError("Fixture de version obsolète")
        if delta > 0:
            if not FloorProduct.objects.filter(
                floor=self.floor, product=product, demandable=True
            ).exists():
                product.refresh_from_db()
                response = self.rights(
                    product,
                    [
                        {
                            "floor_id": str(self.floor.pk),
                            "visible": True,
                            "demandable": True,
                            "maximum": 100,
                        }
                    ],
                )
                self.assertEqual(response.status_code, 200, response.content)
            response = self.post_json(
                "/api/v1/requests",
                {"lines": [{"product_id": str(product.pk), "quantity": delta}]},
                self.user_client,
            )
        else:
            request = InternalRequest.objects.filter(
                assistant=self.assistant,
                status="NEW",
                lines__product=product,
                lines__quantity=-delta,
            ).first()
            if request is None:
                raise ValueError("La fixture M4 libère une demande entière non livrée.")
            response = self.post_json(
                f"/api/v1/requests/{request.pk}/cancel",
                {"expected_version": request.version},
                self.user_client,
            )
        return Outcome(
            response.json(), 200 if response.status_code == 201 else response.status_code
        )

    def intention(self, command, payload, *, resource=None, context=None):
        identifier, key = uuid.uuid4(), uuid.uuid4().hex
        register_intent(
            context or self.context,
            intent_id=identifier,
            key=key,
            fingerprint=payload_hash(payload),
            command_type=command,
            resource_id=resource,
        )
        return identifier, key

    def run_operation(self, command, payload, *, resource=None, context=None):
        identifier, key = self.intention(command, payload, resource=resource, context=context)
        return execute_intent(
            context or self.context, intent_id=identifier, key=key, payload=payload
        )

    def adjustment_payload(self, products, quantities):
        return {
            "lines": [
                {
                    "product_id": str(p.pk),
                    "expected_stock_version": StockBalance.objects.get(product=p).version,
                    "target_physical": q,
                }
                for p, q in zip(products, quantities, strict=True)
            ]
        }

    def stock(self, product):
        row = StockBalance.objects.get(product=product)
        return row.physical, row.reserved, row.available, row.version

    def reserved_product(self):
        product = self.create_product()
        self.assertEqual(self.initial([product]).status_code, 201)
        self.activate(product)
        self.assertEqual(self.reserve(product, 4).status, 200)
        return product

    def initial(self, products, quantities=None, *, client=None, identifier=None, key=None):
        quantities = quantities or [10] * len(products)
        lines = []
        for product, quantity in zip(products, quantities, strict=True):
            product.refresh_from_db()
            lines.append(
                {
                    "product_id": str(product.pk),
                    "expected_stock_version": product.stock.version,
                    "expected_product_version": product.version,
                    "counted_quantity": quantity,
                }
            )
        return self.post_json(
            "/api/v1/inventories/initial",
            {"lines": lines},
            client or self.operator,
            identifier=identifier,
            key=key,
        )

    def adjust(
        self, products, quantities, *, client=None, identifier=None, key=None, versions=None
    ):
        lines = []
        for index, (product, quantity) in enumerate(zip(products, quantities, strict=True)):
            balance = StockBalance.objects.get(product=product)
            lines.append(
                {
                    "product_id": str(product.pk),
                    "expected_stock_version": versions[index] if versions else balance.version,
                    "target_physical": quantity,
                }
            )
        return self.post_json(
            "/api/v1/stocks/adjustments",
            {"lines": lines},
            client or self.operator,
            identifier=identifier,
            key=key,
        )


class InventoryTests(InventoryFixtures, TransactionTestCase):
    def test_initial_real_count_and_zero_are_distinct_from_uncounted(self):
        products = [self.create_product(code=f"COMPTE-{i}") for i in range(3)]
        response = self.initial(products[:2], [8, 0])
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(
            [StockBalance.objects.get(product=p).physical for p in products], [8, 0, None]
        )
        self.assertEqual(StockEntry.objects.count(), 2)
        for product in products[:2]:
            product.refresh_from_db()
            self.assertIsNotNone(product.counted_at)
            self.assertEqual(product.status, "COUNTED_PENDING_ACTIVATION")
        self.assertEqual(self.initial(products[:1], [3]).status_code, 409)
        products[0].refresh_from_db()
        self.assertEqual(self.modify(products[0], unit="pièce").status_code, 409)

    def test_atomic_three_product_adjustment(self):
        products = [self.create_product(code=f"LOT-{i}") for i in range(3)]
        self.assertEqual(self.initial(products, [10, 5, 0]).status_code, 201)
        result = self.adjust(products, [8, 9, 1])
        self.assertEqual(result.status_code, 200, result.content)
        self.assertEqual(
            [StockBalance.objects.get(product=p).physical for p in products], [8, 9, 1]
        )
        self.assertEqual(StockEntry.objects.filter(kind="MANUAL_ADJUSTMENT").count(), 3)
        self.assertEqual(
            len({e.operation_id for e in StockEntry.objects.filter(kind="MANUAL_ADJUSTMENT")}), 1
        )

    def test_initial_stale_product_unit_requires_recount(self):
        product = self.create_product()
        old = product.version
        self.assertEqual(self.modify(product, unit="boîte").status_code, 200)
        response = self.post_json(
            "/api/v1/inventories/initial",
            {
                "lines": [
                    {
                        "product_id": str(product.pk),
                        "expected_stock_version": 0,
                        "expected_product_version": old,
                        "counted_quantity": 2,
                    }
                ]
            },
            self.operator,
        )
        self.assertEqual(response.json()["error"]["code"], "STALE_PRODUCT_VERSION")
        self.assertEqual(self.stock(product), (None, 0, None, 0))

    def test_activation_real_initial_zero_without_supplier(self):
        product = self.create_product()
        self.assertEqual(self.initial([product], [0]).status_code, 201)
        self.activate(product)
        self.assertEqual(product.status, "ACTIVE")
        self.assertEqual(self.stock(product), (0, 0, 0, 1))

    def test_uncounted_product_cannot_be_adjusted(self):
        product = self.create_product()
        response = self.adjust([product], [0])
        self.assertEqual(response.json()["error"]["code"], "NOT_COUNTED")
        self.assertEqual(StockEntry.objects.count(), 0)

    def test_initial_batch_invalid_line_rolls_back_all(self):
        products = [self.create_product(code=f"INITIAL-{i}") for i in range(2)]
        self.initial(products[:1])
        response = self.initial(products, [3, 6])
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.stock(products[1]), (None, 0, None, 0))
        self.assertEqual(StockEntry.objects.count(), 1)

    def test_invalid_negative_fraction_bool_duplicate_and_oversize_rejected(self):
        product = self.create_product()
        self.initial([product])
        for value in [-1, 1.2, True, "3", 1000000001]:
            self.assertEqual(self.adjust([product], [value]).status_code, 400)
        payload = self.adjustment_payload([product, product], [3, 4])
        self.assertEqual(
            self.post_json("/api/v1/stocks/adjustments", payload, self.operator).status_code, 400
        )
        self.assertEqual(self.stock(product)[0], 10)
        self.assertEqual(StockEntry.objects.count(), 1)

    def test_stale_line_rejects_whole_batch_with_current_values(self):
        products = [self.create_product(code=f"STALE-{i}") for i in range(3)]
        self.initial(products)
        self.adjust(products[:1], [9])
        response = self.adjust(products, [5, 5, 5], versions=[1, 1, 1])
        self.assertEqual(response.status_code, 409)
        self.assertEqual(len(response.json()["error"]["current"]), 3)
        self.assertEqual([self.stock(p)[0] for p in products], [9, 10, 10])
        self.assertEqual(InventoryBatch.objects.count(), 2)

    def test_error_after_first_write_rolls_back_stock_audit_outbox_and_batch(self):
        products = [self.create_product(code=f"ROLLBACK-{i}") for i in range(3)]
        self.initial(products)
        totals = [m.objects.count() for m in [Operation, AuditEvent, BusinessEvent, Outbox]]
        original = services.append_change

        def fail_second(execution, stock, **kwargs):
            if kwargs["sequence"] == 2:
                raise RuntimeError("erreur-technique-test")
            return original(execution, stock, **kwargs)

        identifier, key = uuid.uuid4(), uuid.uuid4().hex
        with patch.object(services, "append_change", side_effect=fail_second):
            response = self.adjust(products, [7, 6, 5], identifier=identifier, key=key)
        self.assertEqual(response.status_code, 500)
        self.assertEqual([self.stock(p)[0] for p in products], [10, 10, 10])
        self.assertEqual(OperationIntent.objects.get(pk=identifier).state, "READY")
        self.assertEqual(
            [m.objects.count() for m in [Operation, AuditEvent, BusinessEvent, Outbox]], totals
        )
        self.assertEqual(
            self.adjust(products, [7, 6, 5], identifier=identifier, key=key).status_code, 200
        )

    def test_replay_and_modified_key_binding(self):
        product = self.create_product()
        self.initial([product])
        payload = self.adjustment_payload([product], [7])
        identifier, key = uuid.uuid4(), uuid.uuid4().hex
        first = self.post_json(
            "/api/v1/stocks/adjustments", payload, self.operator, identifier=identifier, key=key
        )
        second = self.post_json(
            "/api/v1/stocks/adjustments", payload, self.operator, identifier=identifier, key=key
        )
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.json(), first.json())
        self.assertEqual(second["Idempotency-Replayed"], "true")
        payload["lines"][0]["target_physical"] = 6
        self.assertEqual(
            self.post_json(
                "/api/v1/stocks/adjustments", payload, self.operator, identifier=identifier, key=key
            ).status_code,
            409,
        )
        self.assertEqual(StockEntry.objects.count(), 2)

    def test_twenty_concurrent_same_intention_exactly_once(self):
        product = self.create_product()
        self.initial([product])
        payload = self.adjustment_payload([product], [7])
        identifier, key = self.intention("inventory.adjust", payload)
        barrier = threading.Barrier(20, timeout=15)

        def worker():
            close_old_connections()
            try:
                barrier.wait()
                return execute_intent(self.context, intent_id=identifier, key=key, payload=payload)
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=20) as executor:
            outcomes = list(executor.map(lambda _: worker(), range(20)))
        self.assertEqual([r.status for r in outcomes], [200] * 20)
        self.assertEqual(len({r.body["operation_id"] for r in outcomes}), 1)
        self.assertEqual(StockEntry.objects.filter(kind="MANUAL_ADJUSTMENT").count(), 1)
        self.assertEqual(self.stock(product), (7, 0, 7, 2))

    def test_simultaneous_distinct_adjustments_same_version_only_one_succeeds(self):
        product = self.create_product()
        self.initial([product])
        payloads = [self.adjustment_payload([product], [q]) for q in [5, 8]]
        intentions = [self.intention("inventory.adjust", p) for p in payloads]
        barrier = threading.Barrier(2, timeout=10)

        def worker(index):
            close_old_connections()
            try:
                barrier.wait()
                identifier, key = intentions[index]
                return execute_intent(
                    self.context, intent_id=identifier, key=key, payload=payloads[index]
                ).status
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(worker, range(2)))
        self.assertEqual(sorted(results), [200, 409])
        self.assertEqual(StockEntry.objects.count(), 2)

    def test_resolve_does_not_invalidate_concurrent_confirmation(self):
        product = self.create_product()
        self.initial([product])
        payload = self.adjustment_payload([product], [7])
        identifier, key = self.intention("inventory.adjust", payload)
        entered, release = threading.Event(), threading.Event()
        original = services.append_change

        def pause(*args, **kwargs):
            entry = original(*args, **kwargs)
            entered.set()
            if not release.wait(10):
                raise TimeoutError("barrière")
            return entry

        def worker():
            close_old_connections()
            try:
                return execute_intent(self.context, intent_id=identifier, key=key, payload=payload)
            finally:
                connection.close()

        with (
            patch.object(services, "append_change", side_effect=pause),
            ThreadPoolExecutor(max_workers=1) as executor,
        ):
            future = executor.submit(worker)
            self.assertTrue(entered.wait(10))
            try:
                result = resolve_intent(self.context, identifier)
                self.assertEqual((result.status, result.body["state"]), (202, "CHECKING"))
            finally:
                release.set()
            self.assertEqual(future.result().status, 200)
        self.assertEqual(OperationIntent.objects.get(pk=identifier).state, "CONFIRMED")

    def test_below_reservations_rejects_all_and_persists_identifiable_case_once(self):
        product = self.reserved_product()
        other = self.create_product(code="AUTRE")
        self.initial([other])
        payload = self.adjustment_payload([product, other], [3, 6])
        identifier, key = uuid.uuid4(), uuid.uuid4().hex
        count = StockEntry.objects.count()
        response = self.post_json(
            "/api/v1/stocks/adjustments", payload, self.operator, identifier=identifier, key=key
        )
        self.assertEqual(response.status_code, 409, response.content)
        case = StockCase.objects.get()
        self.assertEqual(response.json()["case_reference"], case.reference)
        self.assertEqual(case.failed_intent_id, identifier)
        self.assertEqual(case.operation.command_type, "cases.stock_conflict")
        self.assertEqual(self.stock(product), (10, 4, 6, 2))
        self.assertEqual(self.stock(other)[0], 10)
        self.assertEqual(StockEntry.objects.count(), count)
        self.assertFalse(Operation.objects.filter(intent_id=identifier).exists())
        self.assertEqual(AuditEvent.objects.filter(operation=case.operation).count(), 1)
        self.assertEqual(Outbox.objects.filter(event__operation=case.operation).count(), 2)
        replay = self.post_json(
            "/api/v1/stocks/adjustments", payload, self.operator, identifier=identifier, key=key
        )
        self.assertEqual(replay.json(), response.json())
        self.assertEqual(StockCase.objects.count(), 1)
        self.assertEqual(StockHold.objects.count(), 1)

    def test_case_failure_rolls_back_incident_and_original_intent_remains_ready(self):
        product = self.reserved_product()
        identifier, key = uuid.uuid4(), uuid.uuid4().hex
        with patch(
            "apps.cases.services.record_facts", side_effect=RuntimeError("audit indisponible")
        ):
            response = self.adjust([product], [3], identifier=identifier, key=key)
        self.assertEqual(response.status_code, 500)
        self.assertEqual(StockCase.objects.count(), 0)
        self.assertEqual(StockHold.objects.count(), 0)
        self.assertEqual(OperationIntent.objects.get(pk=identifier).state, "READY")
        self.assertEqual(
            self.adjust([product], [3], identifier=identifier, key=key).status_code, 409
        )
        self.assertEqual(StockCase.objects.count(), 1)

    def test_hold_blocks_decrease_and_reservation_but_allows_addition_and_release(self):
        product = self.reserved_product()
        self.adjust([product], [3])
        self.assertEqual(self.adjust([product], [9]).json()["error"]["code"], "PRODUCT_ON_HOLD")
        self.assertEqual(self.reserve(product, 1).body["error"]["code"], "REQUEST_REFUSED")
        self.assertEqual(self.adjust([product], [12]).status_code, 200)
        self.assertEqual(self.reserve(product, -4).status, 200)
        self.assertEqual(self.stock(product), (12, 0, 12, 4))
        self.assertEqual(StockHold.objects.count(), 1)

    def test_stale_reservation_version_refused_without_false_incident(self):
        product = self.reserved_product()
        response = self.adjust([product], [3], versions=[1])
        self.assertEqual(response.json()["error"]["code"], "STALE_VERSION")
        self.assertEqual(StockCase.objects.count(), 0)

    def test_server_permissions_and_scopes(self):
        product = self.create_product()
        for client in [self.admin_client, self.user_client]:
            self.assertEqual(self.initial([product], client=client).status_code, 403)
            self.assertEqual(self.adjust([product], [1], client=client).status_code, 403)
        for path in [
            "/stocks",
            "/stocks/inventaire-initial",
            "/stocks/mouvements",
            "/stocks/signalements",
            "/api/v1/stocks",
            "/api/v1/cases",
            "/api/v1/stock-movements",
        ]:
            self.assertEqual(self.user_client.get(path).status_code, 403, path)
        self.assertEqual(self.operator.get("/configuration/supplier").status_code, 403)

    def test_case_supervision_and_activity_are_scoped_without_m6_resolution(self):
        product = self.reserved_product()
        response = self.adjust([product], [3])
        case_id = response.json()["case_id"]
        from apps.accounts.models import User

        other = User.objects.create_user(
            "autre-reappro", role="REPLENISHER", must_change_password=False
        )
        other_client = self.logged(other)
        for client in [self.admin_client, self.operator]:
            self.assertEqual(client.get(f"/api/v1/cases/{case_id}").status_code, 200)
            self.assertEqual(client.get(f"/stocks/signalements/{case_id}").status_code, 200)
        self.assertEqual(other_client.get(f"/api/v1/cases/{case_id}").status_code, 404)
        self.assertEqual(other_client.get("/api/v1/stock-movements").json()["movements"], [])
        self.assertEqual(
            self.admin_client.post(f"/api/v1/cases/{case_id}/resolve").status_code, 400
        )

    def test_archived_product_physical_stock_remains_countable_and_traceable(self):
        product = self.create_product()
        self.upload(product)
        product.refresh_from_db()
        self.assertEqual(
            self.post_json(
                f"/api/v1/products/{product.pk}/archive",
                {"expected_version": product.version, "confirmed": True},
            ).status_code,
            200,
        )
        self.assertEqual(self.initial([product], [6]).status_code, 201)
        self.assertEqual(self.adjust([product], [5]).status_code, 200)
        self.assertEqual(self.operator.get(f"/api/v1/products/{product.pk}").status_code, 403)
        self.assertEqual(self.operator.get(f"/api/v1/stocks/{product.pk}/photo").status_code, 200)

    def test_off_pilot_issue_is_ordinary_adjustment_without_floor_or_request(self):
        product = self.create_product()
        self.initial([product])
        response = self.adjust([product], [7])
        entry = StockEntry.objects.get(operation_id=response.json()["operation_id"])
        self.assertEqual((entry.kind, entry.delta_p, entry.delta_r), ("MANUAL_ADJUSTMENT", -3, 0))
        self.assertNotIn("floor", {f.name for f in entry._meta.fields})
        self.assertNotIn("request", {f.name for f in entry._meta.fields})

    def test_reconstruction_and_immutable_history(self):
        product = self.reserved_product()
        self.adjust([product], [14])
        self.reserve(product, -4)
        self.adjust([product], [6])
        entries = list(StockEntry.objects.filter(product=product).order_by("version_after"))
        balance = StockBalance.objects.get(product=product)
        self.assertEqual(sum(e.delta_p for e in entries), balance.physical)
        self.assertEqual(sum(e.delta_r for e in entries), balance.reserved)
        self.assertEqual(balance.latest_entry_id, entries[-1].pk)
        output = io.StringIO()
        call_command("check_stock_ledger", stdout=output)
        self.assertIn("Registre cohérent", output.getvalue())
        with self.assertRaises(DatabaseError), transaction.atomic():
            StockEntry.objects.filter(pk=entries[0].pk).update(physical_after=9)
        with self.assertRaises(DatabaseError), transaction.atomic():
            StockEntry.objects.filter(pk=entries[0].pk).delete()

    def test_sql_disallows_unhistorized_balance_and_broken_invariants(self):
        product = self.create_product()
        self.initial([product])
        for fields in [
            {"physical": -1},
            {"reserved": 11},
            {"physical": 9},
            {"reserved": 1},
            {"version": 2},
        ]:
            with self.assertRaises(IntegrityError), transaction.atomic():
                StockBalance.objects.filter(product=product).update(**fields)
                with connection.cursor() as cursor:
                    cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
        self.assertEqual(self.stock(product), (10, 0, 10, 1))

    def test_runtime_role_initial_adjustment_case_and_sql_guards(self):
        import os

        from apps.core.management.commands.grant_runtime import grant_runtime

        product = self.create_product()
        grant_runtime(os.environ["RUNTIME_DATABASE_USER"])
        original = connection.settings_dict.copy()
        connection.close()
        connection.settings_dict.update(
            USER=os.environ["RUNTIME_DATABASE_USER"],
            PASSWORD=os.environ["RUNTIME_DATABASE_PASSWORD"],
        )
        try:
            self.assertEqual(self.initial([product]).status_code, 201)
            self.activate(product)
            self.assertEqual(self.reserve(product, 4).status, 200)
            self.assertEqual(self.adjust([product], [12]).status_code, 200)
            self.assertEqual(self.adjust([product], [3]).status_code, 409)
            self.assertEqual(StockCase.objects.count(), 1)
            for statement in [
                "UPDATE inventory_stockbalance SET physical=1",
                "DELETE FROM inventory_stockbalance",
                "DELETE FROM inventory_stockentry",
                "UPDATE cases_stockhold SET case_id=case_id",
                "DELETE FROM cases_stockcase",
            ]:
                with (
                    self.assertRaises(DatabaseError),
                    transaction.atomic(),
                    connection.cursor() as cursor,
                ):
                    cursor.execute(statement)
        finally:
            connection.close()
            connection.settings_dict.clear()
            connection.settings_dict.update(original)

    def test_initial_concurrent_different_intents_only_one_initial(self):
        product = self.create_product()
        payload = {
            "lines": [
                {
                    "product_id": str(product.pk),
                    "expected_stock_version": 0,
                    "expected_product_version": product.version,
                    "counted_quantity": 0,
                }
            ]
        }
        intentions = [self.intention("inventory.initial", payload) for _ in range(2)]
        barrier = threading.Barrier(2, timeout=10)

        def worker(binding):
            close_old_connections()
            try:
                barrier.wait()
                return execute_intent(
                    self.context, intent_id=binding[0], key=binding[1], payload=payload
                ).status
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(worker, intentions))
        self.assertEqual(sorted(results), [201, 409])
        self.assertEqual(StockEntry.objects.filter(kind="INITIAL_COUNT").count(), 1)
        self.assertEqual(self.stock(product), (0, 0, 0, 1))

    def test_csp_allows_same_origin_fetch_only_and_csrf_is_enforced(self):
        from django.test import Client

        response = self.operator.get("/stocks")
        self.assertIn("connect-src 'self'", response["Content-Security-Policy"])
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.replenisher)
        self.assertEqual(
            self.post_json("/api/v1/stocks/adjustments", {"lines": []}, client).status_code, 403
        )

    def test_registry_reservation_probe_is_not_public_module(self):
        commands._registry.pop(RESERVATION.name)
        response = self.post_json(
            "/api/v1/operation-intents",
            {
                "intent_id": str(uuid.uuid4()),
                "key": uuid.uuid4().hex,
                "command_type": RESERVATION.name,
                "payload_hash": payload_hash({}),
            },
            self.user_client,
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.post_json("/api/v1/deliveries", {}).status_code, 404)
        # M8 expose désormais cette lecture, mais jamais une création globale
        # détachée d’une commande : le POST reste explicitement interdit.
        self.assertEqual(self.post_json("/api/v1/receipts", {}).status_code, 405)

    def test_new_conflict_is_reported_even_when_other_batch_product_already_held(self):
        first = self.reserved_product()
        self.adjust([first], [3])
        second = self.create_product(code="NOUVEAU-CONFLIT")
        self.initial([second])
        self.activate(second)
        self.reserve(second, 5)
        response = self.adjust([first, second], [3, 2])
        self.assertEqual(response.json()["error"]["code"], "STOCK_BELOW_RESERVED")
        self.assertEqual(StockCase.objects.count(), 2)
        self.assertEqual(StockHold.objects.count(), 2)
        self.assertEqual(self.stock(first)[0], 10)
        self.assertEqual(self.stock(second)[0], 10)
