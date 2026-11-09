"""M10 : navigation, filtres, téléchargement réel et accessibilité Chromium."""

import json
from pathlib import Path

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from playwright.sync_api import expect, sync_playwright

from tests.e2e import test_corrections as helpers
from tests.test_notifications import NotificationFixtures
from tests.test_reporting import sheet

ROOT = Path("docs/recette/M10/browser")


class ReportingBrowserTests(NotificationFixtures, StaticLiveServerTestCase):
    db = helpers.BrowserCorrectionTests.db
    login = helpers.BrowserCorrectionTests.login
    page = helpers.BrowserCorrectionTests.page

    def setUp(self):
        super().setUp()
        self.errors = []
        self.failures = []
        ROOT.mkdir(parents=True, exist_ok=True)

    def capture(self, page, name):
        page.wait_for_load_state("networkidle")
        self.assertLessEqual(
            page.evaluate("document.documentElement.scrollWidth"), page.viewport_size["width"]
        )
        page.evaluate(Path("tests/assets/axe.min.js").read_text())
        violations = page.evaluate(
            "async()=> (await axe.run()).violations.filter(v=>['serious','critical'].includes(v.impact)).map(v=>v.id)"
        )
        self.assertEqual(violations, [])
        self.assertEqual(self.errors, [])
        self.assertEqual(self.failures, [])
        expect(page.locator('a[href="#"],a[href^="javascript:"]')).to_have_count(0)
        page.screenshot(path=str(ROOT / (name + ".png")), full_page=True)
        (ROOT / (name + ".json")).write_text(
            json.dumps(
                {
                    "viewport": page.viewport_size,
                    "axe_serious_critical": violations,
                    "page_errors": self.errors,
                    "http_5xx": self.failures,
                    "physical_iphone": False,
                },
                indent=2,
            )
        )

    def test_admin_dashboard_filter_export_and_download(self):
        p = self.demandable_product(physical=0)
        self.buy([p], [20])
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                page = self.page(browser, self.admin)
                page.get_by_role("link", name="Tableau de bord", exact=True).click()
                expect(
                    page.get_by_role("heading", name="Tableau de bord", exact=True)
                ).to_be_visible()
                self.capture(page, "dashboard-1440")
                page.get_by_role("link", name="Consulter toutes les ruptures").click()
                expect(page.get_by_role("heading", name="État actuel des stocks")).to_be_visible()
                page.get_by_label("Produit", exact=True).select_option(str(p.pk))
                page.get_by_role("button", name="Appliquer les filtres").click()
                expect(
                    page.get_by_role("status").filter(has_text="ligne correspond aux filtres")
                ).to_be_visible()
                with page.expect_download() as download:
                    page.get_by_role("button", name="Exporter ces données en Excel").click()
                file = ROOT / "telechargement-stocks.xlsx"
                download.value.save_as(file)
                self.assertIn(p.name, "".join(sheet(file.read_bytes()).itertext()))
                self.capture(page, "stocks-filtre-1440")
                for width, height in [(1280, 800), (768, 1024), (390, 844), (320, 700)]:
                    page.set_viewport_size({"width": width, "height": height})
                    self.capture(page, "stocks-" + str(width))
                page.goto(self.live_server_url + "/stocks/signalements")
                expect(
                    page.get_by_role("link", name="Stock de la réserve", exact=True)
                ).to_have_attribute("aria-current", "page")
                page.get_by_role("link", name="Remises aux unités", exact=True).click()
                expect(
                    page.get_by_role("link", name="Remises aux unités", exact=True)
                ).to_have_attribute("aria-current", "page")
            finally:
                browser.close()

    def test_effective_chart_and_personal_history(self):
        r, ps = self.prepare((3,))
        self.deliver(r)
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                page = self.page(browser, self.admin)
                page.goto(self.live_server_url + "/analyses/deliveries?product=" + str(ps[0].pk))
                expect(
                    page.get_by_role(
                        "heading", name="Remises effectives par jour pour le produit sélectionné"
                    )
                ).to_be_visible()
                expect(page.locator("meter")).to_have_attribute("value", "3")
                self.capture(page, "livraisons-graphe-1440")
                page.get_by_label("Rechercher dans cette analyse").focus()
                page.keyboard.press("Tab")
                expect(page.get_by_role("button", name="Appliquer les filtres")).to_be_focused()
                requester = self.page(browser, self.assistant, 390, 844)
                requester.get_by_role("link", name="Mon historique autorisé", exact=True).click()
                expect(
                    requester.get_by_role("heading", name="Mon historique autorisé")
                ).to_be_visible()
                expect(
                    requester.get_by_role("button", name="Exporter ces données en Excel")
                ).to_have_count(0)
                self.capture(requester, "historique-personnel-390")
            finally:
                browser.close()

    def test_export_can_repeat_and_shows_server_and_offline_errors(self):
        self.demandable_product()
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                page = self.page(browser, self.admin)
                page.goto(self.live_server_url + "/analyses/stocks")
                button = page.get_by_role("button", name="Exporter ces données en Excel")
                for index in range(2):
                    with page.expect_download() as download:
                        button.click()
                    download.value.save_as(ROOT / f"export-repete-{index}.xlsx")
                    expect(button).to_be_enabled()
                page.route(
                    "**/analyses/stocks/export.xlsx*",
                    lambda route: route.fulfill(
                        status=422,
                        content_type="text/html",
                        body="<main><p>Trop de lignes. Réduisez les filtres.</p></main>",
                    ),
                )
                button.click()
                expect(page.locator("#export-status")).to_have_text(
                    "Trop de lignes. Réduisez les filtres."
                )
                expect(button).to_be_enabled()
                page.unroute("**/analyses/stocks/export.xlsx*")
                page.context.set_offline(True)
                button.click()
                expect(page.locator("#export-status")).to_contain_text("Connexion indisponible")
                page.context.set_offline(False)
                self.capture(page, "export-erreur-et-reprise-1440")
            finally:
                browser.close()
