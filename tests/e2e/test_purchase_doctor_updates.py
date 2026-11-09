"""Recette Chromium des achats illustrés et du compte demandeur — profil B, base fictive isolée."""

import json
from pathlib import Path

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from playwright.sync_api import expect, sync_playwright

from apps.purchasing.models import PurchaseOrder
from tests.e2e import test_deliveries as helpers
from tests.test_accounts import FICTIVE_PASSWORD
from tests.test_purchase_doctor_updates import PurchaseDoctorFixtures

ROOT = Path("docs/recette/ACHATS_COMPTES/captures")


class PurchaseDoctorBrowserTests(PurchaseDoctorFixtures, StaticLiveServerTestCase):
    db = helpers.BrowserDeliveryTests.db
    login = helpers.BrowserDeliveryTests.login

    def screenshot(self, page, name):
        ROOT.mkdir(parents=True, exist_ok=True)
        page.wait_for_load_state("networkidle")
        self.assertLessEqual(
            page.evaluate("document.documentElement.scrollWidth"), page.viewport_size["width"]
        )
        page.evaluate(Path("tests/assets/axe.min.js").read_text())
        violations = page.evaluate(
            "async()=>{const r=await axe.run();return r.violations.filter(v=>['serious','critical'].includes(v.impact)).map(v=>v.id);}"
        )
        self.assertEqual(violations, [])
        page.screenshot(path=str(ROOT / (name + ".png")), full_page=True)
        (ROOT / (name + ".json")).write_text(
            json.dumps({"axe_serious_critical": violations, "width": page.viewport_size["width"]})
            + "\n"
        )

    def test_photos_keyboard_prefill_then_explicit_changed_real_purchase(self):
        p = self.demandable_product(physical=5)
        self.modify(p, minimum=10, target=15)
        self.send([p], [2])
        self.buy([p], [4])
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 900}, locale="fr-FR")
            self.login(page, self.admin)
            page.goto(self.live_server_url + "/achats/enregistrer")
            search = page.get_by_label("Ajouter un produit par nom ou référence", exact=True)
            search.fill(p.name[:12])
            option = page.get_by_role("option").filter(has_text=p.name)
            expect(option).to_be_visible()
            expect(option.locator("img")).to_be_visible()
            expect(option.locator("img")).to_have_js_property("complete", True)
            self.assertGreater(option.locator("img").evaluate("n=>n.naturalWidth"), 0)
            self.screenshot(page, "suggestions-photos-1440")
            search.press("ArrowDown")
            search.press("Enter")
            field = page.get_by_label("Quantité réellement achetée", exact=True)
            expect(field).to_have_value("10")
            expect(page.locator("#purchase-lines img")).to_be_visible()
            expect(page.locator("#purchase-attestation")).not_to_be_checked()
            self.assertEqual(self.db(lambda: PurchaseOrder.objects.count()), 1)
            page.get_by_label("Fournisseur", exact=True).select_option(str(self.supplier.pk))
            field.fill("3")
            page.locator("#purchase-attestation").check()
            page.get_by_role("button", name="Vérifier le récapitulatif", exact=True).click()
            expect(page.locator("#purchase-review")).to_contain_text("3 carton réellement achetés")
            self.assertEqual(self.db(lambda: PurchaseOrder.objects.count()), 1)
            for width, height in [(1440, 900), (390, 844)]:
                page.set_viewport_size({"width": width, "height": height})
                self.screenshot(page, f"achat-photo-{width}")
            page.get_by_role("button", name="Enregistrer l’achat", exact=True).click()
            page.get_by_role("link", name="Consulter les données à jour", exact=True).click()
            self.assertEqual(self.db(lambda: PurchaseOrder.objects.count()), 2)
            self.assertEqual(self.db(lambda: self.stock(p)[:3]), (5, 2, 3))
            self.assertEqual(
                self.db(
                    lambda: (
                        PurchaseOrder.objects.exclude(lines__quantity=4).get().lines.get().quantity
                    )
                ),
                3,
            )
            self.assertIn(
                "ui/app-icon-192.png", page.locator("link[rel=icon]").get_attribute("href")
            )
            browser.close()

    def test_doctor_login_catalog_request_follow_and_server_denial(self):
        p = self.demandable_product()
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 900}, locale="fr-FR")
            self.login(page, self.admin)
            page.goto(self.live_server_url + "/administration/utilisateurs/creer")
            page.get_by_label("Nom affiché", exact=True).fill("Demandeur B navigateur fictif")
            page.get_by_label("Rôle", exact=True).select_option("DOCTOR")
            page.get_by_label("Unité d’affectation", exact=True).select_option(str(self.floor.pk))
            page.get_by_label("Identifiant de connexion", exact=True).fill("medecin-navigateur")
            page.get_by_role("button", name="Créer le compte", exact=True).click()
            temporary = page.get_by_label("Mot de passe temporaire", exact=True).input_value()
            context = browser.new_context(viewport={"width": 1440, "height": 900}, locale="fr-FR")
            page = context.new_page()
            page.goto(self.live_server_url + "/connexion")
            page.get_by_label("Identifiant", exact=True).fill("medecin-navigateur")
            page.get_by_label("Mot de passe", exact=True).fill(temporary)
            page.get_by_role("button", name="Se connecter", exact=True).click()
            expect(
                page.get_by_role("heading", name="Remplacer mon mot de passe", exact=True)
            ).to_be_visible()
            self.assertEqual(
                page.request.get(self.live_server_url + "/api/v1/request-catalog").status, 403
            )
            page.get_by_label("Mot de passe actuel ou temporaire", exact=True).fill(temporary)
            page.get_by_label("Nouveau mot de passe", exact=True).fill(FICTIVE_PASSWORD)
            page.get_by_label("Confirmer le nouveau mot de passe", exact=True).fill(
                FICTIVE_PASSWORD
            )
            page.get_by_role(
                "button", name="Enregistrer mon nouveau mot de passe", exact=True
            ).click()
            expect(page.locator("main")).to_contain_text("Demandeur B navigateur fictif")
            page.get_by_role("link", name="Faire une demande", exact=True).click()
            page.get_by_role("button", name=f"Augmenter — {p.name}", exact=True).click()
            page.get_by_role("button", name="Vérifier ma demande", exact=True).click()
            page.get_by_role("button", name="Envoyer la demande", exact=True).click()
            page.get_by_role("link", name="Suivre cette demande", exact=True).click()
            expect(page.locator("#request-status")).to_contain_text("Nouvelle")
            self.assertEqual(self.db(lambda: self.stock(p)[:3]), (10, 1, 9))
            self.screenshot(page, "demande-medecin-1440")
            response = page.goto(self.live_server_url + "/administration/utilisateurs")
            self.assertEqual(response.status, 403)
            browser.close()
