"""M4 : vraies demandes/réservations, sans livraison M5."""

import json
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from django.db import DatabaseError, IntegrityError, close_old_connections, connection, transaction
from django.test import Client, TransactionTestCase

from apps.accounts.models import User
from apps.audit.models import AuditEvent
from apps.core import idempotency
from apps.core.errors import BusinessError
from apps.core.management.commands.grant_runtime import grant_runtime
from apps.core.models import Operation, OperationIntent
from apps.core.policies import ActorContext
from apps.inventory.models import StockEntry
from apps.notifications.models import BusinessEvent, Outbox
from apps.requests import services
from apps.requests.models import (
    InternalRequest,
    RequestEvent,
    RequestLine,
    Reservation,
)
from apps.requests.services import ensure_registered
from tests.probes.inventory import COMMAND as RESERVATION_PROBE
from tests.requests_evidence import observations
from tests.test_accounts import FICTIVE_PASSWORD
from tests.test_inventory import InventoryFixtures


class RequestFixtures(InventoryFixtures):
    def setUp(self):
        super().setUp()
        ensure_registered()

    def demandable_product(self, code="DEMANDE-FICTIVE", physical=10, maximum=5):
        product = self.create_product(code=code, name=f"Produit fictif {code}")
        self.initial([product], [physical])
        self.activate(product)
        self.assertEqual(
            self.rights(
                product,
                [
                    {
                        "floor_id": str(self.floor.pk),
                        "visible": True,
                        "demandable": True,
                        "maximum": maximum,
                    }
                ],
            ).status_code,
            200,
        )
        product.refresh_from_db()
        return product

    def send(self, products, quantities, *, client=None, identifier=None, key=None):
        return self.post_json(
            "/api/v1/requests",
            {
                "lines": [
                    {"product_id": str(p.pk), "quantity": q}
                    for p, q in zip(products, quantities, strict=True)
                ]
            },
            client or self.user_client,
            identifier=identifier,
            key=key,
        )

    def cancel_request(self, request, *, client=None, identifier=None, key=None, version=None):
        return self.post_json(
            f"/api/v1/requests/{request.pk}/cancel",
            {"expected_version": request.version if version is None else version},
            client or self.user_client,
            identifier=identifier,
            key=key,
        )


