"""Parcours M2 avec vrais formulaires, vrais fichiers et PostgreSQL."""

import json
from pathlib import Path

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from playwright.sync_api import expect, sync_playwright

from apps.catalog.models import Product, ProductSupplier
from tests.test_accounts import FICTIVE_PASSWORD
from tests.test_catalog import CatalogFixtures, image_file


class BrowserCatalogTests(CatalogFixtures, StaticLiveServerTestCase):
    def login_page(self, page, user):
        page.goto(self.live_server_url + "/connexion")
        page.wait_for_load_state("networkidle")
        expect(page.get_by_role("heading", name="Se connecter", exact=True)).to_be_visible()
        page.get_by_label("Identifiant", exact=True).fill(user.username)
        page.get_by_label("Mot de passe", exact=True).fill(FICTIVE_PASSWORD)
        page.get_by_role("button", name="Se connecter", exact=True).click()
        page.wait_for_url("**/mon-compte")

    def test_admin_builds_catalog_photo_rights_supplier_and_mobile_sees_authorized_product(self):
        evidence = Path("reports/m2-browser")
        evidence.mkdir(parents=True, exist_ok=True)
        errors, server_errors = [], []
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            desktop = browser.new_context(viewport={"width": 1440, "height": 1050}, locale="fr-FR")
            page = desktop.new_page()
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on(
                "response",
                lambda response: (
                    server_errors.append(response.status) if response.status >= 500 else None
                ),
            )
            self.login_page(page, self.admin)
            page.get_by_role("link", name="Catalogue", exact=True).click()
            page.get_by_role("link", name="Catégories", exact=True).click()
            page.get_by_role("link", name="Créer une catégorie", exact=True).click()
            page.get_by_label("Nom", exact=True).fill("Consommables fictifs")
            page.get_by_role("button", name="Enregistrer catégorie", exact=True).click()
            page.wait_for_url("**/configuration/category")
            page.get_by_role("link", name="Unités", exact=True).click()
            page.get_by_role("link", name="Créer un unité", exact=True).click()
            expect(page.get_by_label("Code unique", exact=True)).to_have_count(0)
            page.get_by_label("Nom", exact=True).fill("Unité fictif C")
            page.get_by_role("button", name="Enregistrer unité", exact=True).click()
            page.wait_for_url("**/configuration/floor")
            page.get_by_role("link", name="Fournisseurs", exact=True).click()
            page.get_by_role("link", name="Créer un fournisseur", exact=True).click()
            expect(page.get_by_label("Code unique", exact=True)).to_have_count(0)
            page.get_by_label("Nom", exact=True).fill("Fournisseur de démonstration fictif")
            page.get_by_label("Site Web (facultatif)", exact=True).fill("https://example.com")
            page.get_by_role("button", name="Enregistrer fournisseur", exact=True).click()
            page.wait_for_url("**/configuration/supplier")
            page.get_by_role("link", name="Produits", exact=True).click()
            page.get_by_role("link", name="Créer un produit", exact=True).click()
            expect(page.get_by_label("Référence interne", exact=True)).to_have_count(0)
            page.get_by_label("Nom du produit", exact=True).fill("Boîtes de fournitures fictives")
            page.get_by_label("Catégorie (facultative)", exact=True).select_option(
                label="Consommables fictifs"
            )
            page.get_by_label("Unité de comptage unique", exact=True).fill("boîte")
            page.get_by_label("Stock bas à", exact=True).fill("3")
            page.get_by_label("Quantité souhaitée", exact=True).fill("15")
            page.get_by_role("button", name="Créer le produit", exact=True).click()
            expect(
                page.get_by_role("heading", name="Boîtes de fournitures fictives", exact=True)
            ).to_be_visible()
            product_path = page.url.removeprefix(self.live_server_url)
            expect(
                page.get_by_text("Non inventorié — quantité inconnue", exact=True)
            ).to_be_visible()
            expect(page.get_by_text("Sans fournisseur configuré.", exact=True)).to_be_visible()
            expect(page.get_by_role("button", name="Activer le produit", exact=True)).to_have_count(
                0
            )
            upload = image_file(size=(600, 400), color="lightblue")
            page.get_by_label("Photographie du produit", exact=True).set_input_files(
                {"name": "photo-demo.png", "mimeType": "image/png", "buffer": upload.read()}
            )
            page.get_by_role("button", name="Ajouter la photographie", exact=True).click()
            page.wait_for_url("**" + product_path)
            expect(
                page.get_by_role("button", name="Remplacer la photographie", exact=True)
            ).to_be_visible()
            self.assertTrue(
                page.locator(".product-main-photo").evaluate(
                    "img=>img.complete && img.naturalWidth>0"
                )
            )
            page.get_by_role("link", name="Configurer les droits et plafonds", exact=True).click()
            group = page.locator("tbody tr").filter(has_text="Unité fictif A")
            group.get_by_label("Demande autorisée", exact=True).check()
            group.get_by_label("Maximum par demande", exact=True).fill("4")
            page.get_by_role(
                "button", name="Enregistrer les droits et plafonds", exact=True
            ).click()
            page.wait_for_url("**" + product_path)
            page.get_by_role("link", name="Configurer les fournisseurs", exact=True).click()
            group = page.get_by_role(
                "group", name="Fournisseur de démonstration fictif", exact=True
            )
            group.get_by_label("Associer ce fournisseur", exact=True).check()
            group.get_by_label("Fournisseur habituel", exact=True).check()
            group.get_by_label(
                "Référence du produit chez le fournisseur (facultative)", exact=True
            ).fill("FICTIF-REF")
            group.get_by_label("Prix indicatif", exact=True).fill("12.30")
            expect(group.locator('input[name$="-currency"]')).to_be_hidden()
            page.get_by_role("button", name="Enregistrer les fournisseurs", exact=True).click()
            page.wait_for_url("**" + product_path)
            page.screenshot(path=str(evidence / "fiche-produit-pc.png"), full_page=True)
            mobile = browser.new_context(
                viewport={"width": 390, "height": 844},
                is_mobile=True,
                has_touch=True,
                locale="fr-FR",
            )
            phone = mobile.new_page()
            phone.on("pageerror", lambda error: errors.append(str(error)))
            self.login_page(phone, self.assistant)
            phone.get_by_role("link", name="Catalogue", exact=True).click()
            phone.wait_for_load_state("networkidle")
            expect(
                phone.get_by_role("heading", name="Boîtes de fournitures fictives", exact=True)
            ).to_be_visible()
            self.assertEqual(
                phone.get_by_role("link", name="Créer un produit", exact=True).count(), 0
            )
            self.assertEqual(
                phone.get_by_text("Fournisseur de démonstration fictif", exact=True).count(), 0
            )
            self.assertTrue(phone.evaluate("document.documentElement.scrollWidth <= innerWidth"))
            phone.screenshot(path=str(evidence / "catalogue-mobile.png"), full_page=True)
            page.get_by_role("link", name="Historique des paramètres", exact=True).click()
            expect(
                page.get_by_text("Fournisseur de démonstration fictif :", exact=False)
            ).to_be_visible()
            page.screenshot(path=str(evidence / "historique-parametres.png"), full_page=True)
            browser.close()
        self.assertFalse(errors, errors)
        self.assertFalse(server_errors, server_errors)
        self.assertTrue(
            ProductSupplier.objects.filter(
                product__name="Boîtes de fournitures fictives", preferred=True
            ).exists()
        )
        self.assertEqual(
            Product.objects.get(name="Boîtes de fournitures fictives").status, "NOT_COUNTED"
        )
        (evidence / "observations.json").write_text(
            json.dumps(
                {
                    "desktop": "1440x1050",
                    "mobile": "390x844",
                    "page_errors": errors,
                    "http_5xx": server_errors,
                    "real_photo_upload": True,
                    "no_inventory_or_request_simulated": True,
                },
                indent=2,
            )
            + "\n"
        )
