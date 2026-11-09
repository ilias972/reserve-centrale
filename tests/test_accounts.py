"""Recette M1 sur PostgreSQL réel, comptes exclusivement fictifs."""

import json
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from unittest.mock import patch

from django.conf import settings
from django.db import DatabaseError, IntegrityError, close_old_connections, connection, transaction
from django.test import Client, TransactionTestCase, override_settings
from django.utils import timezone

from apps.accounts.models import AccountGuard, LoginThrottle, User, UserSession
from apps.accounts.operations import ensure_registered, temporary_access
from apps.accounts.services import bootstrap_admin
from apps.audit.models import AuditEvent
from apps.catalog.models import Floor
from apps.core import commands, idempotency
from apps.core.errors import BusinessError
from apps.core.management.commands.grant_runtime import grant_runtime
from apps.core.models import Operation, OperationIntent
from apps.core.payloads import payload_hash
from apps.core.policies import ActorContext
from apps.notifications.models import BusinessEvent, Outbox

FICTIVE_PASSWORD = "Une phrase fictive longue 2026 !"
SECOND_PASSWORD = "Autre phrase fictive nouvelle 2026 !"


class AccountFixtures:
    def tearDown(self):
        commands._registry.pop("m0.test.counter", None)
        super().tearDown()

    def setUp(self):
        super().setUp()
        ensure_registered()
        AccountGuard.objects.get_or_create(pk=1)
        self.floor = Floor.objects.create(code="FICTIF-A", name="Unité fictif A")
        self.other_floor = Floor.objects.create(code="FICTIF-B", name="Unité fictif B")
        self.admin = User.objects.create_user(
            "gestionnaire-fictif",
            FICTIVE_PASSWORD,
            display_name="Gestionnaire fictive",
            role="ADMIN",
            must_change_password=False,
        )
        AccountGuard.objects.filter(pk=1).update(initialized=True)
        self.replenisher = User.objects.create_user(
            "reappro-fictif",
            FICTIVE_PASSWORD,
            display_name="Réappro fictif",
            role="REPLENISHER",
            must_change_password=False,
        )
        self.assistant = User.objects.create_user(
            "demandeur-a-fictif",
            FICTIVE_PASSWORD,
            display_name="Demandeur A fictive",
            role="ASSISTANT",
            floor=self.floor,
            must_change_password=False,
        )
        self.admin_client = self.logged(self.admin)
        self.user_client = self.logged(self.assistant)

    def logged(self, user):
        client = Client(raise_request_exception=False)
        client.force_login(user)
        return client

    def post_json(self, path, body, client=None, *, method="post", identifier=None, key=None):
        return getattr(client or self.admin_client, method)(
            path,
            data=json.dumps(body),
            content_type="application/json",
            headers={
                "Operation-Intent": str(identifier or uuid.uuid4()),
                "Idempotency-Key": key or uuid.uuid4().hex,
            },
        )

    def create_account(self, **values):
        return self.post_json(
            "/api/v1/users",
            {
                "username": "nouveau-fictif",
                "display_name": "Nouveau fictif",
                "role": "REPLENISHER",
                **values,
            },
        )

    def update(self, user, body, **kwargs):
        return self.post_json(
            f"/api/v1/users/{user.pk}",
            {"expected_version": user.version, **body},
            method="patch",
            **kwargs,
        )

    def reset(self, user, **body):
        return self.post_json(
            f"/api/v1/users/{user.pk}/reset-password",
            {"expected_version": user.version, "identity_verified": True, **body},
        )


