import io
import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from unittest.mock import patch

import psycopg
from django.conf import settings
from django.db import DatabaseError, IntegrityError, connection, connections, transaction
from django.test import Client, LiveServerTestCase, TransactionTestCase, override_settings
from django.utils import timezone

from apps.accounts.models import User
from apps.audit.models import AuditEvent
from apps.core import commands
from apps.core.errors import BusinessError
from apps.core.idempotency import execute_intent, read_intent, register_intent, resolve_intent
from apps.core.management.commands.grant_runtime import grant_runtime
from apps.core.models import Operation, OperationIntent
from apps.core.payloads import payload_hash
from apps.core.policies import ActorContext
from apps.notifications.models import BusinessEvent, Outbox
from tests.probes import commands as probe
from tests.probes.models import Counter, RejectionRecord


class OperationFixtures:
    def setUp(self):
        super().setUp()
        commands.register_command(probe.COMMAND)
        probe.entered.clear()
        probe.release.clear()
        probe.committed.clear()
        probe.backend_ids.clear()
        self.actor = User.objects.create_user(
            "operateur-fictif",
            display_name="Opérateur fictif",
            role=User.Role.REPLENISHER,
            must_change_password=False,
        )
        self.context = ActorContext(self.actor.pk, self.actor.auth_version)
        self.counter = Counter.objects.create(owner=self.actor)
        self.payload = {"delta": 3, "expected_version": 0}
        self.client = self.make_client()

    def tearDown(self):
        probe.release.set()
        commands._registry.clear()
        super().tearDown()

    def make_client(self, user=None, csrf=False):
        client = Client(enforce_csrf_checks=csrf, raise_request_exception=False)
        client.force_login(user or self.actor)
        return client

    def intent(self, payload=None, **overrides):
        identifier = uuid.uuid4()
        key = uuid.uuid4().hex
        payload = self.payload if payload is None else payload
        register_intent(
            self.context,
            intent_id=identifier,
            key=key,
            fingerprint=payload_hash(payload),
            command_type=probe.COMMAND.name,
            resource_id=self.counter.pk,
            **overrides,
        )
        return identifier, key

    def confirm(self, identifier, key, payload=None, client=None):
        return (client or self.client).post(
            f"/_test/confirm/{identifier}",
            data=json.dumps(self.payload if payload is None else payload),
            content_type="application/json",
            headers={"Idempotency-Key": key},
        )

    def assert_no_effects(self, identifier, state="READY"):
        self.counter.refresh_from_db()
        self.assertEqual((self.counter.value, self.counter.version), (0, 0))
        self.assertEqual(OperationIntent.objects.get(pk=identifier).state, state)
        for model in [Operation, AuditEvent, BusinessEvent, Outbox]:
            self.assertEqual(model.objects.count(), 0, model.__name__)


