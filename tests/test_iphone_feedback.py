"""Contrats de lecture live et configuration autonome, sans données réelles."""

from datetime import timedelta

from django.test import TransactionTestCase
from django.utils import timezone

from apps.accounts.models import UserSession
from tests.test_accounts import AccountFixtures
from tests.test_requests import RequestFixtures


class BackgroundSessionTests(AccountFixtures, TransactionTestCase):
    def test_background_read_checks_access_without_extending_idle_session(self):
        identifier = self.user_client.session["reserve_session_id"]
        before = timezone.now() - timedelta(minutes=5)
        expiry = timezone.now() + timedelta(hours=1)
        UserSession.objects.filter(pk=identifier).update(
            last_seen_at=before, idle_expires_at=expiry
        )
        response = self.user_client.get(
            "/api/v1/request-catalog", headers={"X-Reserve-Background": "1"}
        )
        self.assertEqual(response.status_code, 200)
        meta = UserSession.objects.get(pk=identifier)
        self.assertEqual((meta.last_seen_at, meta.idle_expires_at), (before, expiry))
        self.assertEqual(self.user_client.get("/mon-compte?_live=1").status_code, 200)
        meta.refresh_from_db()
        self.assertEqual((meta.last_seen_at, meta.idle_expires_at), (before, expiry))
        self.user_client.get("/api/v1/request-catalog")
        meta.refresh_from_db()
        self.assertGreater(meta.last_seen_at, before)
        UserSession.objects.filter(pk=identifier).update(revoked_at=timezone.now())
        self.assertEqual(
            self.user_client.get(
                "/api/v1/request-catalog", headers={"X-Reserve-Background": "1"}
            ).status_code,
            401,
        )

    def test_manifest_is_public_scoped_and_contains_no_secrets(self):
        response = self.client.get("/manifest.webmanifest")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["display"], "standalone")
        self.assertEqual(body["scope"], "/")
        self.assertEqual(body["start_url"], "/mon-compte")
        self.assertEqual(len(body["icons"]), 2)
        self.assertIn("manifest-src 'self'", response.headers["Content-Security-Policy"])
        page = self.user_client.get("/mon-compte")
        self.assertContains(page, 'name="apple-mobile-web-app-capable" content="yes"')
        self.assertContains(page, 'rel="manifest"')
        self.assertNotIn("password", str(body))


class SelectedProductReadsTests(RequestFixtures, TransactionTestCase):
    def test_targeted_selected_ids_still_enforce_floor_permissions_and_bounds(self):
        product = self.demandable_product()
        hidden = self.create_product(code="HORS-PERIMETRE")
        response = self.user_client.get(
            "/api/v1/request-catalog", {"product_ids": f"{product.pk},{hidden.pk}"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [row["product_id"] for row in response.json()["products"]], [str(product.pk)]
        )
        for value in ["", "incorrect", ",".join([str(product.pk)] * 101)]:
            self.assertEqual(
                self.user_client.get("/api/v1/request-catalog", {"product_ids": value}).status_code,
                400,
            )
        self.assertEqual(
            self.logged(self.replenisher)
            .get("/api/v1/request-catalog", {"product_ids": str(product.pk)})
            .status_code,
            403,
        )