class AccountTests(AccountFixtures, TransactionTestCase):
    def test_admin_creates_temporary_access_once_with_audit_and_outbox(self):
        identifier, key = uuid.uuid4(), uuid.uuid4().hex
        body = {
            "username": "nouveau-fictif",
            "display_name": "Nouveau fictif",
            "role": "ASSISTANT",
            "floor_id": str(self.floor.pk),
        }
        first = self.post_json("/api/v1/users", body, identifier=identifier, key=key)
        self.assertEqual(first.status_code, 201, first.content)
        secret = first.json()["one_time"]["temporary_password"]
        user = User.objects.get(username=body["username"])
        self.assertTrue(user.check_password(secret))
        self.assertTrue(user.password.startswith("argon2$argon2id$"))
        self.assertTrue(user.must_change_password)
        self.assertGreater(user.temporary_expires_at, timezone.now())
        self.assertEqual(user.floor_id, self.floor.pk)
        replay = self.post_json("/api/v1/users", body, identifier=identifier, key=key)
        self.assertEqual(replay.status_code, 201)
        self.assertNotIn("one_time", replay.json())
        self.assertEqual(Operation.objects.filter(command_type="accounts.create").count(), 1)
        for model in [OperationIntent, AuditEvent, BusinessEvent, Outbox]:
            self.assertNotIn(secret, str(list(model.objects.values())))
        self.assertEqual(AuditEvent.objects.count(), Operation.objects.count())
        self.assertEqual(Outbox.objects.count(), BusinessEvent.objects.count())

    def test_valid_invalid_and_unknown_login_have_generic_response(self):
        client = Client(raise_request_exception=False)
        invalid = self.post_json(
            "/api/v1/auth/login",
            {"identifier": self.assistant.username, "password": "incorrect"},
            client,
        )
        unknown = self.post_json(
            "/api/v1/auth/login", {"identifier": "inconnu-fictif", "password": "incorrect"}, client
        )
        self.assertEqual(invalid.status_code, 401)
        self.assertEqual(invalid.json()["error"]["message"], unknown.json()["error"]["message"])
        success = self.post_json(
            "/api/v1/auth/login",
            {"identifier": "DEMANDEUR-A-FICTIF", "password": FICTIVE_PASSWORD, "persistent": True},
            client,
        )
        self.assertEqual(success.status_code, 200, success.content)
        self.assertIn("max-age", client.cookies[settings.SESSION_COOKIE_NAME])
        self.assertEqual(client.get("/api/v1/me").status_code, 200)

    def test_temporary_session_is_restricted_until_valid_change_and_rotated(self):
        created = self.create_account()
        temporary = created.json()["one_time"]["temporary_password"]
        client = Client(raise_request_exception=False)
        self.assertEqual(
            self.post_json(
                "/api/v1/auth/login",
                {"identifier": "nouveau-fictif", "password": temporary},
                client,
            ).status_code,
            200,
        )
        old_cookie = client.cookies[settings.SESSION_COOKIE_NAME].value
        for path in [
            "/api/v1/me",
            "/api/v1/users",
            "/api/v1/auth/sessions",
            "/api/v1/operation-intents",
        ]:
            self.assertEqual(client.get(path).status_code, 403)
        self.assertEqual(client.get("/mon-compte").url, "/mot-de-passe")
        wrong = self.post_json(
            "/api/v1/auth/change-password",
            {"current_password": "bad", "new_password": SECOND_PASSWORD},
            client,
        )
        self.assertEqual(wrong.status_code, 400)
        changed = self.post_json(
            "/api/v1/auth/change-password",
            {"current_password": temporary, "new_password": SECOND_PASSWORD},
            client,
        )
        self.assertEqual(changed.status_code, 200, changed.content)
        self.assertNotEqual(old_cookie, client.cookies[settings.SESSION_COOKIE_NAME].value)
        self.assertEqual(client.get("/api/v1/me").status_code, 200)
        old = Client()
        old.cookies[settings.SESSION_COOKIE_NAME] = old_cookie
        self.assertEqual(old.get("/api/v1/me").status_code, 401)

    def test_expired_temporary_password_cannot_login(self):
        temporary = temporary_access(self.replenisher)
        self.replenisher.temporary_expires_at = timezone.now() - timedelta(seconds=1)
        self.replenisher.save()
        response = self.post_json(
            "/api/v1/auth/login",
            {"identifier": self.replenisher.username, "password": temporary},
            Client(),
        )
        self.assertEqual(response.status_code, 401)

    def test_persistent_cookie_survives_new_client_and_nonpersistent_has_no_expiry(self):
        client = Client()
        self.post_json(
            "/api/v1/auth/login",
            {
                "identifier": self.assistant.username,
                "password": FICTIVE_PASSWORD,
                "persistent": True,
            },
            client,
        )
        cookie = client.cookies[settings.SESSION_COOKIE_NAME]
        self.assertGreater(int(cookie["max-age"]), 86400)
        self.assertTrue(cookie["httponly"])
        self.assertEqual(cookie["samesite"], "Lax")
        reopened = Client()
        reopened.cookies[settings.SESSION_COOKIE_NAME] = cookie.value
        self.assertEqual(reopened.get("/api/v1/me").status_code, 200)
        shared = Client()
        self.post_json(
            "/api/v1/auth/login",
            {
                "identifier": self.replenisher.username,
                "password": FICTIVE_PASSWORD,
                "persistent": False,
            },
            shared,
        )
        self.assertEqual(shared.cookies[settings.SESSION_COOKIE_NAME]["expires"], "")

    def test_disable_reactivate_does_not_resurrect_old_session(self):
        old_cookie = self.user_client.cookies[settings.SESSION_COOKIE_NAME].value
        disabled = self.update(self.assistant, {"is_active": False})
        self.assertEqual(disabled.status_code, 200, disabled.content)
        self.assertEqual(self.user_client.get("/api/v1/me").status_code, 401)
        self.assistant.refresh_from_db()
        self.assertTrue(
            UserSession.objects.filter(user=self.assistant, revoked_at__isnull=False).exists()
        )
        self.assertEqual(self.update(self.assistant, {"is_active": True}).status_code, 200)
        reopened = Client()
        reopened.cookies[settings.SESSION_COOKIE_NAME] = old_cookie
        self.assertEqual(reopened.get("/api/v1/me").status_code, 401)
        self.assertEqual(
            self.post_json(
                "/api/v1/auth/login",
                {"identifier": self.assistant.username, "password": FICTIVE_PASSWORD},
                reopened,
            ).status_code,
            200,
        )

    def test_reset_requires_identity_and_revokes_password_and_all_sessions(self):
        second = self.logged(self.assistant)
        self.assertEqual(self.reset(self.assistant, identity_verified=False).status_code, 400)
        result = self.reset(self.assistant)
        self.assertEqual(result.status_code, 200, result.content)
        temporary = result.json()["one_time"]["temporary_password"]
        for client in [self.user_client, second]:
            self.assertEqual(client.get("/api/v1/me").status_code, 401)
        client = Client()
        self.assertEqual(
            self.post_json(
                "/api/v1/auth/login",
                {"identifier": self.assistant.username, "password": FICTIVE_PASSWORD},
                client,
            ).status_code,
            401,
        )
        self.assertEqual(
            self.post_json(
                "/api/v1/auth/login",
                {"identifier": self.assistant.username, "password": temporary},
                client,
            ).status_code,
            200,
        )

    def test_role_and_floor_changes_revoke_sessions(self):
        self.assertEqual(
            self.update(self.assistant, {"floor_id": str(self.other_floor.pk)}).status_code, 200
        )
        self.assertEqual(self.user_client.get("/api/v1/me").status_code, 401)
        self.assistant.refresh_from_db()
        client = self.logged(self.assistant)
        self.assertEqual(
            self.update(self.assistant, {"role": "REPLENISHER", "floor_id": None}).status_code, 200
        )
        self.assertEqual(client.get("/api/v1/me").status_code, 401)

    def test_assistant_and_replenisher_cannot_manage_accounts_even_by_direct_api(self):
        for user in [self.assistant, self.replenisher]:
            client = self.logged(user)
            self.assertEqual(client.get("/administration/utilisateurs").status_code, 403)
            self.assertEqual(client.get("/api/v1/users").status_code, 403)
            for path, method, body in [
                (
                    "/api/v1/users",
                    "post",
                    {"username": "forged", "display_name": "Forgé", "role": "ADMIN"},
                ),
                (
                    f"/api/v1/users/{self.admin.pk}",
                    "patch",
                    {"expected_version": 0, "is_active": False},
                ),
                (
                    f"/api/v1/users/{self.admin.pk}/reset-password",
                    "post",
                    {"expected_version": 0, "identity_verified": True},
                ),
            ]:
                response = self.post_json(path, body, client, method=method)
                self.assertEqual(response.status_code, 403, response.content)
        self.assertFalse(Operation.objects.exists())
        self.assertEqual(User.objects.count(), 3)

    def test_active_admin_unique_and_last_admin_protected_in_api_and_sql(self):
        self.assertEqual(self.create_account(role="ADMIN").status_code, 409)
        self.assertEqual(self.update(self.admin, {"is_active": False}).status_code, 409)
        self.assertEqual(self.update(self.admin, {"role": "REPLENISHER"}).status_code, 409)
        with self.assertRaises(IntegrityError), transaction.atomic():
            User.objects.create_user("autre-n-fictive", role="ADMIN")
        with self.assertRaises(IntegrityError), transaction.atomic():
            User.objects.filter(pk=self.admin.pk).update(is_active=False)
        with self.assertRaises(IntegrityError), transaction.atomic():
            AccountGuard.objects.filter(pk=1).update(initialized=False)
        self.assertEqual(User.objects.filter(role="ADMIN", is_active=True).count(), 1)
        with self.assertRaises(ValueError):
            User.objects.create_superuser("hidden", FICTIVE_PASSWORD)

    def test_floor_assignment_and_unknown_fields_are_validated(self):
        self.assertEqual(self.create_account(role="ASSISTANT").status_code, 400)
        self.assertEqual(self.create_account(floor_id=str(self.floor.pk)).status_code, 400)
        self.other_floor.active = False
        self.other_floor.save()
        self.assertEqual(
            self.create_account(role="ASSISTANT", floor_id=str(self.other_floor.pk)).status_code,
            400,
        )
        self.assertEqual(self.create_account(is_superuser=True).status_code, 400)
        self.assertEqual(self.create_account().status_code, 201)
        self.assertEqual(self.create_account(username="NOUVEAU-FICTIF").status_code, 409)

    def test_recent_admin_authentication_is_required_and_can_be_renewed(self):
        meta = UserSession.objects.get(user=self.admin, revoked_at__isnull=True)
        meta.authenticated_at = timezone.now() - timedelta(minutes=20)
        meta.save()
        self.assertEqual(self.create_account().status_code, 403)
        self.assertEqual(
            self.post_json(
                "/api/v1/auth/reauthenticate", {"password": FICTIVE_PASSWORD}
            ).status_code,
            200,
        )
        self.assertEqual(self.create_account().status_code, 201)

    def test_idle_absolute_and_generation_expiry_are_enforced(self):
        for field in ["idle_expires_at", "absolute_expires_at", "generation_id"]:
            client = self.logged(self.assistant)
            meta = UserSession.objects.get(
                key_digest=__import__("hashlib")
                .sha256(client.cookies[settings.SESSION_COOKIE_NAME].value.encode())
                .hexdigest()
            )
            if field == "generation_id":
                meta.generation_id = "old-generation"
            else:
                # Pour conserver les contraintes SQL de dates, expirer les deux.
                meta.created_at = timezone.now() - timedelta(days=3)
                meta.idle_expires_at = timezone.now() - timedelta(days=2)
                meta.absolute_expires_at = timezone.now() - timedelta(days=1)
            meta.save()
            self.assertEqual(
                client.get("/api/v1/me").status_code, 409 if field == "generation_id" else 401
            )

    def test_logout_and_individual_revoke_invalidate_replayed_cookie(self):
        cookie = self.user_client.cookies[settings.SESSION_COOKIE_NAME].value
        self.assertEqual(
            self.post_json("/api/v1/auth/logout", {}, self.user_client).status_code, 200
        )
        stale = Client()
        stale.cookies[settings.SESSION_COOKIE_NAME] = cookie
        self.assertEqual(stale.get("/api/v1/me").status_code, 401)
        other = self.logged(self.assistant)
        target = UserSession.objects.filter(user=self.assistant, revoked_at__isnull=True).latest(
            "created_at"
        )
        self.assertEqual(
            self.post_json(f"/api/v1/auth/sessions/{target.pk}/revoke", {}).status_code, 200
        )
        self.assertEqual(other.get("/api/v1/me").status_code, 401)
        self.assertEqual(self.admin_client.get("/api/v1/me").status_code, 200)

    def test_cannot_view_or_revoke_another_users_session(self):
        target = UserSession.objects.get(user=self.admin)
        self.assertEqual(
            self.user_client.get(f"/api/v1/auth/sessions?user_id={self.admin.pk}").status_code, 404
        )
        self.assertEqual(
            self.post_json(
                f"/api/v1/auth/sessions/{target.pk}/revoke", {}, self.user_client
            ).status_code,
            404,
        )
        response = self.user_client.get("/api/v1/auth/sessions")
        self.assertNotIn("key_digest", response.content.decode())
        self.assertNotIn(
            self.user_client.cookies[settings.SESSION_COOKIE_NAME].value, response.content.decode()
        )

    def test_csrf_login_logout_admin_and_no_public_signup(self):
        client = Client(enforce_csrf_checks=True)
        self.assertEqual(
            self.post_json(
                "/api/v1/auth/login",
                {"identifier": self.admin.username, "password": FICTIVE_PASSWORD},
                client,
            ).status_code,
            403,
        )
        client.force_login(self.admin)
        self.assertEqual(self.post_json("/api/v1/users", {}, client).status_code, 403)
        self.assertEqual(client.get("/deconnexion").status_code, 405)
        # Le catalogue et la supervision M4 sont maintenant disponibles pour Gestionnaire.
        self.assertEqual(client.get("/api/v1/products").status_code, 200)
        self.assertEqual(client.get("/api/v1/requests").status_code, 200)
        for route in [
            "/inscription",
            "/api/v1/auth/register",
            "/api/v1/auth/forgot-password",
        ]:
            self.assertEqual(client.get(route).status_code, 404)

    @override_settings(LOGIN_FAILURE_LIMIT=2)
    def test_shared_database_throttle_persists_and_is_not_permanent(self):
        body = {"identifier": self.assistant.username, "password": "wrong"}
        for _ in range(2):
            self.assertEqual(self.post_json("/api/v1/auth/login", body, Client()).status_code, 401)
        self.assertEqual(self.post_json("/api/v1/auth/login", body, Client()).status_code, 429)
        self.assertEqual(LoginThrottle.objects.count(), 2)
        LoginThrottle.objects.update(blocked_until=timezone.now() - timedelta(seconds=1))
        self.assertEqual(
            self.post_json(
                "/api/v1/auth/login", {**body, "password": FICTIVE_PASSWORD}, Client()
            ).status_code,
            200,
        )

    def test_password_policy_rejects_short_common_numeric_and_unchanged(self):
        for password in ["court", "12345678901234567890", FICTIVE_PASSWORD]:
            response = self.post_json(
                "/api/v1/auth/change-password",
                {"current_password": FICTIVE_PASSWORD, "new_password": password},
                self.user_client,
            )
            self.assertEqual(response.status_code, 400)
        self.assertEqual(self.user_client.get("/api/v1/me").status_code, 200)

    def test_technical_failure_rolls_back_creation_and_no_secret_is_replayable(self):
        count = User.objects.count()
        with patch(
            "apps.core.idempotency.Outbox.objects.create", side_effect=RuntimeError("fictive-error")
        ):
            result = self.create_account()
        self.assertEqual(result.status_code, 500)
        self.assertEqual(User.objects.count(), count)
        self.assertFalse(Operation.objects.exists())
        self.assertEqual(OperationIntent.objects.get().state, "READY")

    def test_reset_lost_response_replay_has_no_secret_and_new_reset_supersedes(self):
        identifier, key = uuid.uuid4(), uuid.uuid4().hex
        path = f"/api/v1/users/{self.assistant.pk}/reset-password"
        body = {"expected_version": 0, "identity_verified": True}
        first = self.post_json(path, body, identifier=identifier, key=key)
        old = first.json()["one_time"]["temporary_password"]
        replay = self.post_json(path, body, identifier=identifier, key=key)
        self.assertEqual(replay.status_code, 200)
        self.assertNotIn("one_time", replay.json())
        self.assertEqual(Operation.objects.filter(command_type="accounts.reset").count(), 1)
        self.assistant.refresh_from_db()
        next_result = self.reset(self.assistant)
        self.assertNotEqual(old, next_result.json()["one_time"]["temporary_password"])
        self.assistant.refresh_from_db()
        self.assertFalse(self.assistant.check_password(old))

    def test_runtime_sql_permissions_allow_account_services_but_not_guard_bypass(self):
        grant_runtime(os.environ["RUNTIME_DATABASE_USER"])
        original = connection.settings_dict.copy()
        connection.close()
        connection.settings_dict.update(
            USER=os.environ["RUNTIME_DATABASE_USER"],
            PASSWORD=os.environ["RUNTIME_DATABASE_PASSWORD"],
        )
        try:
            result = self.create_account()
            self.assertEqual(result.status_code, 201, result.content)
            for statement in [
                "DELETE FROM accounts_user",
                "UPDATE accounts_accountguard SET initialized=false",
                "TRUNCATE accounts_accountguard",
                "DELETE FROM accounts_usersession",
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

    def test_concurrent_permission_edits_only_one_version_wins(self):
        cookie = self.admin_client.cookies[settings.SESSION_COOKIE_NAME].value
        barrier = threading.Barrier(2)
        target_id = self.assistant.pk

        def worker(floor):
            close_old_connections()
            client = Client(raise_request_exception=False)
            client.cookies[settings.SESSION_COOKIE_NAME] = cookie
            try:
                barrier.wait(timeout=5)
                response = self.post_json(
                    f"/api/v1/users/{target_id}",
                    {"expected_version": 0, "floor_id": str(floor)},
                    client,
                    method="patch",
                )
                return response.status_code
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(worker, floor) for floor in [self.floor.pk, self.other_floor.pk]]
            statuses = [future.result(timeout=15) for future in futures]
        self.assertEqual(sorted(statuses), [200, 409])
        self.assistant.refresh_from_db()
        self.assertEqual(self.assistant.version, 1)
        self.assertEqual(Operation.objects.filter(command_type="accounts.update").count(), 1)

    def test_server_rechecks_session_after_request_validation(self):
        # Un middleware passé avant une révocation n'autorise pas la mutation.
        meta = UserSession.objects.get(user=self.admin)
        context = ActorContext(self.admin.pk, self.admin.auth_version, meta.pk)
        identifier, key = uuid.uuid4(), uuid.uuid4().hex
        payload = {
            "username": "late-fictif",
            "display_name": "Tardif fictif",
            "role": "REPLENISHER",
        }
        idempotency.register_intent(
            context,
            intent_id=identifier,
            key=key,
            fingerprint=payload_hash(payload),
            command_type="accounts.create",
        )
        meta.revoked_at = timezone.now()
        meta.save()
        with self.assertRaises(BusinessError) as caught:
            idempotency.execute_intent(context, intent_id=identifier, key=key, payload=payload)
        self.assertEqual(caught.exception.status, 401)
        self.assertFalse(User.objects.filter(username="late-fictif").exists())

    def test_bootstrap_refuses_second_admin_and_controlled_replacement_is_audited(self):
        with self.assertRaises(BusinessError):
            bootstrap_admin(username="nouvelle-fictive", display_name="Nouvelle fictive")
        old_before = self.admin.auth_version
        user, temporary = bootstrap_admin(
            username="nouvelle-fictive",
            display_name="Nouvelle fictive",
            replace=self.admin.pk,
            reason="Remplacement fictif contrôlé par test",
        )
        self.admin.refresh_from_db()
        self.assertFalse(self.admin.is_active)
        self.assertGreater(self.admin.auth_version, old_before)
        self.assertTrue(user.check_password(temporary))
        self.assertEqual(User.objects.filter(role="ADMIN", is_active=True).count(), 1)
        self.assertEqual(self.admin_client.get("/api/v1/me").status_code, 401)
        self.assertTrue(Operation.objects.filter(command_type="accounts.replace_admin").exists())
        audit = AuditEvent.objects.get(operation__command_type="accounts.replace_admin")
        self.assertTrue(audit.before["is_active"])
        self.assertEqual(audit.before["version"], 0)

    def test_html_pages_forms_accessibility_labels_and_no_store(self):
        for path, client in [
            ("/connexion", Client()),
            ("/mon-compte", self.user_client),
            ("/administration/utilisateurs", self.admin_client),
            ("/administration/utilisateurs/creer", self.admin_client),
            (f"/administration/utilisateurs/{self.assistant.pk}", self.admin_client),
            (f"/administration/utilisateurs/{self.assistant.pk}/reinitialiser", self.admin_client),
        ]:
            response = client.get(path)
            self.assertEqual(response.status_code, 200, response.content)
            self.assertEqual(response["Cache-Control"], "no-store")
            self.assertContains(response, '<html lang="fr" dir="ltr">')
        self.assertContains(Client().get("/connexion"), "csrfmiddlewaretoken")
        self.assertNotContains(self.user_client.get("/mon-compte"), "Utilisateurs</a>")

    def test_second_login_rotates_key_and_revokes_previous_session(self):
        client = Client()
        body = {
            "identifier": self.assistant.username,
            "password": FICTIVE_PASSWORD,
            "persistent": True,
        }
        self.assertEqual(self.post_json("/api/v1/auth/login", body, client).status_code, 200)
        old_cookie = client.cookies[settings.SESSION_COOKIE_NAME].value
        self.assertEqual(self.post_json("/api/v1/auth/login", body, client).status_code, 200)
        self.assertNotEqual(client.cookies[settings.SESSION_COOKIE_NAME].value, old_cookie)
        old = Client()
        old.cookies[settings.SESSION_COOKIE_NAME] = old_cookie
        self.assertEqual(old.get("/api/v1/me").status_code, 401)

    def test_twenty_same_account_creation_intentions_confirm_once(self):
        from tests.accounts_evidence import observations

        identifier, key = uuid.uuid4(), uuid.uuid4().hex
        cookie = self.admin_client.cookies[settings.SESSION_COOKIE_NAME].value
        payload = {
            "username": "vingt-fictif",
            "display_name": "Vingt fictif",
            "role": "REPLENISHER",
        }
        barrier = threading.Barrier(20)

        def worker():
            close_old_connections()
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_backend_pid()")
                    pid = cursor.fetchone()[0]
                client = Client(raise_request_exception=False)
                client.cookies[settings.SESSION_COOKIE_NAME] = cookie
                barrier.wait(timeout=10)
                response = self.post_json(
                    "/api/v1/users", payload, client, identifier=identifier, key=key
                )
                return pid, response.status_code, response.json()
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=20) as pool:
            results = [
                future.result(timeout=30) for future in [pool.submit(worker) for _ in range(20)]
            ]
        self.assertEqual({status for _, status, _ in results}, {201})
        self.assertEqual(len({body["operation_id"] for _, _, body in results}), 1)
        self.assertEqual(sum("one_time" in body for _, _, body in results), 1)
        self.assertEqual(len({pid for pid, _, _ in results}), 20)
        self.assertEqual(User.objects.filter(username="vingt-fictif").count(), 1)
        self.assertEqual(Operation.objects.filter(command_type="accounts.create").count(), 1)
        observations["twenty_account_confirmations"] = {
            "requests": 20,
            "distinct_connections": 20,
            "confirmed_operations": 1,
            "secret_deliveries": 1,
        }

    def test_reset_waits_for_inflight_login_then_revokes_its_new_session(self):
        from apps.accounts import services
        from tests.accounts_evidence import observations

        entered, release = threading.Event(), threading.Event()
        original = services.record_action

        def paused_record(actor, kind, **kwargs):
            if kind == "auth.login" and actor.pk == self.assistant.pk:
                entered.set()
                if not release.wait(timeout=10):
                    raise RuntimeError("test synchronization expired")
            return original(actor, kind, **kwargs)

        admin_cookie = self.admin_client.cookies[settings.SESSION_COOKIE_NAME].value

        def login_worker():
            close_old_connections()
            try:
                client = Client(raise_request_exception=False)
                response = self.post_json(
                    "/api/v1/auth/login",
                    {
                        "identifier": self.assistant.username,
                        "password": FICTIVE_PASSWORD,
                        "persistent": True,
                    },
                    client,
                )
                return response.status_code, client.cookies[settings.SESSION_COOKIE_NAME].value
            finally:
                connection.close()

        def reset_worker():
            close_old_connections()
            try:
                client = Client(raise_request_exception=False)
                client.cookies[settings.SESSION_COOKIE_NAME] = admin_cookie
                return self.post_json(
                    f"/api/v1/users/{self.assistant.pk}/reset-password",
                    {"expected_version": 0, "identity_verified": True},
                    client,
                ).status_code
            finally:
                connection.close()

        with (
            patch("apps.accounts.services.record_action", side_effect=paused_record),
            ThreadPoolExecutor(max_workers=2) as pool,
        ):
            login_future = pool.submit(login_worker)
            try:
                self.assertTrue(entered.wait(timeout=5))
                reset_future = pool.submit(reset_worker)
                self.wait_for_account_lock()
            finally:
                release.set()
            login_status, cookie = login_future.result(timeout=10)
            reset_status = reset_future.result(timeout=10)
        self.assertEqual((login_status, reset_status), (200, 200))
        old = Client()
        old.cookies[settings.SESSION_COOKIE_NAME] = cookie
        self.assertEqual(old.get("/api/v1/me").status_code, 401)
        observations["login_reset_race"] = {
            "real_sql_lock_observed": True,
            "old_cookie_http_status": 401,
        }

    def wait_for_account_lock(self):
        import time

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND pid<>pg_backend_pid() AND wait_event_type='Lock' AND query LIKE '%%accounts_user%%'"
                )
                if cursor.fetchone()[0]:
                    return
            time.sleep(0.02)
        self.fail("No real concurrent account lock observed")

    def test_disable_waits_for_authorized_mutation_then_rejects_later_mutation(self):
        from tests.accounts_evidence import observations
        from tests.probes import commands as probe
        from tests.probes.models import Counter

        commands.register_command(probe.COMMAND)
        probe.entered.clear()
        probe.release.clear()
        counter = Counter.objects.create(owner=self.replenisher)
        user_client = self.logged(self.replenisher)
        meta = UserSession.objects.filter(user=self.replenisher).latest("created_at")
        ctx = ActorContext(self.replenisher.pk, self.replenisher.auth_version, meta.pk)
        identifier, key = uuid.uuid4(), uuid.uuid4().hex
        payload = {"delta": 2, "expected_version": 0, "pause": True}
        idempotency.register_intent(
            ctx,
            intent_id=identifier,
            key=key,
            fingerprint=payload_hash(payload),
            command_type=probe.COMMAND.name,
            resource_id=counter.pk,
        )
        admin_cookie = self.admin_client.cookies[settings.SESSION_COOKIE_NAME].value

        def mutation():
            close_old_connections()
            try:
                return idempotency.execute_intent(
                    ctx, intent_id=identifier, key=key, payload=payload
                ).status
            finally:
                connection.close()

        def disable():
            close_old_connections()
            try:
                client = Client(raise_request_exception=False)
                client.cookies[settings.SESSION_COOKIE_NAME] = admin_cookie
                return self.post_json(
                    f"/api/v1/users/{self.replenisher.pk}",
                    {"expected_version": 0, "is_active": False},
                    client,
                    method="patch",
                ).status_code
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(mutation)
            try:
                self.assertTrue(probe.entered.wait(timeout=5))
                second = pool.submit(disable)
                self.wait_for_account_lock()
            finally:
                probe.release.set()
            self.assertEqual(first.result(timeout=10), 200)
            self.assertEqual(second.result(timeout=10), 200)
        counter.refresh_from_db()
        self.assertEqual(counter.value, 2)
        self.assertEqual(user_client.get("/api/v1/me").status_code, 401)
        with self.assertRaises(BusinessError):
            idempotency.execute_intent(ctx, intent_id=identifier, key=key, payload=payload)
        observations["mutation_disable_race"] = {
            "real_sql_lock_observed": True,
            "committed_before_disable": 1,
            "late_access_refused": True,
        }

    def test_sessions_are_paginated_and_admin_has_session_controls(self):
        now = timezone.now()
        UserSession.objects.bulk_create(
            [
                UserSession(
                    user=self.assistant,
                    key_digest=uuid.uuid4().hex + uuid.uuid4().hex,
                    auth_version=1,
                    generation_id=settings.OPERATION_GENERATION,
                    created_at=now,
                    authenticated_at=now,
                    last_seen_at=now,
                    idle_expires_at=now + timedelta(days=1),
                    absolute_expires_at=now + timedelta(days=2),
                )
                for _ in range(105)
            ]
        )
        first = self.user_client.get("/api/v1/auth/sessions").json()
        self.assertEqual(len(first["sessions"]), 100)
        self.assertEqual(first["next_offset"], 100)
        self.assertEqual(
            len(self.user_client.get("/api/v1/auth/sessions?offset=100").json()["sessions"]), 6
        )
        page = self.admin_client.get(f"/administration/utilisateurs/{self.assistant.pk}")
        self.assertContains(page, "Fermer cette session")
        self.assertContains(page, "Sessions suivantes")


