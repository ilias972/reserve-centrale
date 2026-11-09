"""Chromium réel : reçus identiques et reprise d'une demande Médecin confirmée."""

import json
from pathlib import Path
from unittest.mock import patch

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from PIL import Image, ImageChops
from playwright.sync_api import expect, sync_playwright

from apps.accounts.models import User
from apps.core.models import OperationIntent
from apps.receiving import selectors
from apps.receiving.models import Receipt
from apps.requests.models import InternalRequest
from tests.e2e import test_deliveries as delivery_browser
from tests.test_accounts import FICTIVE_PASSWORD
from tests.test_receiving import ReceivingFixtures

ROOT = Path("docs/recette/A14-REMEDIATION/browser")


class RemediationBrowserTests(ReceivingFixtures, StaticLiveServerTestCase):
    db = delivery_browser.BrowserDeliveryTests.db
    login = delivery_browser.BrowserDeliveryTests.login

    def test_receipts_before_after_identical_desktop_mobile_and_real_detail_link(self):
        products = [self.demandable_product(code=f"PHOTO-RECU-{i}") for i in range(3)]
        order = self.buy(products, [20] * 3)
        self.assertEqual(self.receive(order, [23, 10, 4], [0, 1, 0]).status_code, 201)
        receipt = Receipt.objects.get()
        original = selectors.receipts
        ROOT.mkdir(parents=True, exist_ok=True)
        results = []
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            for width, height in [(1440, 900), (390, 844)]:
                page = browser.new_page(viewport={"width": width, "height": height}, locale="fr-FR")
                errors = []
                page.on("pageerror", lambda e, errors=errors: errors.append(str(e)))
                self.login(page, self.admin)
                before = ROOT / f"receipts-{width}-before.png"
                after = ROOT / f"receipts-{width}-after.png"
                # Même rendu de référence, sans préchargement, mêmes faits PG.
                with patch.object(
                    selectors, "receipts", side_effect=lambda a: original(a).prefetch_related(None)
                ):
                    page.goto(self.live_server_url + "/receptions/historique")
                    page.wait_for_load_state("networkidle")
                    page.screenshot(path=str(before), full_page=True, animations="disabled")
                page.goto(self.live_server_url + "/receptions/historique")
                page.wait_for_load_state("networkidle")
                expect(page.get_by_text(receipt.reference, exact=True)).to_be_visible()
                page.screenshot(path=str(after), full_page=True, animations="disabled")
                with Image.open(before) as a, Image.open(after) as b:
                    identical = (
                        a.size == b.size
                        and ImageChops.difference(a.convert("RGB"), b.convert("RGB")).getbbox()
                        is None
                    )
                self.assertTrue(identical)
                self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), width)
                page.evaluate(Path("tests/assets/axe.min.js").read_text())
                violations = page.evaluate(
                    "async()=>{const r=await axe.run();return r.violations.filter(v=>['critical','serious'].includes(v.impact)).map(v=>v.id)}"
                )
                self.assertEqual(violations, [])
                page.get_by_role("link", name="Voir le reçu", exact=True).click()
                expect(page.get_by_text(receipt.reference, exact=False).first).to_be_visible()
                self.assertIn(f"/receptions/recus/{receipt.pk}", page.url)
                self.assertEqual(errors, [])
                results.append(
                    {
                        "width": width,
                        "pixel_identical": identical,
                        "axe_serious_critical": violations,
                        "js_errors": errors,
                    }
                )
                page.close()
            browser.close()
        (ROOT / "receipts-comparison.json").write_text(json.dumps(results, indent=2))

    def test_doctor_request_lost_response_double_click_reload_and_new_tab_only_once(self):
        doctor = User.objects.create_user(
            "docteur-navigateur",
            FICTIVE_PASSWORD,
            role="DOCTOR",
            floor=self.floor,
            must_change_password=False,
        )
        product = self.demandable_product(physical=100, maximum=100)
        ROOT.mkdir(parents=True, exist_ok=True)
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            context = browser.new_context(
                viewport={"width": 390, "height": 844},
                is_mobile=True,
                has_touch=True,
                locale="fr-FR",
            )
            page = context.new_page()
            self.login(page, doctor)
            page.goto(self.live_server_url + "/demandes/nouvelle")
            page.wait_for_load_state("networkidle")
            page.get_by_label(f"Quantité demandée — {product.name}", exact=True).fill("12")
            page.get_by_role("button", name="Vérifier ma demande", exact=True).click()
            sent = []

            def lose(route):
                response = route.fetch()
                self.assertEqual(response.status, 201)
                sent.append(response.json())
                route.abort("failed")

            page.route("**/api/v1/requests", lose, times=1)
            page.get_by_role("button", name="Envoyer la demande", exact=True).evaluate(
                "(b)=>{b.click();b.click()}"
            )
            expect(page.locator("#operation-result")).to_contain_text(
                "Connexion indisponible pendant"
            )
            self.assertEqual(self.db(lambda: InternalRequest.objects.count()), 1)
            self.assertEqual(self.db(lambda: self.stock(product)[:3]), (100, 12, 88))
            intent = sent[0]["intent_id"]
            pending_count = self.db(lambda: OperationIntent.objects.filter(actor=doctor).count())
            page.screenshot(path=str(ROOT / "doctor-response-lost.png"), full_page=True)
            page.reload()
            expect(
                page.get_by_role("link", name="Suivre cette demande", exact=True)
            ).to_be_visible()
            expect(page.locator("#operation-recovery")).to_be_hidden()
            self.assertEqual(
                self.db(lambda: OperationIntent.objects.filter(actor=doctor).count()), pending_count
            )
            reference = sent[0]["resource"]["reference"]
            page.close()
            again = context.new_page()
            again.goto(self.live_server_url + "/demandes")
            expect(again.get_by_role("link", name=reference, exact=True)).to_be_visible()
            outcome = context.request.get(
                self.live_server_url + f"/api/v1/operation-intents/{intent}"
            )
            self.assertEqual(outcome.status, 200)
            self.assertEqual(outcome.json()["state"], "CONFIRMED")
            self.assertEqual(self.db(lambda: InternalRequest.objects.count()), 1)
            self.assertEqual(
                self.db(lambda: OperationIntent.objects.filter(actor=doctor).count()), pending_count
            )
            self.assertEqual(self.db(lambda: self.stock(product)[:3]), (100, 12, 88))
            again.screenshot(path=str(ROOT / "doctor-request-recovered.png"), full_page=True)
            (ROOT / "doctor-recovery.json").write_text(
                json.dumps(
                    {
                        "intent_id": intent,
                        "request_count": 1,
                        "P": 100,
                        "R": 12,
                        "D": 88,
                        "new_intention_after_recovery": False,
                        "physical_iphone": False,
                    },
                    indent=2,
                )
            )
            browser.close()