class OperationTests(OperationFixtures, TransactionTestCase):
    def test_atomic_confirmation_contains_audit_and_outbox(self):
        identifier, key = self.intent()
        response = self.confirm(identifier, key)
        self.assertEqual(response.status_code, 200, response.content)
        self.counter.refresh_from_db()
        self.assertEqual((self.counter.value, self.counter.version), (3, 1))
        operation = Operation.objects.get(intent_id=identifier)
        self.assertEqual(response.json()["operation_id"], str(operation.pk))
        self.assertEqual(AuditEvent.objects.get(operation=operation).after["value"], 3)
        self.assertEqual(BusinessEvent.objects.filter(operation=operation).count(), 1)
        self.assertEqual(Outbox.objects.get().event.operation_id, operation.pk)

    def test_twenty_concurrent_requests_confirm_exactly_once(self):
        payload = {**self.payload, "pause": True}
        identifier, key = self.intent(payload)
        clients = [self.make_client() for _ in range(20)]
        barrier = threading.Barrier(20, timeout=10)

        def worker(client):
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_backend_pid()")
                    backend = cursor.fetchone()[0]
                barrier.wait()
                response = self.confirm(identifier, key, payload, client=client)
                return backend, response.status_code, response.json()
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=20) as pool:
            futures = [pool.submit(worker, client) for client in clients]
            waiting = 0
            try:
                self.assertTrue(probe.entered.wait(timeout=5))
                deadline = time.monotonic() + 5
                while waiting < 19 and time.monotonic() < deadline:
                    with connection.cursor() as cursor:
                        cursor.execute(
                            "SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() "
                            "AND wait_event_type='Lock' AND query LIKE '%%core_operationintent%%'"
                        )
                        waiting = cursor.fetchone()[0]
                    time.sleep(0.01)
                self.assertEqual(
                    waiting, 19, "Les 19 autres confirmations doivent attendre le même verrou."
                )
            finally:
                probe.release.set()
            results = [future.result(timeout=10) for future in futures]
        self.assertEqual(len({item[0] for item in results}), 20)
        self.assertTrue(all(item[1] == 200 for item in results), results)
        self.assertEqual(len({item[2]["operation_id"] for item in results}), 1)
        self.assertEqual(len(probe.backend_ids), 1)
        self.assertEqual(Operation.objects.count(), 1)
        self.assertEqual(AuditEvent.objects.count(), 1)
        self.assertEqual(Outbox.objects.count(), 1)
        self.counter.refresh_from_db()
        self.assertEqual((self.counter.value, self.counter.version), (3, 1))
        probe.observations["concurrent_confirmation"] = {
            "requests": 20,
            "distinct_initial_database_connections": len({item[0] for item in results}),
            "waiting_on_intent_lock": waiting,
            "confirmed_operations": Operation.objects.count(),
        }

    def test_key_cannot_bind_different_payload(self):
        identifier, key = self.intent()
        response = self.confirm(identifier, key, {"delta": 4, "expected_version": 0})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "IDEMPOTENCY_MISMATCH")
        self.assert_no_effects(identifier)

    def test_registration_is_idempotent_and_conflicts_are_rejected(self):
        identifier, key = self.intent()
        result = register_intent(
            self.context,
            intent_id=identifier,
            key=key,
            fingerprint=payload_hash(self.payload),
            command_type=probe.COMMAND.name,
            resource_id=self.counter.pk,
        )
        self.assertEqual(result.body["intent_id"], str(identifier))
        with self.assertRaises(BusinessError):
            register_intent(
                self.context,
                intent_id=uuid.uuid4(),
                key=key,
                fingerprint=payload_hash(self.payload),
                command_type=probe.COMMAND.name,
                resource_id=self.counter.pk,
            )
        self.assertEqual(OperationIntent.objects.count(), 1)

    def test_technical_exception_rolls_back_every_write(self):
        payload = {**self.payload, "fail": "technical"}
        identifier, key = self.intent(payload)
        response = self.confirm(identifier, key, payload)
        self.assertEqual(response.status_code, 500)
        self.assertNotIn("detail-interne", response.content.decode())
        self.assert_no_effects(identifier)

    def test_database_error_rolls_back_every_write(self):
        payload = {**self.payload, "fail": "database"}
        identifier, key = self.intent(payload)
        response = self.confirm(identifier, key, payload)
        self.assertEqual(response.status_code, 503)
        self.assert_no_effects(identifier)

    def test_business_refusal_keeps_only_rejection_record_and_stable_result(self):
        payload = {**self.payload, "fail": "business"}
        identifier, key = self.intent(payload)
        first = self.confirm(identifier, key, payload)
        second = self.confirm(identifier, key, payload)
        self.assertEqual(first.status_code, 409)
        self.assertEqual(first.json(), second.json())
        self.assertEqual(RejectionRecord.objects.count(), 1)
        self.assertTrue(second.headers["Idempotency-Replayed"])
        self.assert_no_effects(identifier, state="REJECTED")

    def test_stale_version_refuses_new_intent_without_second_change(self):
        identifier, key = self.intent()
        self.assertEqual(self.confirm(identifier, key).status_code, 200)
        other_id, other_key = self.intent()
        response = self.confirm(other_id, other_key)
        self.assertEqual(response.json()["error"]["code"], "STALE_VERSION")
        self.assertEqual(Operation.objects.count(), 1)
        self.counter.refresh_from_db()
        self.assertEqual(self.counter.value, 3)

    def test_resolver_cannot_expire_confirmation_in_flight(self):
        payload = {**self.payload, "pause": True}
        identifier, key = self.intent(payload)
        client = self.make_client()

        def confirm():
            try:
                return self.confirm(identifier, key, payload, client=client).status_code
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(confirm)
            try:
                self.assertTrue(probe.entered.wait(timeout=5))
                self.assertEqual(read_intent(self.context, identifier).body["state"], "READY")
                start = time.monotonic()
                outcome = resolve_intent(self.context, identifier)
                self.assertEqual(outcome.status, 202)
                self.assertLess(time.monotonic() - start, 2)
            finally:
                probe.release.set()
            self.assertEqual(future.result(timeout=10), 200)
        self.assertEqual(resolve_intent(self.context, identifier).body["result"], "CONFIRMED")

    def test_resolver_winning_first_prevents_late_confirmation(self):
        identifier, key = self.intent()
        self.assertEqual(resolve_intent(self.context, identifier).status, 409)
        self.assertEqual(self.confirm(identifier, key).status_code, 409)
        self.assert_no_effects(identifier, state="EXPIRED")

    @override_settings(OPERATION_INTENT_TTL_SECONDS=1)
    def test_deadline_expiration_is_authoritative(self):
        identifier, key = self.intent()
        time.sleep(1.05)
        self.assertEqual(self.confirm(identifier, key).json()["error"]["code"], "INTENT_EXPIRED")
        self.assert_no_effects(identifier, state="EXPIRED")

    def test_backend_connection_interruption_rolls_back_and_allows_retry(self):
        payload = {**self.payload, "pause": True}
        identifier, key = self.intent(payload)
        client = self.make_client()

        def confirm():
            try:
                return self.confirm(identifier, key, payload, client=client).status_code
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(confirm)
            try:
                self.assertTrue(probe.entered.wait(timeout=5))
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_terminate_backend(%s)", [probe.backend_ids[0]])
                    self.assertTrue(cursor.fetchone()[0])
            finally:
                probe.release.set()
            self.assertEqual(future.result(timeout=10), 503)
        self.assert_no_effects(identifier)
        self.assertEqual(self.confirm(identifier, key, payload).status_code, 200)
        self.assertEqual(Operation.objects.count(), 1)

    def test_permission_checks_do_not_trust_role_or_actor_headers(self):
        response = Client().post(
            "/api/v1/operation-intents",
            data="{}",
            content_type="application/json",
            headers={"X-Role": "ADMIN", "X-Actor-ID": str(self.actor.pk)},
        )
        self.assertEqual(response.status_code, 401)
        admin = User.objects.create_user(
            "admin-fictif",
            role=User.Role.ADMIN,
            display_name="Gestionnaire fictive",
            must_change_password=False,
        )
        response = self.make_client(admin).post(
            "/api/v1/operation-intents",
            content_type="application/json",
            data=json.dumps(
                {
                    "intent_id": str(uuid.uuid4()),
                    "key": uuid.uuid4().hex,
                    "payload_hash": payload_hash(self.payload),
                    "command_type": probe.COMMAND.name,
                    "resource_id": str(self.counter.pk),
                }
            ),
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(OperationIntent.objects.count(), 0)

    def test_other_owner_cannot_read_or_resolve_an_intent(self):
        identifier, _ = self.intent()
        other = User.objects.create_user(
            "autre-fictif",
            role=User.Role.REPLENISHER,
            display_name="Autre fictif",
            must_change_password=False,
        )
        client = self.make_client(other)
        self.assertEqual(client.get(f"/api/v1/operation-intents/{identifier}").status_code, 404)
        self.assertEqual(
            client.post(f"/api/v1/operation-intents/{identifier}/resolve").status_code, 404
        )

    def test_disabled_account_and_stale_authorization_context_are_rejected(self):
        identifier, key = self.intent()
        User.objects.filter(pk=self.actor.pk).update(auth_version=2)
        with self.assertRaises(BusinessError) as caught:
            execute_intent(self.context, intent_id=identifier, key=key, payload=self.payload)
        self.assertEqual(caught.exception.status, 401)
        User.objects.filter(pk=self.actor.pk).update(is_active=False)
        self.assertEqual(self.confirm(identifier, key).status_code, 401)
        self.assert_no_effects(identifier)

    def test_password_change_required_blocks_operation(self):
        identifier, key = self.intent()
        User.objects.filter(pk=self.actor.pk).update(must_change_password=True)
        self.assertEqual(self.confirm(identifier, key).status_code, 403)
        self.assert_no_effects(identifier)

    def test_generation_change_blocks_old_intent(self):
        identifier, key = self.intent()
        with override_settings(OPERATION_GENERATION="new-test-generation"):
            self.assertEqual(self.confirm(identifier, key).status_code, 409)
        self.assert_no_effects(identifier)

    def test_default_deny_and_missing_business_routes(self):
        self.assertEqual(self.client.get("/_test/undeclared").status_code, 403)
        # M5 ouvre la file opérationnelle R, sans donner le droit de créer des demandes A.
        self.assertEqual(self.client.get("/api/v1/auth/login").status_code, 405)
        self.assertEqual(self.client.get("/api/v1/requests").status_code, 200)
        for route in ["/admin/"]:
            self.assertEqual(self.client.get(route).status_code, 404)

    def test_csrf_and_invalid_json_are_rejected(self):
        identifier, key = self.intent()
        client = self.make_client(csrf=True)
        self.assertEqual(self.confirm(identifier, key, client=client).status_code, 403)
        for text in ['{"delta":1,"delta":2}', '{"delta":NaN}', "[]", "not-json"]:
            response = self.client.post(
                f"/_test/confirm/{identifier}",
                data=text,
                content_type="application/json",
                headers={"Idempotency-Key": key},
            )
            self.assertEqual(response.status_code, 400, text)
        self.assert_no_effects(identifier)

    def test_unknown_commands_are_not_dynamically_imported(self):
        response = self.client.post(
            "/api/v1/operation-intents",
            data=json.dumps(
                {
                    "intent_id": str(uuid.uuid4()),
                    "key": uuid.uuid4().hex,
                    "payload_hash": payload_hash(self.payload),
                    "command_type": "os.system",
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(OperationIntent.objects.count(), 0)

    def test_append_only_facts_are_protected_even_from_direct_sql(self):
        identifier, key = self.intent()
        self.assertEqual(self.confirm(identifier, key).status_code, 200)
        for table in [
            "core_operation",
            "audit_auditevent",
            "notifications_businessevent",
            "notifications_outbox",
        ]:
            with self.subTest(table=table), self.assertRaises(IntegrityError), transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute(
                        psycopg.sql.SQL("DELETE FROM {}").format(psycopg.sql.Identifier(table))
                    )
        with self.assertRaises(IntegrityError), transaction.atomic():
            OperationIntent.objects.filter(pk=identifier).update(state="READY", response=None)

    def test_commit_requires_operation_audit_event_and_outbox(self):
        for missing in ["audit", "event", "outbox"]:
            identifier, _ = self.intent()
            with (
                self.subTest(missing=missing),
                self.assertRaises(IntegrityError),
                transaction.atomic(),
            ):
                operation = Operation.objects.create(
                    intent_id=identifier,
                    actor=self.actor,
                    actor_role=self.actor.role,
                    command_type=probe.COMMAND.name,
                    resource_id=self.counter.pk,
                    correlation_id=uuid.uuid4(),
                )
                if missing != "audit":
                    AuditEvent.objects.create(operation=operation)
                if missing != "event":
                    event = BusinessEvent.objects.create(
                        operation=operation,
                        event_type="test.confirmed",
                        aggregate_id=self.counter.pk,
                    )
                    if missing != "outbox":
                        Outbox.objects.create(event=event, dedupe_key=uuid.uuid4().hex)
                OperationIntent.objects.filter(pk=identifier).update(
                    state="CONFIRMED",
                    completed_at=timezone.now(),
                    http_status=200,
                    response={
                        "operation_id": str(operation.pk),
                        "intent_id": str(identifier),
                        "result": "CONFIRMED",
                    },
                )
        self.assertEqual(Operation.objects.count(), 0)

    def test_sql_constraints_reject_invalid_states_and_duplicate_admin(self):
        identifier, _ = self.intent()
        with self.assertRaises(IntegrityError), transaction.atomic():
            OperationIntent.objects.filter(pk=identifier).update(
                state="CONFIRMED", response={}, http_status=None, completed_at=timezone.now()
            )
        for name in ["admin-a-fictif", "admin-b-fictif"]:
            if name.endswith("a-fictif"):
                User.objects.create_user(name, role="ADMIN")
            else:
                with self.assertRaises(IntegrityError), transaction.atomic():
                    User.objects.create_user(name, role="ADMIN")
        with self.assertRaises(IntegrityError), transaction.atomic():
            User.objects.create_user("demandeur — profil A-sans-etage", role="ASSISTANT")

    def test_sql_runtime_role_can_execute_but_cannot_modify_audit_or_schema(self):
        role = os.environ["RUNTIME_DATABASE_USER"]
        password = os.environ["RUNTIME_DATABASE_PASSWORD"]
        grant_runtime(role)
        with connection.cursor() as cursor:
            cursor.execute("SELECT has_schema_privilege(%s, 'public', 'CREATE')", [role])
            self.assertFalse(cursor.fetchone()[0])
            cursor.execute(
                psycopg.sql.SQL("GRANT SELECT, UPDATE ON probes_counter TO {}").format(
                    psycopg.sql.Identifier(role)
                )
            )
        identifier, key = self.intent()
        original = connection.settings_dict.copy()
        connection.close()
        try:
            connection.settings_dict.update(USER=role, PASSWORD=password)
            outcome = execute_intent(
                self.context, intent_id=identifier, key=key, payload=self.payload
            )
            self.assertEqual(outcome.status, 200)
            for statement in [
                "DELETE FROM audit_auditevent",
                "TRUNCATE audit_auditevent",
                "CREATE TABLE intrusion(id int)",
            ]:
                with (
                    self.subTest(statement=statement),
                    self.assertRaises(DatabaseError),
                    transaction.atomic(),
                ):
                    with connection.cursor() as cursor:
                        cursor.execute(statement)
        finally:
            connection.close()
            connection.settings_dict.clear()
            connection.settings_dict.update(original)

    def test_canonical_payload_hash_and_sensitive_audit_fields(self):
        self.assertEqual(payload_hash({"a": 1, "b": 2}), payload_hash({"b": 2, "a": 1}))
        from apps.core.payloads import safe_record

        with self.assertRaises(ValueError):
            safe_record({"password": "must-not-be-persisted"})

    def test_production_registry_and_routes_do_not_expose_test_commands(self):
        from config.urls import urlpatterns

        self.assertTrue(all("_test" not in str(item.pattern) for item in urlpatterns))
        self.assertFalse(
            any(
                "probes" in name
                for name in __import__(
                    "config.settings.base", fromlist=["INSTALLED_APPS"]
                ).INSTALLED_APPS
            )
        )

    def test_audit_or_outbox_failure_rolls_back_business_effect(self):
        for target in [
            "apps.core.idempotency.AuditEvent.objects.create",
            "apps.core.idempotency.Outbox.objects.create",
        ]:
            identifier, key = self.intent()
            with patch(target, side_effect=RuntimeError("fault-injection")):
                response = self.confirm(identifier, key)
            self.assertEqual(response.status_code, 500)
            self.assert_no_effects(identifier)

    def test_failure_while_recording_refusal_does_not_persist_false_terminal_result(self):
        def broken_record(execution, resources, error):
            RejectionRecord.objects.create(intent_id=execution.intent.pk, code=error.code)
            raise RuntimeError("rejection-record-failure")

        commands._registry[probe.COMMAND.name] = replace(
            probe.COMMAND, record_rejection=broken_record
        )
        payload = {**self.payload, "fail": "business"}
        identifier, key = self.intent(payload)
        self.assertEqual(self.confirm(identifier, key, payload).status_code, 500)
        self.assert_no_effects(identifier)
        self.assertEqual(RejectionRecord.objects.count(), 0)

    def test_uuid_rebinding_and_metadata_rewrites_are_refused(self):
        identifier, _ = self.intent()
        with self.assertRaises(BusinessError):
            register_intent(
                self.context,
                intent_id=identifier,
                key=uuid.uuid4().hex,
                fingerprint=payload_hash(self.payload),
                command_type=probe.COMMAND.name,
                resource_id=self.counter.pk,
            )
        with self.assertRaises(IntegrityError), transaction.atomic():
            OperationIntent.objects.filter(pk=identifier).update(payload_hash="f" * 64)
        with self.assertRaises(IntegrityError), transaction.atomic():
            OperationIntent.objects.filter(pk=identifier).delete()

    def test_null_terminal_http_status_is_rejected_for_all_terminal_states(self):
        for state in ["CONFIRMED", "REJECTED", "EXPIRED"]:
            identifier, _ = self.intent()
            with self.subTest(state=state), self.assertRaises(IntegrityError), transaction.atomic():
                OperationIntent.objects.filter(pk=identifier).update(
                    state=state, response={}, http_status=None, completed_at=timezone.now()
                )

    def test_technical_lock_timeout_keeps_competing_intent_ready(self):
        paused = {**self.payload, "pause": True}
        first_id, first_key = self.intent(paused)
        second_id, second_key = self.intent()
        client = self.make_client()

        def worker():
            try:
                return self.confirm(first_id, first_key, paused, client=client).status_code
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(worker)
            try:
                self.assertTrue(probe.entered.wait(timeout=5))
                with override_settings(OPERATION_LOCK_TIMEOUT_MS=50):
                    response = self.confirm(second_id, second_key)
                self.assertEqual(response.status_code, 503)
                self.assertEqual(OperationIntent.objects.get(pk=second_id).state, "READY")
            finally:
                probe.release.set()
            self.assertEqual(future.result(timeout=10), 200)
        self.assertEqual(Operation.objects.count(), 1)

    def test_registration_route_validates_binding_and_http_contract(self):
        identifier = uuid.uuid4()
        body = {
            "intent_id": str(identifier),
            "key": uuid.uuid4().hex,
            "payload_hash": payload_hash(self.payload),
            "command_type": probe.COMMAND.name,
            "resource_id": str(self.counter.pk),
        }
        response = self.client.post(
            "/api/v1/operation-intents", data=json.dumps(body), content_type="application/json"
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(
            self.client.get(f"/api/v1/operation-intents/{identifier}").json()["state"], "READY"
        )
        self.assertEqual(
            self.client.post(f"/api/v1/operation-intents/{identifier}/resolve").status_code, 409
        )
        for change in [
            {"key": "short"},
            {"intent_id": "bad"},
            {"payload_hash": "x"},
            {"role": "ADMIN"},
            {"command_type": []},
            {"command_type": {}},
            {"command_type": None},
            {"command_type": 1},
        ]:
            response = self.client.post(
                "/api/v1/operation-intents",
                data=json.dumps({**body, **change}),
                content_type="application/json",
            )
            self.assertEqual(response.status_code, 400)
        self.assertEqual(self.client.get("/api/v1/operation-intents").status_code, 405)
        self.assertEqual(self.client.post("/api/v1/operation-intents", data={}).status_code, 400)

    def test_json_telemetry_correlates_failure_without_sensitive_payload(self):
        from apps.core.logging import JsonFormatter

        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(JsonFormatter())
        logger = logging.getLogger("reserve")
        logger.addHandler(handler)
        payload = {**self.payload, "fail": "technical", "password": "fictive-sensitive-marker"}
        identifier, key = self.intent(payload)
        try:
            response = self.confirm(identifier, key, payload)
        finally:
            logger.removeHandler(handler)
        records = [json.loads(line) for line in stream.getvalue().splitlines()]
        self.assertEqual({r["event"] for r in records}, {"request.failed", "http.completed"})
        self.assertTrue(
            all(r["correlation_id"] == response.headers["X-Correlation-ID"] for r in records)
        )
        self.assertNotIn("fictive-sensitive-marker", stream.getvalue())
        self.assertNotIn("detail-interne", stream.getvalue())
        completed = next(r for r in records if r["event"] == "http.completed")
        self.assertEqual(completed["status"], 500)
        self.assertNotIn(str(identifier), completed["route"])
        self.assertGreaterEqual(completed["duration_ms"], 0)

    def test_test_configuration_refuses_external_or_unsafe_database_names(self):
        for override in [
            {"DATABASE_HOST": "external.invalid"},
            {"TEST_DATABASE_NAME": "stock_app_example"},
        ]:
            # config.settings.test remplace DATABASE_USER en mémoire par le rôle
            # isolé de test. Les imports enfants doivent repartir du rôle owner
            # configuré pour évaluer les autres garde-fous indépendamment.
            env = {
                **os.environ,
                "DATABASE_USER": settings.APPLICATION_DATABASE_USER,
                **override,
            }
            result = subprocess.run(  # noqa: S603
                [sys.executable, "-c", "import config.settings.test"],
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("base de test locale distincte", result.stderr)

    def test_test_configuration_refuses_reusing_the_test_role_as_owner(self):
        env = {**os.environ, "DATABASE_USER": settings.TEST_DATABASE_USER}
        result = subprocess.run(  # noqa: S603
            [sys.executable, "-c", "import config.settings.test"],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("test role must differ", result.stderr)

    def test_production_does_not_fall_back_to_local_secrets(self):
        env = {k: v for k, v in os.environ.items() if k != "DJANGO_SECRET_KEY"}
        result = subprocess.run(  # noqa: S603
            [sys.executable, "-c", "import config.settings.production"],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("DJANGO_SECRET_KEY", result.stderr)


class LostResponseTests(OperationFixtures, LiveServerTestCase):
    def test_lost_real_http_response_is_recovered_without_second_operation(self):
        identifier, key = self.intent()
        # Cookie de session fictive + CSRF valide, sans développer le login M1.
        from django.middleware.csrf import get_token
        from django.test import RequestFactory

        request = RequestFactory().get("/")
        csrf_token = get_token(request)
        cookie = "; ".join(
            [
                f"{settings.SESSION_COOKIE_NAME}={self.client.cookies[settings.SESSION_COOKIE_NAME].value}",
                f"csrftoken={request.META['CSRF_COOKIE']}",
            ]
        )
        body = json.dumps(self.payload).encode()
        host, port = self.server_thread.host, self.server_thread.port
        raw = (
            f"POST /_test/confirm/{identifier} HTTP/1.1\r\nHost: localhost:{port}\r\n"
            f"Cookie: {cookie}\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\n"
            f"X-CSRFToken: {csrf_token}\r\nIdempotency-Key: {key}\r\nConnection: close\r\n\r\n"
        ).encode() + body
        with socket.create_connection((host, port), timeout=5) as wire:
            wire.sendall(raw)
            self.assertTrue(probe.committed.wait(timeout=5))
            # Aucun recv : la réponse du vrai serveur HTTP est abandonnée après commit.
        result = read_intent(self.context, identifier)
        self.assertEqual(result.body["state"], "CONFIRMED")
        replay = self.confirm(identifier, key)
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay.json()["operation_id"], result.body["response"]["operation_id"])
        self.assertEqual(Operation.objects.count(), 1)
        self.counter.refresh_from_db()
        self.assertEqual(self.counter.value, 3)
