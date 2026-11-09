"""Achats illustrés et profil demandeur — profil B, via les services réels et PostgreSQL."""

from django.db import IntegrityError, transaction
from django.test import TransactionTestCase

from apps.accounts.models import User
from apps.purchasing.models import PurchaseOrder
from apps.requests.models import Delivery
from tests.test_corrections import CorrectionFixtures
from tests.test_purchasing import PurchaseFixtures


class PurchaseDoctorFixtures(PurchaseFixtures, CorrectionFixtures):
    def doctor(self):
        response = self.update(
            self.assistant, {"role": "DOCTOR", "display_name": "Demandeur B fictif"}
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assistant.refresh_from_db()
        self.user_client = self.logged(self.assistant)
        return self.assistant


class PurchaseDoctorTests(PurchaseDoctorFixtures, TransactionTestCase):
    def test_four_fixed_roles_and_new_responsible_label(self):
        self.assertEqual(set(User.Role.values), {"ADMIN", "REPLENISHER", "ASSISTANT", "DOCTOR"})
        self.assertEqual(User.Role.ADMIN.label, "Gestionnaire")
        response = self.admin_client.get("/administration/utilisateurs/creer")
        self.assertContains(response, "Demandeur — profil B")
        self.assertContains(response, "Identifiant de connexion")
        self.assertNotContains(response, "N" + "+1")

    def test_custom_roles_cannot_be_created(self):
        response = self.create_account(role="CUSTOM_ROLE")
        self.assertEqual(response.status_code, 400)
        self.assertFalse(User.objects.filter(role="CUSTOM_ROLE").exists())

    def test_responsible_creates_doctor_individual_account_and_forced_password(self):
        response = self.create_account(role="DOCTOR", floor_id=str(self.floor.pk))
        self.assertEqual(response.status_code, 201, response.content)
        user = User.objects.get(username="nouveau-fictif")
        self.assertTrue(user.is_requester)
        self.assertTrue(user.must_change_password)
        self.assertEqual(user.floor_id, self.floor.pk)

    def test_doctor_needs_one_active_floor_and_sql_enforces_shape(self):
        self.assertEqual(self.create_account(role="DOCTOR").status_code, 400)
        self.floor.active = False
        self.floor.save(update_fields=["active"])
        self.assertEqual(
            self.create_account(role="DOCTOR", floor_id=str(self.floor.pk)).status_code, 400
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            User.objects.create_user("sans-etage", role="DOCTOR")

    def test_doctor_catalog_only_explicit_authorizations_and_no_global_balances(self):
        user = self.doctor()
        allowed = self.demandable_product(code="AUTORISE")
        hidden = self.demandable_product(code="INTERDIT")
        self.rights(
            hidden,
            [
                {
                    "floor_id": str(self.floor.pk),
                    "visible": False,
                    "demandable": False,
                    "maximum": None,
                }
            ],
        )
        response = self.user_client.get("/api/v1/catalog")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertContains(response, allowed.name)
        self.assertNotContains(response, hidden.name)
        self.assertEqual(
            self.user_client.get(f"/api/v1/products/{hidden.pk}/photo").status_code, 404
        )
        data = self.user_client.get("/api/v1/request-catalog").json()
        self.assertNotIn("reserved_quantity", str(data))
        self.assertEqual(user.role, "DOCTOR")

    def test_doctor_request_reserves_cancels_and_owns_history(self):
        self.doctor()
        request, products = self.requested((4,))
        self.assertEqual(self.stock(products[0])[:3], (10, 4, 6))
        self.assertEqual(self.cancel_request(request).status_code, 200)
        self.assertEqual(self.stock(products[0])[:3], (10, 0, 10))
        self.assert_request_consistent(request)

    def test_doctor_request_can_be_delivered_and_corrected_using_same_ledger(self):
        self.doctor()
        request, products = self.prepare((10,))
        self.assertEqual(self.deliver(request).status_code, 201)
        delivery = Delivery.objects.get(request=request)
        case = self.error_case(delivery)
        self.correct(delivery, case, [8])
        self.assertEqual(self.stock(products[0])[:3], (2, 0, 2))
        self.assert_request_consistent(request)

    def test_doctor_cannot_read_other_requests_or_administer_or_mutate_stocks(self):
        request, _ = self.requested()
        other = User.objects.create_user(
            "medecin-autre",
            "Une phrase fictive longue 2026 !",
            role="DOCTOR",
            floor=self.floor,
            must_change_password=False,
        )
        client = self.logged(other)
        for path in [
            "/administration/utilisateurs",
            "/stocks",
            "/stocks/signalements",
            "/achats",
            "/receptions",
        ]:
            self.assertEqual(client.get(path).status_code, 403, path)
        self.assertEqual(client.get(f"/demandes/{request.pk}").status_code, 404)
        self.assertEqual(self.post_json("/api/v1/orders", {}, client).status_code, 403)
        self.assertEqual(self.post_json("/api/v1/users", {}, client).status_code, 403)

    def test_doctor_plafond_and_inactive_floor_are_rechecked(self):
        self.doctor()
        p = self.demandable_product()
        self.assertEqual(self.send([p], [6]).status_code, 409)
        self.floor.active = False
        self.floor.save(update_fields=["active"])
        self.assertEqual(self.send([p], [1]).status_code, 409)
        self.assertEqual(self.stock(p)[:3], (10, 0, 10))

    def test_physical_gap_prefill_is_distinct_from_recommendation(self):
        p = self.demandable_product(physical=5)
        self.modify(p, minimum=10, target=15)
        self.send([p], [2])
        self.buy([p], [4])
        response = self.admin_client.get("/achats/enregistrer", {"product": p.pk})
        product = response.context["config"]["initial_product"]
        self.assertEqual(product["recommended"], 8)
        self.assertEqual(product["suggested_actual_quantity"], 10)
        self.assertEqual(product["physical"], 5)
        self.assertTrue(product["photo_url"])
        self.assertEqual(PurchaseOrder.objects.count(), 1)
        self.assertEqual(self.stock(p)[:3], (5, 2, 3))

    def test_product_added_later_uses_current_physical_and_unknown_is_not_zero(self):
        p = self.demandable_product(physical=5)
        self.modify(p, target=15)
        self.adjust([p], [8])
        response = self.admin_client.get("/api/v1/purchase-products", {"code": p.code})
        self.assertEqual(response.json()["product"]["suggested_actual_quantity"], 7)
        uncounted = self.create_product(code="NON-COMPTE")
        response = self.admin_client.get("/api/v1/purchase-products", {"code": uncounted.code})
        self.assertIsNone(response.json()["product"]["suggested_actual_quantity"])
        self.assertIsNone(response.json()["product"]["physical"])

    def test_photo_suggestions_and_private_photo_permissions(self):
        p = self.demandable_product()
        response = self.admin_client.get(
            "/api/v1/search-suggestions", {"scope": "products", "q": p.name}
        )
        url = response.json()["suggestions"][0]["photo_url"]
        self.assertTrue(url.startswith("/api/v1/products/"))
        self.assertEqual(self.admin_client.get(url).status_code, 200)

    def test_zero_gap_cannot_create_zero_quantity_order(self):
        p = self.demandable_product(physical=10)
        self.modify(p, target=5)
        response = self.admin_client.get("/api/v1/purchase-products", {"code": p.code})
        self.assertEqual(response.json()["product"]["suggested_actual_quantity"], 0)
        self.assertEqual(
            self.post_json(
                "/api/v1/orders",
                {
                    "supplier_id": str(self.supplier.pk),
                    "purchased_on": "2026-10-25",
                    "purchase_confirmed": True,
                    "lines": [
                        {
                            "product_id": str(p.pk),
                            "quantity": 0,
                            "expected_product_version": p.version,
                        }
                    ],
                },
            ).status_code,
            400,
        )
        self.assertEqual(PurchaseOrder.objects.count(), 0)