class BootstrapTests(TransactionTestCase):
    def test_documented_initialization_creates_only_explicit_admin_and_cannot_repeat(self):
        from io import StringIO

        from django.core.management import call_command

        AccountGuard.objects.get_or_create(pk=1)
        self.assertEqual(User.objects.count(), 0)
        output = StringIO()
        call_command(
            "bootstrap_admin",
            username="premiere-fictive",
            display_name="Première fictive",
            confirm_local_test=True,
            stdout=output,
        )
        user = User.objects.get(username="premiere-fictive")
        temporary = next(
            line
            for line in output.getvalue().splitlines()
            if line.startswith("Mot de passe temporaire")
        ).rsplit(": ", 1)[1]
        self.assertTrue(user.must_change_password)
        self.assertTrue(user.check_password(temporary))
        self.assertEqual(User.objects.filter(is_active=True, role="ADMIN").count(), 1)
        self.assertTrue(AccountGuard.objects.get(pk=1).initialized)
        self.assertEqual(
            Operation.objects.filter(command_type="accounts.initialize_admin").count(), 1
        )
        with self.assertRaises(BusinessError):
            bootstrap_admin(username="seconde-fictive", display_name="Seconde fictive")

    def test_runtime_cannot_invoke_the_bootstrap_command(self):
        from io import StringIO

        from django.core.management import call_command
        from django.core.management.base import CommandError

        grant_runtime(os.environ["RUNTIME_DATABASE_USER"])
        original = connection.settings_dict.copy()
        connection.close()
        connection.settings_dict.update(
            USER=os.environ["RUNTIME_DATABASE_USER"],
            PASSWORD=os.environ["RUNTIME_DATABASE_PASSWORD"],
        )
        try:
            with self.assertRaises(CommandError):
                call_command(
                    "bootstrap_admin",
                    username="runtime-interdit-fictif",
                    display_name="Interdit",
                    confirm_local_test=True,
                    stdout=StringIO(),
                )
        finally:
            connection.close()
            connection.settings_dict.clear()
            connection.settings_dict.update(original)
        self.assertFalse(User.objects.exists())

    def test_former_admin_reactivation_is_a_business_conflict_not_sql_error(self):
        AccountGuard.objects.get_or_create(pk=1)
        active = User.objects.create_user(
            "active-fictive",
            FICTIVE_PASSWORD,
            display_name="N active fictive",
            role="ADMIN",
            must_change_password=False,
        )
        former = User.objects.create_user(
            "ancienne-fictive",
            FICTIVE_PASSWORD,
            display_name="Ancienne N fictive",
            role="ADMIN",
            is_active=False,
            must_change_password=False,
        )
        client = Client(raise_request_exception=False)
        client.force_login(active)
        response = client.patch(
            f"/api/v1/users/{former.pk}",
            data=json.dumps({"expected_version": 0, "is_active": True}),
            content_type="application/json",
            headers={"Operation-Intent": str(uuid.uuid4()), "Idempotency-Key": uuid.uuid4().hex},
        )
        self.assertEqual(response.status_code, 409, response.content)
        self.assertEqual(User.objects.filter(role="ADMIN", is_active=True).count(), 1)
