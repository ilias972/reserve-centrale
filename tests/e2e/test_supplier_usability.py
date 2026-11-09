"""Recette ciblée des simplifications Gestionnaire, données fictives et vrais formulaires."""

import json
from pathlib import Path

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.urls import reverse
from playwright.sync_api import expect, sync_playwright

from apps.catalog.models import ProductSupplier, Supplier
from apps.purchasing.models import PurchaseOrder
from tests.e2e import test_deliveries as delivery_browser
from tests.test_corrections import CorrectionFixtures
from tests.test_purchasing import PurchaseFixtures

ROOT = Path("docs/recette/AMELIORATIONS-M7/captures")


class BrowserChecks:
    db = delivery_browser.BrowserDeliveryTests.db
    login = delivery_browser.BrowserDeliveryTests.login

    def inspect(self, page, name):
        page.wait_for_load_state("networkidle")
        page.evaluate("document.fonts.ready")
        self.assertLessEqual(
            page.evaluate("document.documentElement.scrollWidth"), page.viewport_size["width"]
        )
        page.evaluate(Path("tests/assets/axe.min.js").read_text())
        serious = page.evaluate(
            "async()=>{const r=await axe.run();return r.violations.filter(v=>['serious','critical'].includes(v.impact)).map(v=>v.id)}"
        )
        self.assertEqual(serious, [])
        ROOT.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(ROOT / (name + ".png")), full_page=True)
        (ROOT / (name + ".json")).write_text(
            json.dumps(
                {
                    "viewport": page.viewport_size,
                    "serious_or_critical_axe": serious,
                    "physical_iphone_tested": False,
                },
                indent=2,
            )
            + "\n"
        )


class BrowserSupplierUsabilityTests(BrowserChecks, PurchaseFixtures, StaticLiveServerTestCase):
    def test_responsable_links_price_without_currency_entry_and_real_purchase_navigation(self):
        product = self.demandable_product(physical=2)
        path = reverse("product-suppliers", args=[product.pk])
        need_path = reverse("purchase-need", args=[product.pk])
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 900}, locale="fr-FR")
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            self.login(page, self.admin)
            page.goto(self.live_server_url + reverse("catalog-reference-create", args=["supplier"]))
            expect(page.get_by_label("Code unique", exact=True)).to_have_count(0)
            page.get_by_label("Nom", exact=True).fill("Fournisseur créé sans code")
            page.get_by_role("button", name="Enregistrer fournisseur", exact=True).click()
            page.wait_for_url("**/configuration/supplier")
            self.assertEqual(
                self.db(lambda: len(Supplier.objects.get(name="Fournisseur créé sans code").code)),
                32,
            )
            page.goto(self.live_server_url + path)
            group = page.get_by_role("group", name="Fournisseur fictif", exact=True)
            group.get_by_label("Associer ce fournisseur", exact=True).check()
            group.get_by_label("Fournisseur habituel", exact=True).check()
            group.get_by_label("Lien produit", exact=True).fill(
                "https://example.invalid/article-fictif"
            )
            group.get_by_label("Prix indicatif", exact=True).fill("12.30")
            expect(group.locator('input[name$="-currency"]')).to_be_hidden()
            expect(group).to_contain_text("En euros (€)")
            page.get_by_role("button", name="Enregistrer les fournisseurs", exact=True).click()
            page.wait_for_url("**" + reverse("product-detail", args=[product.pk]))
            self.assertEqual(
                self.db(lambda: ProductSupplier.objects.get(product=product).currency), "EUR"
            )
            page.goto(self.live_server_url + need_path)
            supplier = page.get_by_role("article", name="Fournisseur fictif", exact=True)
            link = supplier.get_by_role(
                "link", name="Voir le produit chez Fournisseur fictif", exact=True
            )
            expect(link).to_have_attribute("href", "https://example.invalid/article-fictif")
            page.context.route(
                "https://example.invalid/**", lambda r: r.fulfill(status=200, body="Article fictif")
            )
            with page.expect_popup() as opened:
                link.click()
            opened.value.wait_for_load_state("load")
            self.assertEqual(opened.value.url, "https://example.invalid/article-fictif")
            opened.value.close()
            self.assertEqual(self.db(lambda: PurchaseOrder.objects.count()), 0)
            self.inspect(page, "besoin-fournisseurs-1440")
            page.set_viewport_size({"width": 390, "height": 844})
            self.inspect(page, "besoin-fournisseurs-390")
            supplier.get_by_role(
                "link", name="Enregistrer un achat chez ce fournisseur", exact=True
            ).click()
            expect(page.locator("#purchase-supplier")).to_have_value(str(self.supplier.pk))
            expect(page.get_by_label("Quantité réellement achetée", exact=True)).to_have_value(
                str(max(0, product.target - 2))
            )
            expect(page.locator("#purchase-attestation")).not_to_be_checked()
            self.assertEqual(self.db(lambda: PurchaseOrder.objects.count()), 0)
            self.assertEqual(errors, [])
            browser.close()


class BrowserSignalementUsabilityTests(BrowserChecks, CorrectionFixtures, StaticLiveServerTestCase):
    def test_delivery_signalements_use_human_context_keep_real_case_links_and_audit_ids(self):
        request, _, _, case = self.completed()
        identifier, reference = str(case.pk), case.reference
        self.assertEqual(self.admin_client.get(f"/api/v1/cases/{identifier}").status_code, 200)
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 900}, locale="fr-FR")
            self.login(page, self.admin)
            page.goto(self.live_server_url + reverse("delivery-issues"))
            expect(page.locator("main")).to_contain_text(request.reference)
            self.assertNotIn(identifier, page.locator("main").inner_text())
            self.inspect(page, "signalements-livraison-1440")
            page.get_by_role("link", name="Examiner le signalement", exact=True).click()
            page.wait_for_url(
                "**"
                + reverse("case-detail", args=[identifier])
                + "?from=deliveries&state=pending&q=&offset=0"
            )
            expect(
                page.get_by_role("heading", name="Erreur de quantité remise", exact=True)
            ).to_be_visible()
            self.assertNotIn(identifier, page.locator("main").inner_text())
            self.assertNotIn(reference, page.locator("main").inner_text())
            expect(
                page.get_by_role(
                    "link", name="Corriger les quantités réellement remises", exact=True
                )
            ).to_be_visible()
            page.goto(self.live_server_url + reverse("case-list"))
            expect(
                page.get_by_role("link", name="Ouvrir le dossier", exact=True)
            ).to_have_attribute("href", reverse("case-detail", args=[identifier]) + "?from=cases")
            self.assertNotIn(reference, page.locator("main").inner_text())
            browser.close()
