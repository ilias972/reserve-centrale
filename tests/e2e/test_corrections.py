"""Recette M6 réelle Gestionnaire, préparateur et demandeur — profil A ; PostgreSQL isolé."""

import json
from pathlib import Path

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from playwright.sync_api import expect, sync_playwright

from apps.cases.models import Case, StockHold
from apps.requests.models import Delivery, DeliveryCorrection
from tests.e2e import test_deliveries as delivery_browser
from tests.test_accounts import FICTIVE_PASSWORD
from tests.test_corrections import CorrectionFixtures

ROOT = Path("docs/recette/M6/browser")


class BrowserCorrectionTests(CorrectionFixtures, StaticLiveServerTestCase):
    db = delivery_browser.BrowserDeliveryTests.db
    login = delivery_browser.BrowserDeliveryTests.login

    def page(self, browser, user, width=1440, height=900):
        page = browser.new_page(
            viewport={"width": width, "height": height},
            has_touch=width == 390,
            is_mobile=width == 390,
            locale="fr-FR",
        )
        page.on("pageerror", lambda e: self.errors.append(str(e)))
        page.on("response", lambda r: self.failures.append(r.status) if r.status >= 500 else None)
        self.login(page, user)
        return page

    def setUp(self):
        super().setUp()
        ROOT.mkdir(parents=True, exist_ok=True)
        self.errors = []
        self.failures = []

    def inspect(self, page, name):
        page.wait_for_load_state("networkidle")
        page.evaluate("document.fonts.ready")
        self.assertLessEqual(
            page.evaluate("document.documentElement.scrollWidth"), page.viewport_size["width"]
        )
        page.evaluate(Path("tests/assets/axe.min.js").read_text())
        violations = page.evaluate(
            "async()=>{const r=await axe.run();return r.violations.filter(v=>['serious','critical'].includes(v.impact)).map(v=>({id:v.id,targets:v.nodes.map(n=>n.target)}));}"
        )
        self.assertEqual(violations, [], name)
        expect(page.locator('a[href="#"],a[href^="javascript:"]')).to_have_count(0)
        page.screenshot(path=str(ROOT / (name + ".png")), full_page=True)

    def fill_verification(self, page):
        review = page.locator(".case-review")
        if review.count() and not review.evaluate("node=>node.open"):
            review.locator("summary").click()
        if review.count():
            expect(review).to_have_attribute("open", "")
            message = review.locator("#case-message")
        else:
            message = page.locator("#case-message")
        expect(message).to_be_visible()
        message.fill("Quantités fictives recomptées et conséquences vérifiées.")
        page.locator("#case-verification").check()

    def follow_success(self, page):
        expect(page.locator("#case-form")).to_be_hidden()
        page.wait_for_load_state("networkidle")
        link = page.get_by_role("link", name="Consulter le dossier à jour", exact=True)
        if link.is_visible():
            link.click()
            page.wait_for_load_state("networkidle")
        else:
            expect(page.locator(".dossier-summary")).to_be_visible()

    def test_report_review_correct_10_to_8_three_roles_and_visual_matrix(self):
        request, products = self.prepare((10,))
        self.deliver(request)
        delivery = Delivery.objects.get(request=request)
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            operator = self.page(browser, self.replenisher, 390, 844)
            operator.goto(self.live_server_url + f"/livraisons/{delivery.pk}/signaler")
            operator.get_by_label("Précision facultative", exact=True).fill(
                "Huit unités remises, dix déclarées, situation fictive."
            )
            operator.get_by_role("button", name="Confirmer le signalement", exact=True).click()
            expect(
                operator.get_by_text(
                    "Une erreur a déjà été signalée pour cette remise.", exact=True
                )
            ).to_be_visible()
            case_id = self.db(lambda: str(Case.objects.get(delivery_id=delivery.pk).pk))
            admin = self.page(browser, self.admin)
            admin.get_by_role("link", name="Signalements", exact=True).click()
            admin.get_by_role("link", name="Remises aux unités", exact=True).click()
            admin.get_by_role("link", name="Examiner le signalement", exact=True).click()
            self.fill_verification(admin)
            admin.get_by_role("button", name="Commencer la vérification", exact=True).click()
            self.follow_success(admin)
            expect(admin.get_by_text("En vérification", exact=False).first).to_be_visible()
            admin.get_by_role(
                "link", name="Corriger les quantités réellement remises", exact=True
            ).click()
            admin.locator("[data-correct-line]").fill("8")
            self.fill_verification(admin)
            admin.get_by_role("button", name="Prévisualiser les conséquences", exact=True).click()
            expect(admin.locator("#case-preview")).to_contain_text("en réserve : 0 → 2")
            self.assertEqual(self.db(lambda: self.stock(products[0])[:3]), (0, 0, 0))
            for w, h in [(1440, 900), (1280, 800), (390, 844)]:
                admin.set_viewport_size({"width": w, "height": h})
                self.inspect(admin, f"correction-preview-{w}")
            admin.get_by_role("button", name="Confirmer la correction", exact=True).click()
            self.follow_success(admin)
            expect(admin.locator("main")).to_contain_text("Résolu")
            expect(admin.locator("main")).to_contain_text("10 → 8")
            admin.set_viewport_size({"width": 1440, "height": 900})
            self.inspect(admin, "dossier-corrige")
            assistant = self.page(browser, self.assistant)
            assistant.goto(self.live_server_url + f"/demandes/{request.pk}")
            expect(assistant.locator("#request-status")).to_contain_text("clôturée")
            expect(
                assistant.get_by_role("button", name="Annuler ma demande", exact=True)
            ).to_have_count(0)
            expect(assistant.locator("main")).to_contain_text(
                "Livraison corrigée par la gestionnaire"
            )
            operator.goto(self.live_server_url + f"/demandes/{request.pk}")
            expect(operator.locator("main")).to_contain_text("8 après correction Gestionnaire")
            self.assertEqual(self.db(lambda: self.stock(products[0])[:3]), (2, 0, 2))
            self.assertEqual(self.db(lambda: DeliveryCorrection.objects.count()), 1)
            self.assertEqual(self.errors, [])
            self.assertEqual(self.failures, [])
            browser.close()
        (ROOT / "three-roles.json").write_text(
            json.dumps(
                {
                    "case_id": case_id,
                    "original_handed": 10,
                    "effective_handed": 8,
                    "P_R_D": [2, 0, 2],
                    "physical_iphone": False,
                    "errors": self.errors,
                    "http_5xx": self.failures,
                },
                indent=2,
            )
            + "\n"
        )

    def test_reconcile_chosen_commitment_then_assistant_can_cancel_before_first_delivery(self):
        request, products, case = self.conflict()
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            admin = self.page(browser, self.admin)
            admin.goto(self.live_server_url + f"/stocks/signalements/{case.pk}")
            admin.get_by_role(
                "link", name="Vérifier le comptage et les réservations", exact=True
            ).click()
            admin.locator("[data-release-line]").fill("2")
            self.fill_verification(admin)
            admin.get_by_role("button", name="Prévisualiser les conséquences", exact=True).click()
            expect(admin.locator("#case-preview")).to_contain_text("réservé : 8 → 6")
            self.assertEqual(self.db(lambda: self.stock(products[0])[:3]), (10, 8, 2))
            self.inspect(admin, "regularisation-preview")
            admin.get_by_role("button", name="Confirmer la régularisation", exact=True).click()
            self.follow_success(admin)
            expect(admin.locator("main")).to_contain_text("Résolu")
            self.assertEqual(self.db(lambda: StockHold.objects.count()), 0)
            assistant = self.page(browser, self.assistant)
            assistant.goto(self.live_server_url + f"/demandes/{request.pk}")
            expect(assistant.locator("main")).to_contain_text(
                "Engagement réduit après comptage vérifié"
            )
            assistant.get_by_role("button", name="Annuler ma demande", exact=True).click()
            assistant.get_by_role("button", name="Confirmer l'annulation", exact=True).click()
            expect(assistant.locator("#request-status")).to_have_text("Annulée")
            self.assertEqual(self.db(lambda: self.stock(products[0])[:3]), (6, 0, 6))
            self.assertEqual(self.errors, [])
            self.assertEqual(self.failures, [])
            browser.close()

    def test_abandon_reopen_and_reassign_guided_forms(self):
        from apps.accounts.models import User

        request, products = self.prepare((10,))
        self.deliver(request, [4], "LATER")
        other = User.objects.create_user(
            "reappro-ui-fictif",
            FICTIVE_PASSWORD,
            role="REPLENISHER",
            must_change_password=False,
            display_name="Opérateur fictif de remplacement",
        )
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            admin = self.page(browser, self.admin)
            admin.goto(self.live_server_url + f"/demandes/{request.pk}")
            admin.get_by_role("link", name="Clôturer le reliquat pour abandon", exact=True).click()
            self.fill_verification(admin)
            admin.get_by_role("button", name="Prévisualiser les conséquences", exact=True).click()
            expect(admin.locator("#case-preview")).to_contain_text("réservé : 6 → 0")
            self.inspect(admin, "cloture-preview")
            admin.get_by_role("button", name="Confirmer la clôture", exact=True).click()
            self.follow_success(admin)
            admin.get_by_role("link", name="Rouvrir la demande", exact=True).click()
            admin.locator("[data-reopen-line]").fill("2")
            self.fill_verification(admin)
            admin.get_by_role("button", name="Prévisualiser les conséquences", exact=True).click()
            expect(admin.locator("#case-preview")).to_contain_text("réservé : 0 → 2")
            admin.get_by_role("button", name="Confirmer la réouverture", exact=True).click()
            self.follow_success(admin)
            admin.get_by_role("link", name="Réattribuer le reste à livrer", exact=True).click()
            admin.locator("#case-operator").select_option(str(other.pk))
            self.fill_verification(admin)
            admin.get_by_role("button", name="Prévisualiser les conséquences", exact=True).click()
            expect(admin.locator("#case-preview")).to_contain_text(other.display_name)
            self.inspect(admin, "reattribution-preview")
            admin.get_by_role("button", name="Confirmer la réattribution", exact=True).click()
            self.follow_success(admin)
            self.assertEqual(self.db(lambda: self.stock(products[0])[:3]), (6, 2, 4))
            self.assertEqual(self.errors, [])
            self.assertEqual(self.failures, [])
            browser.close()

    def test_lost_confirmation_response_is_recovered_once_and_offline_keeps_input(self):
        request, products, delivery, case = self.completed()
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = self.page(browser, self.admin, 390, 844)
            page.goto(self.live_server_url + f"/dossiers/{case.pk}/corriger")
            page.locator("[data-correct-line]").fill("8")
            self.fill_verification(page)
            page.context.set_offline(True)
            expect(
                page.get_by_role("button", name="Prévisualiser les conséquences", exact=True)
            ).to_be_disabled()
            expect(page.locator("[data-correct-line]")).to_have_value("8")
            page.context.set_offline(False)
            page.get_by_role("button", name="Prévisualiser les conséquences", exact=True).click()
            expect(page.locator("#case-preview")).to_be_visible()

            def lose(route):
                response = route.fetch()
                self.assertEqual(response.status, 200)
                route.abort("failed")

            page.route("**/api/v1/corrections/confirm", lose)
            page.locator("#case-confirm").evaluate("n=>{n.click();n.click();}")
            expect(page.locator("#operation-result")).to_contain_text("Vérifiez")
            page.unroute("**/api/v1/corrections/confirm", lose)
            page.get_by_role("button", name="Vérifier l'enregistrement", exact=True).click()
            self.follow_success(page)
            self.assertEqual(self.db(lambda: DeliveryCorrection.objects.count()), 1)
            self.assertEqual(self.db(lambda: self.stock(products[0])[:3]), (2, 0, 2))
            self.inspect(page, "reprise-reseau-mobile")
            self.assertEqual(self.errors, [])
            self.assertEqual(self.failures, [])
            browser.close()
