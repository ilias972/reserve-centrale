"""Recette réelle Gestionnaire : compteur et formulaires sans saisie de codes internes."""

import json
import os
from pathlib import Path

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from playwright.sync_api import expect, sync_playwright

from apps.catalog.models import Floor, Product
from tests.e2e import test_deliveries as delivery_browser
from tests.test_purchasing import PurchaseFixtures

ROOT = Path(
    os.environ.get("RESPONSABLE_CAPTURE_ROOT", "docs/recette/AMELIORATIONS_RESPONSABLE/captures")
)


class ResponsableImprovementsBrowserTests(PurchaseFixtures, StaticLiveServerTestCase):
    db = delivery_browser.BrowserDeliveryTests.db
    login = delivery_browser.BrowserDeliveryTests.login

    def test_need_badge_live_waiting_reservations_zero_and_network_failure(self):
        ROOT.mkdir(parents=True, exist_ok=True)
        errors = []
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 900}, locale="fr-FR")
            page.on("pageerror", lambda e: errors.append(str(e)))
            self.login(page, self.admin)
            page.goto(self.live_server_url + "/mon-compte")
            badge = page.locator("#pending-purchase-needs-count")
            expect(badge).to_be_hidden()
            expect(badge).to_have_text("")
            page.evaluate("window.badgePageMarker='unchanged'")

            def create():
                p = self.demandable_product(physical=7)
                self.modify(p, minimum=10, target=100)
                return p

            p = self.db(create)
            expect(badge).to_have_text("1", timeout=10000)
            expect(badge).to_be_visible()
            self.assertEqual(page.evaluate("window.badgePageMarker"), "unchanged")
            page.screenshot(path=str(ROOT / "besoin-1440.png"))
            self.db(lambda: self.buy([p], [93]))
            expect(badge).to_be_hidden(timeout=10000)
            self.db(lambda: self.send([p], [2]))
            expect(badge).to_have_text("1", timeout=10000)
            page.route(
                "**/api/v1/recommendations/pending-count", lambda route: route.abort("failed")
            )
            expect(page.locator("#live-status")).to_contain_text(
                "Actualisation indisponible", timeout=12000
            )
            expect(badge).to_have_text("1")
            page.unroute("**/api/v1/recommendations/pending-count")
            page.set_viewport_size({"width": 390, "height": 844})
            page.get_by_role("button", name="Ouvrir la navigation", exact=True).click()
            expect(badge).to_be_visible()
            page.screenshot(path=str(ROOT / "besoin-390.png"))
            self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 390)
            page.evaluate(Path("tests/assets/axe.min.js").read_text())
            violations = page.evaluate(
                "async()=>{let r=await axe.run();return r.violations.filter(x=>['serious','critical'].includes(x.impact)).map(x=>x.id)}"
            )
            self.assertEqual(violations, [])
            (ROOT / "accessibility.json").write_text(
                json.dumps({"serious_critical": violations, "physical_iphone": False}, indent=2)
                + "\n"
            )
            self.db(lambda: self.rec_action(p, "defer"))
            expect(badge).to_be_hidden(timeout=10000)
            browser.close()
        self.assertEqual(errors, [])

    def test_product_and_floor_created_without_internal_code_fields(self):
        ROOT.mkdir(parents=True, exist_ok=True)
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 900}, locale="fr-FR")
            self.login(page, self.admin)
            page.goto(self.live_server_url + "/configuration/floor/creer")
            expect(page.get_by_label("Code unique", exact=True)).to_have_count(0)
            page.get_by_label("Nom", exact=True).fill("Unité automatique navigateur")
            page.screenshot(path=str(ROOT / "creation-etage.png"))
            page.get_by_role("button", name="Enregistrer unité", exact=True).click()
            page.wait_for_url("**/configuration/floor")
            self.assertEqual(
                len(self.db(lambda: Floor.objects.get(name="Unité automatique navigateur").code)),
                32,
            )
            page.goto(self.live_server_url + "/catalogue/creer")
            expect(page.get_by_label("Référence interne", exact=True)).to_have_count(0)
            page.get_by_label("Nom du produit", exact=True).fill("Produit automatique navigateur")
            page.get_by_label("Unité de comptage unique", exact=True).fill("boîte")
            page.screenshot(path=str(ROOT / "creation-produit.png"))
            page.get_by_role("button", name="Créer le produit", exact=True).click()
            expect(
                page.get_by_role("heading", name="Produit automatique navigateur", exact=True)
            ).to_be_visible()
            code = self.db(lambda: Product.objects.get(name="Produit automatique navigateur").code)
            self.assertEqual(len(code), 32)
            expect(
                page.get_by_text("Non inventorié — quantité inconnue", exact=True)
            ).to_be_visible()
            browser.close()

    def test_responsable_badge_is_same_red_circle_as_replenisher_and_assets_versioned(self):
        ROOT.mkdir(parents=True, exist_ok=True)
        product = self.demandable_product(physical=2)
        self.send([product], [1])
        styles = []
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            for user, identifier, width, height in [
                (self.admin, "pending-purchase-needs-count", 1440, 900),
                (self.replenisher, "pending-request-count", 390, 844),
            ]:
                page = browser.new_page(viewport={"width": width, "height": height})
                self.login(page, user)
                page.goto(self.live_server_url + "/mon-compte")
                badge = page.locator("#" + identifier)
                expect(badge).to_be_visible()
                expect(badge).to_have_text("1")
                for selector in [
                    "link[href*='/ui/app.css']",
                    "script[src*='/requests/navigation.js']",
                ]:
                    attribute = "href" if selector.startswith("link") else "src"
                    self.assertIn(
                        "?v=1",
                        page.locator(selector).get_attribute(attribute),
                    )
                state = badge.evaluate("""n=>{
                    const s=getComputedStyle(n),r=n.getBoundingClientRect();
                    const c=document.createElement('canvas');c.width=c.height=1;
                    const ctx=c.getContext('2d');ctx.fillStyle=s.backgroundColor;ctx.fillRect(0,0,1,1);
                    return {background:s.backgroundColor,color:s.color,width:r.width,height:r.height,
                            radius:s.borderRadius,rgba:[...ctx.getImageData(0,0,1,1).data]};
                }""")
                red, green, blue, alpha = state["rgba"]
                self.assertGreater(red, green * 1.7)
                self.assertGreater(red, blue * 1.7)
                self.assertEqual(alpha, 255)
                self.assertAlmostEqual(state["width"], 20, delta=0.1)
                self.assertAlmostEqual(state["height"], 20, delta=0.1)
                self.assertEqual(state["radius"], "999px")
                styles.append(state)
                page.screenshot(path=str(ROOT / (identifier + "-rouge.png")))
                page.context.close()
            browser.close()
        self.assertEqual(styles[0], styles[1])
        (ROOT / "comparaison-pastilles.json").write_text(json.dumps(styles, indent=2) + "\n")
