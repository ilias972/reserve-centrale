"""Chromium réel : rupture couverte, réactivation et navigation préservée."""

import json
from pathlib import Path

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from playwright.sync_api import expect, sync_playwright

from tests.e2e import test_corrections as browser_helpers
from tests.test_notifications import NotificationFixtures

ROOT = Path("docs/recette/A11-FINAL/browser")


class A11BrowserTests(NotificationFixtures, StaticLiveServerTestCase):
    db = browser_helpers.BrowserCorrectionTests.db
    login = browser_helpers.BrowserCorrectionTests.login
    page = browser_helpers.BrowserCorrectionTests.page

    def setUp(self):
        super().setUp()
        ROOT.mkdir(parents=True, exist_ok=True)
        self.errors = []
        self.failures = []

    def capture(self, page, name):
        page.wait_for_load_state("networkidle")
        self.assertLessEqual(
            page.evaluate("document.documentElement.scrollWidth"), page.viewport_size["width"]
        )
        page.evaluate(Path("tests/assets/axe.min.js").read_text())
        violations = page.evaluate(
            "async()=>{const r=await axe.run();return r.violations.filter(v=>['serious','critical'].includes(v.impact)).map(v=>({id:v.id,targets:v.nodes.map(n=>n.target)}));}"
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
                    "physical_iphone_tested": False,
                },
                indent=2,
            )
        )

    def test_covered_shortage_visible_pc_and_mobile_no_false_purchase_badge(self):
        p = self.demandable_product(physical=0)
        self.rec_action(p, "defer")
        self.buy([p], [20])
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            try:
                for width, height in [(1440, 900), (390, 844), (320, 700)]:
                    page = self.page(browser, self.admin, width, height)
                    page.goto(self.live_server_url + "/achats/besoins")
                    expect(page.get_by_text("Rupture de stock — 20", exact=False)).to_be_visible()
                    page.get_by_role("link", name="Consulter le besoin", exact=True).click()
                    expect(page.get_by_text("Rupture de stock — 20", exact=False)).to_be_visible()
                    expect(
                        page.get_by_role("link", name="Mettre en attente", exact=True)
                    ).to_have_count(0)
                    self.capture(page, "rupture-couverte-" + str(width))
                    page.close()
            finally:
                browser.close()

    def test_real_defer_then_reservation_reactivation_notification_and_tabs(self):
        p = self.demandable_product(physical=2)
        self.process()
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            try:
                page = self.page(browser, self.admin)
                page.goto(self.live_server_url + f"/achats/besoins/{p.pk}")
                page.get_by_role("link", name="Mettre en attente", exact=True).click()
                page.get_by_role("button", name="Confirmer la mise en attente", exact=True).click()
                expect(
                    page.get_by_role("link", name="Consulter les données à jour", exact=True)
                ).to_be_visible()
                page.get_by_role("link", name="Consulter les données à jour", exact=True).click()
                expect(page.get_by_role("link", name="Réactiver", exact=True)).to_be_visible()
                self.db(lambda: self.send([p], [1]))
                self.db(self.process)
                page.reload()
                expect(
                    page.get_by_role("link", name="Mettre en attente", exact=True)
                ).to_be_visible()
                self.capture(page, "besoin-reactive-1440")
                page.goto(self.live_server_url + "/notifications")
                expect(page.get_by_text("Besoin d’achat réactivé", exact=True)).to_be_visible()
                self.capture(page, "notification-reactivation-1440")
                page.goto(self.live_server_url + "/stocks/signalements")
                stock = page.get_by_role("link", name="Stock de la réserve", exact=True)
                expect(stock).to_have_attribute("aria-current", "page")
                page.get_by_role("link", name="Remises aux unités", exact=True).click()
                expect(
                    page.get_by_role("link", name="Remises aux unités", exact=True)
                ).to_have_attribute("aria-current", "page")
                self.capture(page, "deux-onglets-preserves-1440")
            finally:
                browser.close()
