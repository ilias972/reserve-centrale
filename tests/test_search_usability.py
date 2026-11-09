"""Recherche réelle PostgreSQL : portée, normalisation et formulaire unique."""

from django.test import TransactionTestCase

from apps.catalog.forms import PermissionRowForm
from apps.catalog.models import Category, FloorProduct, Supplier
from apps.requests.models import InternalRequest
from tests.test_deliveries import DeliveryFixtures


class SearchUsabilityTests(DeliveryFixtures, TransactionTestCase):
    def test_single_permission_control_derives_visibility_and_requires_ceiling(self):
        form = PermissionRowForm(
            data={"floor_id": str(self.floor.pk), "demandable": "on", "maximum": "3", "visible": ""}
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertTrue(form.cleaned_data["visible"])
        self.assertNotIn("visible", form.fields)
        empty = PermissionRowForm(data={"floor_id": str(self.floor.pk), "demandable": "on"})
        self.assertFalse(empty.is_valid())
        disabled = PermissionRowForm(data={"floor_id": str(self.floor.pk), "maximum": "3"})
        self.assertTrue(disabled.is_valid())
        self.assertFalse(disabled.cleaned_data["visible"])

    def test_suggestions_and_submitted_search_match_accents_words_and_reference(self):
        product = self.demandable_product(code="GANTS-BLEUS")
        self.assertEqual(self.modify(product, name="Gants stériles bleus").status_code, 200)
        for text in ["STERILES", "steri gan", "GANTS-BL", "gants bleus", "stériles"]:
            response = self.user_client.get(
                "/api/v1/search-suggestions", {"scope": "products", "q": text}
            )
            self.assertEqual(response.status_code, 200, response.content)
            self.assertEqual([s["value"] for s in response.json()["suggestions"]], [product.code])
            rows = self.user_client.get("/api/v1/request-catalog", {"q": text}).json()["products"]
            self.assertEqual([p["product_id"] for p in rows], [str(product.pk)])
        response = self.user_client.get(
            "/api/v1/search-suggestions", {"scope": "products", "q": "%%%"}
        )
        self.assertEqual(response.json()["suggestions"], [])

    def test_suggestions_keep_floor_scope_roles_and_eight_result_limit(self):
        products = [self.demandable_product(code=f"VISIBLE-{i}") for i in range(10)]
        hidden = self.create_product(code="VISIBLE-SECRET")
        response = self.user_client.get(
            "/api/v1/search-suggestions", {"scope": "products", "q": "VISIBLE"}
        )
        self.assertEqual(len(response.json()["suggestions"]), 8)
        self.assertNotIn(hidden.code, str(response.json()))
        for scope in ["stocks", "users", "floor", "supplier", "category"]:
            self.assertEqual(
                self.user_client.get(
                    "/api/v1/search-suggestions", {"scope": scope, "q": "fictif"}
                ).status_code,
                403,
            )
        for scope in ["users", "floor", "supplier", "category"]:
            self.assertEqual(
                self.operator.get(
                    "/api/v1/search-suggestions", {"scope": scope, "q": "fictif"}
                ).status_code,
                403,
            )
        self.assertEqual(
            self.client.get(
                "/api/v1/search-suggestions", {"scope": "products", "q": "VISIBLE"}
            ).status_code,
            401,
        )
        self.assertEqual(
            self.user_client.get(
                "/api/v1/search-suggestions", {"scope": "unknown", "q": "VISIBLE"}
            ).status_code,
            400,
        )
        self.assertEqual(
            self.user_client.get(
                "/api/v1/search-suggestions",
                {"scope": "products", "q": "VISIBLE", "category": "invalid"},
            ).status_code,
            400,
        )
        for mode, expected in [("initial", [hidden.code]), ("counted", [])]:
            response = self.operator.get(
                "/api/v1/search-suggestions", {"scope": "stocks", "q": hidden.code, "mode": mode}
            )
            self.assertEqual([row["value"] for row in response.json()["suggestions"]], expected)
        self.assertEqual(
            self.operator.get(
                "/api/v1/search-suggestions", {"scope": "stocks", "q": "VISIBLE", "mode": "invalid"}
            ).status_code,
            400,
        )
        self.assertTrue(products)

    def test_request_name_search_no_duplicate_requests_and_no_other_owner(self):
        request, products = self.requested(quantities=(2, 3))
        # Both historical line names match the same query: only one request result.
        response = self.user_client.get("/api/v1/requests", {"q": "produit fictif"})
        self.assertEqual([r["id"] for r in response.json()["requests"]], [str(request.pk)])
        self.assertEqual(
            self.user_client.get(
                "/api/v1/search-suggestions", {"scope": "requests", "q": "produit fictif"}
            ).json()["suggestions"][0]["value"],
            request.reference,
        )
        other = self.logged(self.create_other_assistant())
        self.assertEqual(
            other.get(
                "/api/v1/search-suggestions", {"scope": "requests", "q": request.reference}
            ).json()["suggestions"],
            [],
        )
        self.assertEqual(InternalRequest.objects.count(), 1)
        self.assertEqual(len(products), 2)

    def create_other_assistant(self):
        from apps.accounts.models import User

        return User.objects.create_user(
            "autre-fictive", role="ASSISTANT", floor=self.floor, must_change_password=False
        )

    def test_reference_and_user_suggestions_use_same_submitted_search(self):
        Category.objects.create(name="Hygiène générale")
        Supplier.objects.create(code="FOURN-1", name="Équipement fictif", active=True)
        for scope, text, value in [
            ("category", "HYGIE", "Hygiène générale"),
            ("supplier", "equip", "FOURN-1"),
            ("floor", "unite", "FICTIF-A"),
            ("users", "demandeur", "demandeur-a-fictif"),
        ]:
            response = self.admin_client.get(
                "/api/v1/search-suggestions", {"scope": scope, "q": text}
            )
            self.assertEqual(response.status_code, 200, response.content)
            self.assertIn(value, [s["value"] for s in response.json()["suggestions"]])
        self.assertEqual(
            self.admin_client.get("/configuration/category", {"q": "hygiene"})
            .context["references"][0]
            .name,
            "Hygiène générale",
        )

    def test_reading_new_permission_screen_does_not_rewrite_existing_rights(self):
        product = self.demandable_product()
        right = FloorProduct.objects.get(product=product, floor=self.floor)
        self.assertEqual(
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
            ).status_code,
            200,
        )
        self.admin_client.get(f"/catalogue/{product.pk}/droits")
        right.refresh_from_db()
        self.assertTrue(right.visible)
        self.assertFalse(right.demandable)
