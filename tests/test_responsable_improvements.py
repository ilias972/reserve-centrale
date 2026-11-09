"""Automatisation des références et compteur Gestionnaire : vrais services et PostgreSQL."""

from django.test import Client, TransactionTestCase
from django.urls import reverse

from apps.catalog.models import Floor, Product
from apps.core.models import Operation
from apps.notifications.models import Outbox
from tests.test_purchasing import PurchaseFixtures


class ResponsableImprovementsTests(PurchaseFixtures, TransactionTestCase):
    def form_payload(self, url):
        response = self.admin_client.get(url)
        self.assertEqual(response.status_code, 200)
        form = response.context["form"]
        self.assertNotIn("code", form.fields)
        return {
            field.html_name: field.value() if field.value() is not None else "" for field in form
        }

    def count(self, client=None):
        response = (client or self.admin_client).get("/api/v1/recommendations/pending-count")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response["Cache-Control"], "no-store")
        return response.json()["pending_count"]

    def test_product_reference_is_server_generated_replay_safe_and_readonly(self):
        url = reverse("product-create")
        body = self.form_payload(url)
        body.update(
            name="Produit automatiquement référencé",
            unit="boîte",
            minimum=2,
            target=10,
            category_id="",
            code="INJECTE",
        )
        for _ in range(2):
            self.assertEqual(self.admin_client.post(url, body).status_code, 302)
        product = Product.objects.get(name=body["name"])
        self.assertEqual(product.code, str(body["operation_id"]).replace("-", "").upper())
        self.assertIsNone(product.stock.physical)
        self.assertEqual(Product.objects.filter(name=body["name"]).count(), 1)
        edit = reverse("product-edit", args=[product.pk])
        changed = self.form_payload(edit)
        changed.update(
            name="Nom modifié", unit="boîte", minimum=3, target=10, category_id="", code="AUTRE"
        )
        self.assertEqual(self.admin_client.post(edit, changed).status_code, 302)
        product.refresh_from_db()
        self.assertEqual(product.code, str(body["operation_id"]).replace("-", "").upper())

    def test_floor_code_generated_once_and_existing_code_preserved(self):
        url = reverse("catalog-reference-create", args=["floor"])
        body = self.form_payload(url)
        body.update(name="Unité automatique", active="on", code="INJECTE")
        for _ in range(2):
            self.assertEqual(self.admin_client.post(url, body).status_code, 302)
        floor = Floor.objects.get(name=body["name"])
        original = floor.code
        self.assertEqual(original, str(body["operation_id"]).replace("-", "").upper())
        edit = reverse("catalog-reference-edit", args=["floor", floor.pk])
        body = self.form_payload(edit)
        body.update(name="Nom unité modifié", active="on", code="AUTRE")
        self.assertEqual(self.admin_client.post(edit, body).status_code, 302)
        floor.refresh_from_db()
        self.assertEqual(floor.code, original)
        self.assertEqual(Floor.objects.filter(code=original).count(), 1)

    def test_distinct_creations_generate_unique_codes_and_invalid_form_creates_nothing(self):
        url = reverse("product-create")
        codes = []
        for name in ["Création A", "Création B"]:
            body = self.form_payload(url)
            body.update(name=name, unit="carton", minimum=0, target=1, category_id="")
            self.assertEqual(self.admin_client.post(url, body).status_code, 302)
            codes.append(Product.objects.get(name=name).code)
        self.assertNotEqual(*codes)
        before = Product.objects.count()
        body = self.form_payload(url)
        body.update(name="", unit="carton", minimum=0, target=1)
        response = self.admin_client.post(url, body)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Product.objects.count(), before)
        self.assertNotIn("code", response.context["form"].fields)

    def test_counter_tracks_stock_reservations_waiting_defer_and_reactivate(self):
        self.assertEqual(self.count(), 0)
        p = self.demandable_product(physical=7)
        self.modify(p, minimum=10, target=100)
        self.assertEqual(self.count(), 1)
        physical = self.stock(p)[:2]
        self.buy([p], [93])
        self.assertEqual(self.count(), 0)
        self.assertEqual(self.stock(p)[:2], physical)
        self.send([p], [2])
        self.assertEqual(self.count(), 1)
        self.assertEqual(self.rec_action(p, "defer").status_code, 200)
        self.assertEqual(self.count(), 0)
        self.assertEqual(self.rec_action(p, "reactivate").status_code, 200)
        self.assertEqual(self.count(), 1)

    def test_counter_excludes_archived_uninventoried_and_deferred_but_not_missing_supplier(self):
        self.create_product(code="NON-INVENTORIE")
        p = self.demandable_product(physical=0)
        self.assertEqual(self.count(), 1)
        self.assertEqual(p.supplier_links.count(), 0)
        self.rec_action(p, "defer")
        self.assertEqual(self.count(), 0)
        self.assertTrue(self.need(p)["shortage"])  # Rupture toujours visible, A11 inchangé.
        self.rec_action(p, "reactivate")
        p.refresh_from_db()
        self.assertEqual(
            self.post_json(
                f"/api/v1/products/{p.pk}/archive",
                {"expected_version": p.version, "confirmed": True},
            ).status_code,
            200,
        )
        self.assertEqual(self.count(), 0)

    def test_counter_is_admin_only_readonly_and_global_not_current_filters(self):
        self.demandable_product(physical=1)
        before = (Operation.objects.count(), Outbox.objects.count())
        for _ in range(3):
            self.assertEqual(self.count(), 1)
        self.assertEqual((Operation.objects.count(), Outbox.objects.count()), before)
        self.assertContains(self.admin_client.get("/achats/besoins?q=absent"), ">1</span>")
        for client in [self.operator, self.user_client]:
            self.assertEqual(client.get("/api/v1/recommendations/pending-count").status_code, 403)
            self.assertNotContains(client.get("/mon-compte"), 'id="pending-purchase-needs-count"')
            self.assertEqual(client.get(reverse("product-create")).status_code, 403)
        self.assertEqual(Client().get("/api/v1/recommendations/pending-count").status_code, 401)
