"""Simplification des fournisseurs : prix réels préservés, liens exacts, aucune commande implicite."""

from decimal import Decimal

from django.test import TransactionTestCase
from django.urls import reverse

from apps.catalog.models import ProductSupplier, Supplier
from apps.purchasing.models import PurchaseOrder
from tests.test_purchasing import PurchaseFixtures


class SupplierUsabilityTests(PurchaseFixtures, TransactionTestCase):
    def test_supplier_code_is_generated_from_intent_and_replay_creates_only_one(self):
        url = reverse("catalog-reference-create", args=["supplier"])
        response = self.admin_client.get(url)
        form = response.context["form"]
        self.assertNotIn("code", form.fields)
        payload = {
            field.html_name: field.value() if field.value() is not None else "" for field in form
        }
        payload.update(name="Nouveau fournisseur fictif", active="on", code="INJECTE")
        for _ in range(2):
            response = self.admin_client.post(url, payload)
            self.assertEqual(response.status_code, 302, response.content)
        supplier = Supplier.objects.get(name="Nouveau fournisseur fictif")
        self.assertEqual(supplier.code, str(payload["operation_id"]).replace("-", "").upper())
        self.assertEqual(Supplier.objects.filter(name="Nouveau fournisseur fictif").count(), 1)

    def test_supplier_edit_preserves_existing_code_without_saisie(self):
        original_code = self.supplier.code
        url = reverse("catalog-reference-edit", args=["supplier", self.supplier.pk])
        form = self.admin_client.get(url).context["form"]
        self.assertNotIn("code", form.fields)
        payload = {
            field.html_name: field.value() if field.value() is not None else "" for field in form
        }
        payload.update(name="Nom fournisseur modifié", active="on", code="INJECTE")
        response = self.admin_client.post(url, payload)
        self.assertEqual(response.status_code, 302, response.content)
        self.supplier.refresh_from_db()
        self.assertEqual(self.supplier.code, original_code)
        self.assertEqual(self.supplier.name, "Nom fournisseur modifié")

    def link(self, product, **values):
        product.refresh_from_db()
        response = self.post_json(
            f"/api/v1/products/{product.pk}/supplier-links",
            {
                "expected_version": product.version,
                "suppliers": [
                    {
                        "supplier_id": str(self.supplier.pk),
                        "preferred": True,
                        "reference": "ARTICLE-FICTIF",
                        "website": "https://example.invalid/produit-fictif",
                        "indicative_price": None,
                        "currency": "",
                        **values,
                    }
                ],
            },
            method="put",
        )
        self.assertEqual(response.status_code, 200, response.content)

    def supplier_form(self, product):
        url = reverse("product-suppliers", args=[product.pk])
        response = self.admin_client.get(url)
        self.assertEqual(response.status_code, 200)
        rowset = response.context["rowset"]
        payload = {}
        for form in [response.context["form"], rowset.management_form, *rowset]:
            for field in form:
                if field.field.disabled:
                    continue
                value = field.value()
                payload[field.html_name] = (
                    ("on" if value else "")
                    if isinstance(value, bool)
                    else ("" if value is None else value)
                )
        row = rowset[0]
        self.assertEqual(str(row["supplier_id"].value()), str(self.supplier.pk))
        self.assertTrue(row["currency"].is_hidden)
        self.assertTrue(row.fields["currency"].disabled)
        return url, payload, row.prefix

    def test_new_price_uses_euros_without_currency_entry_or_client_override(self):
        product = self.create_product()
        url, payload, prefix = self.supplier_form(product)
        payload.update(
            {
                f"{prefix}-included": "on",
                f"{prefix}-indicative_price": "12.30",
                f"{prefix}-currency": "USD",
            }
        )
        response = self.admin_client.post(url, payload)
        self.assertEqual(response.status_code, 302, response.content)
        link = ProductSupplier.objects.get(product=product)
        self.assertEqual((link.indicative_price, link.currency), (Decimal("12.30"), "EUR"))
        self.assertFalse(PurchaseOrder.objects.exists())

    def test_existing_non_euro_price_is_preserved_without_editable_currency(self):
        product = self.create_product()
        self.link(product, indicative_price="9.50", currency="USD")
        url, payload, prefix = self.supplier_form(product)
        payload[f"{prefix}-currency"] = "EUR"
        response = self.admin_client.post(url, payload)
        self.assertEqual(response.status_code, 302, response.content)
        link = ProductSupplier.objects.get(product=product)
        self.assertEqual((link.indicative_price, link.currency), (Decimal("9.50"), "USD"))
        self.assertEqual(link.reference, "ARTICLE-FICTIF")

    def test_no_price_needs_no_currency_and_removing_price_clears_the_pair(self):
        product = self.create_product()
        url, payload, prefix = self.supplier_form(product)
        payload[f"{prefix}-included"] = "on"
        self.assertEqual(self.admin_client.post(url, payload).status_code, 302)
        link = ProductSupplier.objects.get(product=product)
        self.assertEqual((link.indicative_price, link.currency), (None, ""))
        self.link(product, indicative_price="9.50", currency="USD")
        url, payload, prefix = self.supplier_form(product)
        payload[f"{prefix}-indicative_price"] = ""
        response = self.admin_client.post(url, payload)
        self.assertEqual(response.status_code, 302, response.content)
        link.refresh_from_db()
        self.assertEqual((link.indicative_price, link.currency), (None, ""))

    def test_need_labels_product_link_and_group_header_only_general_supplier_site(self):
        product = self.demandable_product(physical=2)
        self.link(product)
        detail = self.admin_client.get(reverse("purchase-need", args=[product.pk]))
        self.assertContains(detail, "Voir le produit chez Fournisseur fictif")
        self.assertContains(detail, 'href="https://example.invalid/produit-fictif"')
        self.assertNotContains(detail, "Ouvrir le site de Fournisseur fictif")
        listing = self.admin_client.get(reverse("purchase-needs"))
        self.assertContains(listing, 'href="https://example.invalid/catalogue"')
        self.assertNotContains(listing, 'href="https://example.invalid/produit-fictif"')
        self.assertNotIn(
            "link_is_product",
            self.admin_client.get(f"/api/v1/recommendations/{product.pk}").json()["recommendation"][
                "suppliers"
            ][0],
        )
        self.assertFalse(PurchaseOrder.objects.exists())

    def test_need_falls_back_to_general_supplier_site_with_honest_label(self):
        product = self.demandable_product(physical=2)
        self.link(product, website="")
        detail = self.admin_client.get(reverse("purchase-need", args=[product.pk]))
        self.assertContains(detail, "Ouvrir le site de Fournisseur fictif")
        self.assertNotContains(detail, "Voir le produit chez Fournisseur fictif")
        self.assertContains(detail, 'href="https://example.invalid/catalogue"')
        self.assertFalse(PurchaseOrder.objects.exists())