class RequestTests(RequestFixtures, TransactionTestCase):
    def test_multi_product_request_reserves_without_changing_physical(self):
        products = [self.demandable_product(code=f"PROD-{i}") for i in range(3)]
        response = self.send(products, [2, 3, 4])
        self.assertEqual(response.status_code, 201, response.content)
        row = InternalRequest.objects.get()
        self.assertTrue(row.reference.startswith("DEM-"))
        self.assertEqual(row.status, "NEW")
        self.assertEqual(
            [self.stock(p)[:3] for p in products], [(10, 2, 8), (10, 3, 7), (10, 4, 6)]
        )
        self.assertEqual(Reservation.objects.count(), 3)
        for line in row.lines.all():
            self.assertEqual(
                line.quantity, line.delivered + line.closed + line.reservation.remaining
            )

    def test_cancel_releases_all_exactly_and_preserves_request(self):
        product = self.demandable_product()
        self.assertEqual(self.send([product], [4]).status_code, 201)
        request = InternalRequest.objects.get()
        response = self.cancel_request(request)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self.stock(product)[:3], (10, 0, 10))
        request.refresh_from_db()
        self.assertEqual((request.status, request.version), ("CANCELLED", 1))
        self.assertEqual(RequestEvent.objects.count(), 2)
        self.assertEqual(RequestLine.objects.get().closed, 4)
        self.assertEqual(StockEntry.objects.filter(kind="RELEASE").get().delta_r, -4)

    def test_assistant_catalog_only_authorized_no_global_stock_or_supplier_data(self):
        allowed = self.demandable_product()
        hidden = self.demandable_product(code="CACHE")
        self.rights(hidden, [])
        rows = self.user_client.get("/api/v1/request-catalog").json()["products"]
        self.assertEqual([row["product_id"] for row in rows], [str(allowed.pk)])
        self.assertEqual((rows[0]["available"], rows[0]["maximum_allowed"]), (10, 5))
        for word in ["physical", '"reserved"', "supplier", "minimum", "target"]:
            self.assertNotIn(word, json.dumps(rows))

    def test_ceiling_and_availability_rechecked_after_display(self):
        product = self.demandable_product()
        self.assertEqual(self.send([product], [6]).status_code, 409)
        product.refresh_from_db()
        self.rights(
            product,
            [{"floor_id": str(self.floor.pk), "visible": True, "demandable": True, "maximum": 2}],
        )
        self.assertEqual(self.send([product], [3]).status_code, 409)
        self.adjust([product], [1])
        result = self.send([product], [2])
        self.assertEqual(result.status_code, 409)
        self.assertEqual(result.json()["error"]["lines"][0]["maximum_allowed"], 1)
        self.assertFalse(InternalRequest.objects.exists())

    def test_noncounted_inactive_archived_and_held_refused(self):
        uncounted = self.create_product(code="NON-COMPTE")
        self.rights(
            uncounted,
            [{"floor_id": str(self.floor.pk), "visible": True, "demandable": True, "maximum": 5}],
        )
        self.assertEqual(self.send([uncounted], [1]).status_code, 409)
        pending = self.create_product(code="ATTENTE")
        self.initial([pending])
        pending.refresh_from_db()
        self.rights(
            pending,
            [{"floor_id": str(self.floor.pk), "visible": True, "demandable": True, "maximum": 5}],
        )
        self.assertEqual(self.send([pending], [1]).status_code, 409)
        archived = self.demandable_product(code="ARCHIVE")
        self.post_json(
            f"/api/v1/products/{archived.pk}/archive",
            {"expected_version": archived.version, "confirmed": True},
        )
        self.assertEqual(self.send([archived], [1]).status_code, 409)
        held = self.demandable_product(code="BLOQUE")
        self.send([held], [4])
        self.adjust([held], [3])
        self.assertEqual(self.send([held], [1]).status_code, 409)
        self.assertEqual(self.stock(held)[:3], (10, 4, 6))

    def test_invalid_line_rejects_entire_request_with_no_operation_or_reservation(self):
        products = [self.demandable_product(code=f"ATOM-{i}") for i in range(2)]
        identifier = uuid.uuid4()
        before = StockEntry.objects.count()
        result = self.send(products, [2, 6], identifier=identifier)
        self.assertEqual(result.status_code, 409)
        self.assertEqual(StockEntry.objects.count(), before)
        self.assertFalse(InternalRequest.objects.exists())
        self.assertFalse(Reservation.objects.exists())
        self.assertFalse(Operation.objects.filter(intent_id=identifier).exists())
        self.assertEqual(OperationIntent.objects.get(pk=identifier).state, "REJECTED")

    def test_positive_integer_strict_payload_duplicates_and_unknown_reference(self):
        product = self.demandable_product()
        for value in [0, -1, True, 1.2, "2", 1000000001]:
            self.assertEqual(self.send([product], [value]).status_code, 400)
        self.assertEqual(self.send([product, product], [1, 2]).status_code, 400)
        self.assertEqual(
            self.post_json(
                "/api/v1/requests",
                {
                    "lines": [{"product_id": str(product.pk), "quantity": 1}],
                    "floor_id": str(self.other_floor.pk),
                },
                self.user_client,
            ).status_code,
            400,
        )
        self.assertEqual(
            self.post_json(
                "/api/v1/requests",
                {"lines": [{"product_id": str(uuid.uuid4()), "quantity": 1}]},
                self.user_client,
            ).status_code,
            404,
        )
        self.assertFalse(InternalRequest.objects.exists())

    def test_missing_right_or_ceiling_refuses_without_exposing_hidden_product(self):
        product = self.demandable_product()
        self.rights(product, [])
        response = self.send([product], [1])
        self.assertEqual(response.status_code, 409)
        self.assertNotIn(product.name, response.content.decode())
        product.refresh_from_db()
        self.rights(
            product,
            [
                {
                    "floor_id": str(self.floor.pk),
                    "visible": True,
                    "demandable": False,
                    "maximum": None,
                }
            ],
        )
        self.assertEqual(self.send([product], [1]).status_code, 409)
        self.assertEqual(
            self.user_client.get("/api/v1/request-catalog").json()["products"][0][
                "maximum_allowed"
            ],
            0,
        )

    def test_twenty_confirmations_same_intention_create_one_request_and_event(self):
        product = self.demandable_product()
        payload = {"lines": [{"product_id": str(product.pk), "quantity": 4}]}
        context = ActorContext(self.assistant.pk, self.assistant.auth_version)
        identifier, key = self.intention("requests.create", payload, context=context)
        barrier = threading.Barrier(20, timeout=15)

        def worker(_):
            close_old_connections()
            try:
                barrier.wait()
                return idempotency.execute_intent(
                    context, intent_id=identifier, key=key, payload=payload
                )
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=20) as pool:
            outcomes = list(pool.map(worker, range(20)))
        self.assertEqual([o.status for o in outcomes], [201] * 20)
        self.assertEqual(len({o.body["operation_id"] for o in outcomes}), 1)
        self.assertEqual(InternalRequest.objects.count(), 1)
        self.assertEqual(StockEntry.objects.filter(kind="RESERVE").count(), 1)
        self.assertEqual(RequestEvent.objects.count(), 1)
        self.assertEqual(self.stock(product)[:3], (10, 4, 6))
        observations["twenty_confirmations"] = {
            "requests": len(outcomes),
            "distinct_operations": len({o.body["operation_id"] for o in outcomes}),
            "internal_requests": InternalRequest.objects.count(),
            "reservation_entries": StockEntry.objects.filter(kind="RESERVE").count(),
            "P_R_D": list(self.stock(product)[:3]),
        }

    def test_two_assistants_race_for_last_units_no_overbooking(self):
        product = self.demandable_product(physical=1, maximum=1)
        other = User.objects.create_user(
            "autre-demandeur — profil A",
            FICTIVE_PASSWORD,
            role="ASSISTANT",
            floor=self.floor,
            must_change_password=False,
        )
        payload = {"lines": [{"product_id": str(product.pk), "quantity": 1}]}
        contexts = [ActorContext(u.pk, u.auth_version) for u in [self.assistant, other]]
        bindings = [self.intention("requests.create", payload, context=c) for c in contexts]
        barrier = threading.Barrier(2, timeout=10)

        def worker(index):
            close_old_connections()
            try:
                barrier.wait()
                identifier, key = bindings[index]
                return idempotency.execute_intent(
                    contexts[index], intent_id=identifier, key=key, payload=payload
                ).status
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            statuses = list(pool.map(worker, range(2)))
        self.assertEqual(sorted(statuses), [201, 409])
        self.assertEqual(self.stock(product)[:3], (1, 1, 0))
        self.assertEqual(Reservation.objects.count(), 1)
        observations["last_stock_race"] = {
            "statuses": sorted(statuses),
            "P_R_D": list(self.stock(product)[:3]),
            "reservations": Reservation.objects.count(),
        }

    def test_same_key_changed_content_refused_without_second_reservation(self):
        product = self.demandable_product()
        identifier, key = uuid.uuid4(), uuid.uuid4().hex
        first = self.send([product], [2], identifier=identifier, key=key)
        replay = self.send([product], [2], identifier=identifier, key=key)
        self.assertEqual(first.json(), replay.json())
        self.assertEqual(self.send([product], [3], identifier=identifier, key=key).status_code, 409)
        self.assertEqual(self.stock(product)[:3], (10, 2, 8))

    def test_failure_mid_request_rolls_back_every_line_audit_and_outbox(self):
        products = [self.demandable_product(code=f"PANNE-{i}") for i in range(2)]
        original = services.append_change

        def fail(*args, **kwargs):
            if kwargs["sequence"] == 2:
                raise RuntimeError("panne fictive")
            return original(*args, **kwargs)

        identifier, key = uuid.uuid4(), uuid.uuid4().hex
        totals = [m.objects.count() for m in [Operation, AuditEvent, BusinessEvent, Outbox]]
        with patch.object(services, "append_change", side_effect=fail):
            self.assertEqual(
                self.send(products, [2, 3], identifier=identifier, key=key).status_code, 500
            )
        self.assertFalse(InternalRequest.objects.exists())
        self.assertFalse(Reservation.objects.exists())
        self.assertEqual(
            [m.objects.count() for m in [Operation, AuditEvent, BusinessEvent, Outbox]], totals
        )
        self.assertEqual([self.stock(p)[:3] for p in products], [(10, 0, 10)] * 2)
        self.assertEqual(OperationIntent.objects.get(pk=identifier).state, "READY")
        self.assertEqual(
            self.send(products, [2, 3], identifier=identifier, key=key).status_code, 201
        )

    def test_events_audit_and_outbox_match_request_without_email(self):
        product = self.demandable_product()
        response = self.send([product], [3])
        request = InternalRequest.objects.get()
        operation = Operation.objects.get(pk=response.json()["operation_id"])
        self.assertEqual(
            AuditEvent.objects.get(operation=operation).after["reference"], request.reference
        )
        self.assertEqual(Outbox.objects.filter(event__operation=operation).count(), 2)
        event = BusinessEvent.objects.get(operation=operation, event_type="request.created")
        self.assertEqual(
            event.payload, {"request_id": str(request.pk), "recipient_role": "REPLENISHER"}
        )
        self.cancel_request(request)
        self.assertEqual(BusinessEvent.objects.filter(event_type="request.cancelled").count(), 1)
        self.assertFalse(BusinessEvent.objects.filter(event_type__icontains="email").exists())

    def test_user_floor_deactivation_and_revoked_access_stop_new_requests(self):
        product = self.demandable_product()
        self.post_json(
            f"/api/v1/floors/{self.floor.pk}",
            {"expected_version": self.floor.version, "active": False},
            method="patch",
        )
        self.assertEqual(self.send([product], [1]).status_code, 409)
        self.assertFalse(InternalRequest.objects.exists())
        response = self.update(self.assistant, {"is_active": False})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.send([product], [1]).status_code, 401)

    def test_foreign_request_page_api_cancel_photo_and_history_are_scoped(self):
        product = self.demandable_product()
        self.send([product], [2])
        request = InternalRequest.objects.get()
        line = request.lines.get()
        other = User.objects.create_user(
            "demandeur — profil A-etrangere",
            FICTIVE_PASSWORD,
            role="ASSISTANT",
            floor=self.other_floor,
            must_change_password=False,
        )
        client = self.logged(other)
        for path in [
            f"/api/v1/requests/{request.pk}",
            f"/demandes/{request.pk}",
            f"/api/v1/requests/{request.pk}/lines/{line.pk}/photo",
        ]:
            self.assertEqual(client.get(path).status_code, 404, path)
        self.assertEqual(self.cancel_request(request, client=client).status_code, 404)
        self.assertEqual(client.get("/api/v1/requests").json()["requests"], [])
        self.assertEqual(client.get("/api/v1/requests/history").json()["events"], [])
        self.assertEqual(self.admin_client.get(f"/api/v1/requests/{request.pk}").status_code, 200)
        self.assertEqual(self.operator.get(f"/api/v1/requests/{request.pk}").status_code, 200)

    def test_non_assistant_creation_and_cancel_forbidden_and_csrf_required(self):
        product = self.demandable_product()
        for client in [self.admin_client, self.operator]:
            self.assertEqual(self.send([product], [1], client=client).status_code, 403)
        self.send([product], [2])
        request = InternalRequest.objects.get()
        for client in [self.admin_client, self.operator]:
            self.assertEqual(self.cancel_request(request, client=client).status_code, 403)
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.assistant)
        self.assertEqual(self.send([product], [1], client=client).status_code, 403)

    def test_cancellation_idempotence_new_key_double_cancel_and_concurrency(self):
        product = self.demandable_product()
        self.send([product], [3])
        request = InternalRequest.objects.get()
        context = ActorContext(self.assistant.pk, self.assistant.auth_version)
        payload = {"expected_version": 0}
        bindings = [
            self.intention("requests.cancel", payload, resource=request.pk, context=context)
            for _ in range(2)
        ]
        barrier = threading.Barrier(2, timeout=10)

        def worker(binding):
            close_old_connections()
            try:
                barrier.wait()
                return idempotency.execute_intent(
                    context, intent_id=binding[0], key=binding[1], payload=payload
                )
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(worker, bindings))
        self.assertEqual(sorted(o.status for o in outcomes), [200, 409])
        for binding, outcome in zip(bindings, outcomes, strict=True):
            replay = idempotency.execute_intent(
                context, intent_id=binding[0], key=binding[1], payload=payload
            )
            self.assertEqual(replay.body, outcome.body)
        self.assertEqual(StockEntry.objects.filter(kind="RELEASE").count(), 1)
        self.assertEqual(self.stock(product)[:3], (10, 0, 10))
        request.refresh_from_db()
        self.assertEqual(self.cancel_request(request).status_code, 409)

    def test_cancel_allowed_after_rights_removed_product_archived_and_floor_disabled(self):
        product = self.demandable_product()
        self.send([product], [4])
        request = InternalRequest.objects.get()
        self.rights(product, [])
        product.refresh_from_db()
        self.post_json(
            f"/api/v1/products/{product.pk}/archive",
            {"expected_version": product.version, "confirmed": True},
        )
        self.post_json(
            f"/api/v1/floors/{self.floor.pk}",
            {"expected_version": self.floor.version, "active": False},
            method="patch",
        )
        self.assertEqual(self.cancel_request(request).status_code, 200)
        self.assertEqual(self.stock(product)[:3], (10, 0, 10))

    def test_historical_names_floor_permissions_photo_and_owner_survive_changes(self):
        product = self.demandable_product()
        self.send([product], [3])
        request = InternalRequest.objects.get()
        line = request.lines.get()
        photo = line.photo_id
        self.modify(product, name="Nouveau nom fictif")
        product.refresh_from_db()
        self.upload(product)
        self.rights(product, [])
        self.post_json(
            f"/api/v1/floors/{self.floor.pk}",
            {"expected_version": self.floor.version, "name": "Unité fictif renommé"},
            method="patch",
        )
        self.update(self.assistant, {"floor_id": str(self.other_floor.pk)})
        self.assistant.refresh_from_db()
        client = self.logged(self.assistant)
        data = client.get(f"/api/v1/requests/{request.pk}").json()["request"]
        self.assertEqual(data["floor_name"], "Unité fictif A")
        self.assertEqual(data["lines"][0]["name"], line.product_name)
        self.assertEqual(data["lines"][0]["maximum_at_creation"], 5)
        self.assertEqual(
            client.get(f"/api/v1/requests/{request.pk}/lines/{line.pk}/photo").status_code, 200
        )
        line.refresh_from_db()
        self.assertEqual(line.photo_id, photo)
        self.assertEqual(self.stock(product)[:3], (10, 3, 7))

    def test_sql_projection_snapshot_and_fact_tampering_refused(self):
        product = self.demandable_product()
        self.send([product], [4])
        request = InternalRequest.objects.get()
        line = request.lines.get()
        for statement in [
            "UPDATE requests_reservation SET remaining=1",
            "UPDATE requests_requestline SET closed=3",
            "UPDATE requests_requestline SET quantity=6",
            "UPDATE requests_internalrequest SET floor_name='falsifie'",
            "DELETE FROM requests_requestline",
            "UPDATE requests_requestevent SET kind='CANCELLED'",
            "DELETE FROM requests_reservationchange",
        ]:
            with (
                self.assertRaises(DatabaseError),
                transaction.atomic(),
                connection.cursor() as cursor,
            ):
                cursor.execute(statement)
                cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
        self.assertEqual(self.stock(product)[:3], (10, 4, 6))
        self.assertEqual(Reservation.objects.get(line=line).remaining, 4)

    def test_unlinked_technical_reservation_probe_cannot_commit_after_m4(self):
        product = self.demandable_product()
        context = ActorContext(self.assistant.pk, self.assistant.auth_version)
        payload = {"version": self.stock(product)[3], "delta": 1}
        identifier, key = self.intention(
            RESERVATION_PROBE.name, payload, resource=product.pk, context=context
        )
        with self.assertRaises(IntegrityError):
            idempotency.execute_intent(context, intent_id=identifier, key=key, payload=payload)
        self.assertEqual(self.stock(product)[:3], (10, 0, 10))
        self.assertFalse(Reservation.objects.exists())
        self.assertEqual(OperationIntent.objects.get(pk=identifier).state, "READY")

    def test_runtime_sql_role_can_create_and_cancel_but_cannot_erase_history(self):
        product = self.demandable_product()
        grant_runtime(os.environ["RUNTIME_DATABASE_USER"])
        original = connection.settings_dict.copy()
        connection.close()
        connection.settings_dict.update(
            USER=os.environ["RUNTIME_DATABASE_USER"],
            PASSWORD=os.environ["RUNTIME_DATABASE_PASSWORD"],
        )
        try:
            self.assertEqual(self.send([product], [2]).status_code, 201)
            request = InternalRequest.objects.get()
            self.assertEqual(self.cancel_request(request).status_code, 200)
            with (
                self.assertRaises(DatabaseError),
                transaction.atomic(),
                connection.cursor() as cursor,
            ):
                cursor.execute("DELETE FROM requests_requestevent")
        finally:
            connection.close()
            connection.settings_dict.clear()
            connection.settings_dict.update(original)

    def test_permission_change_waits_for_inflight_reservation_then_applies_to_next_request(self):
        product = self.demandable_product()
        payload = {"lines": [{"product_id": str(product.pk), "quantity": 3}]}
        context = ActorContext(self.assistant.pk, self.assistant.auth_version)
        identifier, key = self.intention("requests.create", payload, context=context)
        entered, release, changed = threading.Event(), threading.Event(), threading.Event()
        original = services.append_change

        def pause(*args, **kwargs):
            result = original(*args, **kwargs)
            entered.set()
            if not release.wait(10):
                raise TimeoutError("barrière")
            return result

        def create_worker():
            close_old_connections()
            try:
                return idempotency.execute_intent(
                    context, intent_id=identifier, key=key, payload=payload
                )
            finally:
                connection.close()

        def configure_worker():
            close_old_connections()
            try:
                result = self.rights(
                    product,
                    [
                        {
                            "floor_id": str(self.floor.pk),
                            "visible": True,
                            "demandable": True,
                            "maximum": 1,
                        }
                    ],
                )
                changed.set()
                return result.status_code
            finally:
                connection.close()

        with (
            patch.object(services, "append_change", side_effect=pause),
            ThreadPoolExecutor(max_workers=2) as pool,
        ):
            future = pool.submit(create_worker)
            self.assertTrue(entered.wait(10))
            configuration = pool.submit(configure_worker)
            try:
                self.assertFalse(changed.wait(0.2))
            finally:
                release.set()
            self.assertEqual(future.result().status, 201)
            self.assertEqual(configuration.result(), 200)
        self.assertEqual(RequestLine.objects.get().maximum, 5)
        self.assertEqual(self.send([product], [2]).status_code, 409)

    def test_cancel_outbox_failure_rolls_back_release_and_can_retry_same_intent(self):
        product = self.demandable_product()
        self.send([product], [3])
        request = InternalRequest.objects.get()
        identifier, key = uuid.uuid4(), uuid.uuid4().hex
        with patch(
            "apps.core.idempotency.Outbox.objects.create",
            side_effect=RuntimeError("panne outbox fictive"),
        ):
            response = self.cancel_request(request, identifier=identifier, key=key)
        self.assertEqual(response.status_code, 500)
        self.assertEqual(self.stock(product)[:3], (10, 3, 7))
        self.assertEqual(RequestEvent.objects.count(), 1)
        self.assertEqual(InternalRequest.objects.get().status, "NEW")
        self.assertEqual(OperationIntent.objects.get(pk=identifier).state, "READY")
        self.assertEqual(
            self.cancel_request(request, identifier=identifier, key=key).status_code, 200
        )
        self.assertEqual(StockEntry.objects.filter(kind="RELEASE").count(), 1)

    def test_request_and_physical_adjustment_concurrent_share_same_stock_lock(self):
        product = self.demandable_product()
        contexts = [ActorContext(self.assistant.pk, self.assistant.auth_version), self.context]
        payloads = [
            {"lines": [{"product_id": str(product.pk), "quantity": 4}]},
            self.adjustment_payload([product], [3]),
        ]
        bindings = [
            self.intention(command, payload, context=context)
            for command, payload, context in zip(
                ["requests.create", "inventory.adjust"], payloads, contexts, strict=True
            )
        ]
        barrier = threading.Barrier(2, timeout=10)

        def worker(index):
            close_old_connections()
            try:
                barrier.wait()
                identifier, key = bindings[index]
                return idempotency.execute_intent(
                    contexts[index], intent_id=identifier, key=key, payload=payloads[index]
                ).status
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            statuses = list(pool.map(worker, range(2)))
        self.assertIn(statuses, [[201, 409], [409, 200]])
        self.assertIn(self.stock(product)[:3], [(10, 4, 6), (3, 0, 3)])
        with connection.cursor() as cursor:
            cursor.execute("SELECT reserve_assert_reservations(%s)", [product.pk])

    def test_foreign_intention_status_and_resolution_never_reveal_request(self):
        product = self.demandable_product()
        identifier, key = uuid.uuid4(), uuid.uuid4().hex
        self.send([product], [1], identifier=identifier, key=key)
        other = User.objects.create_user(
            "autre-intention",
            FICTIVE_PASSWORD,
            role="ASSISTANT",
            floor=self.floor,
            must_change_password=False,
        )
        client = self.logged(other)
        self.assertEqual(client.get(f"/api/v1/operation-intents/{identifier}").status_code, 404)
        self.assertEqual(
            self.post_json(
                f"/api/v1/operation-intents/{identifier}/resolve", {}, client
            ).status_code,
            404,
        )
        self.assertEqual(InternalRequest.objects.count(), 1)

    def test_revocation_between_registration_and_confirmation_blocks_request(self):
        product = self.demandable_product()
        payload = {"lines": [{"product_id": str(product.pk), "quantity": 2}]}
        context = ActorContext(self.assistant.pk, self.assistant.auth_version)
        identifier, key = self.intention("requests.create", payload, context=context)
        self.assertEqual(self.update(self.assistant, {"is_active": False}).status_code, 200)
        with self.assertRaises(BusinessError) as error:
            idempotency.execute_intent(context, intent_id=identifier, key=key, payload=payload)
        self.assertEqual(error.exception.code, "SESSION_REVOKED")
        self.assertFalse(InternalRequest.objects.exists())
        self.assertEqual(self.stock(product)[:3], (10, 0, 10))

    def test_history_filters_and_m5_routes_remain_unavailable(self):
        product = self.demandable_product()
        self.send([product], [2])
        request = InternalRequest.objects.get()
        self.assertEqual(
            len(
                self.user_client.get(f"/api/v1/requests/history?product_id={product.pk}").json()[
                    "events"
                ]
            ),
            1,
        )
        self.assertEqual(
            self.user_client.get("/api/v1/requests/history?from=invalide").status_code, 400
        )
        self.assertEqual(
            self.user_client.get("/api/v1/requests/history?request_id=invalide").status_code, 400
        )
        # M5 ouvre ces routes aux seuls R ; le compte N conserve le refus serveur.
        for suffix in ["claim", "deliveries"]:
            self.assertEqual(
                self.post_json(f"/api/v1/requests/{request.pk}/{suffix}", {}).status_code, 403
            )
        self.assertEqual(
            self.post_json(f"/api/v1/requests/{request.pk}/close-remainder", {}).status_code, 400
        )
        self.assertFalse(
            self.admin_client.get(f"/api/v1/requests/{request.pk}").json()["request"]["can_cancel"]
        )
