"""Recette des dossiers sur PostgreSQL isolé et Chromium réel."""

import os
from pathlib import Path
from urllib.parse import urlsplit

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from playwright.sync_api import sync_playwright

from tests.e2e import test_corrections as browser_helpers
from tests.test_signalements import SignalementFixtures

ROOT = Path("docs/recette/UI-FIX-SIGNALEMENTS")


class SignalementCaptureTests(SignalementFixtures, StaticLiveServerTestCase):
    page = browser_helpers.BrowserCorrectionTests.page
    db = browser_helpers.BrowserCorrectionTests.db
    login = browser_helpers.BrowserCorrectionTests.login

    def setUp(self):
        super().setUp()
        self.errors = []
        self.failures = []

    # Les parcours M6 ne sont pas hérités une seconde fois.
    def test_signalement_source_comparison(self):
        _, _, case = self.conflict()
        phase = os.environ.get("UI_FIX_PHASE", "after")
        target = ROOT / phase / "captures"
        target.mkdir(parents=True, exist_ok=True)
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = self.page(browser, self.admin)
            for width, height in [(1440, 900), (390, 844)]:
                page.set_viewport_size({"width": width, "height": height})
                for name, path in [
                    ("stock", "/stocks/signalements"),
                    ("dossier", f"/dossiers/{case.pk}"),
                    ("remises", "/livraisons/signalements"),
                ]:
                    page.goto(self.live_server_url + path)
                    page.wait_for_load_state("networkidle")
                    page.screenshot(path=str(target / f"{name}-{width}.png"), full_page=True)
            browser.close()

    def check_screen(self, page, name):
        import json

        from playwright.sync_api import expect

        page.wait_for_load_state("networkidle")
        self.assertLessEqual(
            page.evaluate("document.documentElement.scrollWidth"), page.viewport_size["width"]
        )
        page.evaluate(Path("tests/assets/axe.min.js").read_text())
        violations = page.evaluate(
            "async()=>{const r=await axe.run();return r.violations.filter(v=>['serious','critical'].includes(v.impact)).map(v=>({id:v.id,targets:v.nodes.map(n=>n.target)}));}"
        )
        self.assertEqual(violations, [], name)
        expect(page.locator('a[href="#"],a[href^="javascript:"],button:not([type])')).to_have_count(
            0
        )
        target = ROOT / os.environ.get("UI_FIX_PHASE", "after") / "captures"
        target.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(target / (name + ".png")), full_page=True)
        controls = page.locator(
            "main a,main button,main input,main select,main textarea,main summary"
        ).evaluate_all(
            "nodes=>nodes.map(n=>({tag:n.tagName,text:n.textContent.trim(),href:n.getAttribute('href'),type:n.type,visible:n.getClientRects().length>0}))"
        )
        (target / (name + ".json")).write_text(
            json.dumps({"axe_serious_critical": violations, "controls": controls}, indent=2) + "\n"
        )

    def verification(self, page):
        page.locator("#case-message").fill(
            "Quantités et engagements fictifs vérifiés personnellement."
        )
        page.locator("#case-verification").check()

    def test_tabs_filters_search_direct_legacy_and_responsive(self):
        from playwright.sync_api import expect

        _, products, case = self.conflict()
        self.db(lambda: self.adjust(products, [12]))
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = self.page(browser, self.admin)
            page.goto(self.live_server_url + "/stocks/signalements")
            expect(
                page.get_by_role("link", name="Stock de la réserve", exact=True)
            ).to_have_attribute("aria-current", "page")
            page.get_by_label("Rechercher un produit ou un auteur", exact=True).fill("inexistant")
            page.get_by_role("button", name="Rechercher", exact=True).click()
            expect(page.get_by_role("status").filter(has_text="Aucun signalement")).to_contain_text(
                "recherche"
            )
            page.get_by_label("Rechercher un produit ou un auteur", exact=True).fill(
                products[0].code
            )
            page.get_by_role("button", name="Rechercher", exact=True).click()
            page.get_by_role("link", name="Examiner le signalement", exact=True).click()
            expect(page.locator(".dossier-original")).to_contain_text("10")
            expect(page.locator(".dossier-current")).to_contain_text("12")
            page.reload()
            expect(
                page.get_by_role("link", name="Retour aux signalements de stock", exact=False)
            ).to_have_attribute(
                "href", f"/stocks/signalements?state=pending&q={products[0].code}&offset=0"
            )
            for width, height in [(1440, 900), (1280, 800), (768, 1024), (390, 844), (320, 700)]:
                page.set_viewport_size({"width": width, "height": height})
                self.check_screen(page, f"dossier-actuel-{width}")
                expect(
                    page.get_by_role(
                        "link", name="Vérifier le comptage et les réservations", exact=True
                    )
                ).to_be_visible()
            page.goto(self.live_server_url + f"/stocks/signalements/{case.stock_case_id}")
            expect(
                page.get_by_role("heading", name="Situation actuelle", exact=True)
            ).to_be_visible()
            page.get_by_role("link", name="Remises aux unités", exact=True).click()
            expect(
                page.get_by_role("link", name="Remises aux unités", exact=True)
            ).to_have_attribute("aria-current", "page")
            page.get_by_label("Afficher", exact=True).select_option("resolved")
            page.get_by_role("button", name="Rechercher", exact=True).click()
            expect(page.locator("#signalement-results")).to_contain_text("Aucun signalement résolu")
            self.check_screen(page, "remises-vide-320")
            browser.close()
        self.assertEqual(self.errors, [])
        self.assertEqual(self.failures, [])

    def test_review_and_targeted_reconciliation_from_single_dossier(self):
        from playwright.sync_api import expect

        from apps.cases.models import StockHold
        from apps.inventory.models import StockEntry
        from apps.requests.models import InternalRequest

        request, products = self.requested((8,))
        other_response = self.send(products, [1])
        other = InternalRequest.objects.get(pk=other_response.json()["resource"]["id"])
        response = self.adjust(products, [6])
        case_id = response.json()["case_id"]
        before = StockEntry.objects.count()
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = self.page(browser, self.admin)
            page.goto(self.live_server_url + f"/dossiers/{case_id}")
            page.locator(".case-review summary").click()
            self.verification(page)
            page.get_by_role("button", name="Commencer la vérification", exact=True).click()
            page.wait_for_url("**/dossiers/*?from=stock")
            expect(page.locator(".dossier-summary")).to_contain_text("En vérification")
            self.assertEqual(self.db(lambda: StockEntry.objects.count()), before)
            page.get_by_role(
                "link", name="Vérifier le comptage et les réservations", exact=True
            ).click()
            # Deux demandes, aucune libération présélectionnée.
            expect(page.locator("[data-release-line]")).to_have_count(2)
            for n in page.locator("[data-release-line]").all():
                expect(n).to_have_value("0")
            main_line = self.db(lambda: str(request.lines.get().pk))
            other_line = self.db(lambda: str(other.lines.get().pk))
            page.locator(f'[data-release-line="{main_line}"]').fill("2")
            page.locator(f'[data-release-line="{other_line}"]').fill("1")
            self.verification(page)
            page.get_by_role("button", name="Prévisualiser les conséquences", exact=True).click()
            expect(page.locator("#case-preview")).to_contain_text("réservé : 9 → 6")
            self.check_screen(page, "regularisation-multi-demandes")
            page.get_by_role("button", name="Confirmer la régularisation", exact=True).click()
            page.wait_for_url("**/dossiers/*?from=stock")
            expect(page.locator(".dossier-summary")).to_contain_text("Résolu")
            self.assertEqual(self.db(lambda: self.stock(products[0])[:3]), (6, 6, 0))
            self.assertEqual(self.db(lambda: StockHold.objects.count()), 0)
            browser.close()
        self.assertEqual(self.errors, [])
        self.assertEqual(self.failures, [])

    def test_successive_corrections_keep_initial_and_closed_request(self):
        from playwright.sync_api import expect

        from apps.requests.models import DeliveryCorrection

        request, products, delivery, case = self.completed()
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = self.page(browser, self.admin)
            page.goto(self.live_server_url + f"/dossiers/{case.pk}?from=deliveries")
            for amount, before in [(8, 10), (7, 8)]:
                page.get_by_role(
                    "link", name="Corriger les quantités réellement remises", exact=True
                ).click()
                expect(page.locator("[data-correct-line]")).to_have_value(str(before))
                page.locator("[data-correct-line]").fill(str(amount))
                self.verification(page)
                page.get_by_role(
                    "button", name="Prévisualiser les conséquences", exact=True
                ).click()
                expect(page.locator("#case-preview")).to_contain_text("La demande restera close.")
                page.get_by_role("button", name="Confirmer la correction", exact=True).click()
                page.wait_for_url("**/dossiers/*?from=deliveries")
                expect(page.locator(".dossier-summary")).to_contain_text("Résolu")
            expect(page.locator(".dossier-original")).to_contain_text("10")
            expect(page.locator(".dossier-current")).to_contain_text("8 → 7")
            self.assertEqual(self.db(lambda: DeliveryCorrection.objects.count()), 2)
            self.assertEqual(self.db(lambda: self.stock(products[0])[:3]), (3, 0, 3))
            self.check_screen(page, "remise-corrections-successives")
            browser.close()
        self.assertEqual(self.errors, [])
        self.assertEqual(self.failures, [])

    def test_shortage_verified_resolution_lost_reply_and_no_double_write(self):
        from playwright.sync_api import expect

        from apps.cases.models import CaseEvent
        from apps.inventory.models import StockEntry

        request, products, case = self.shortage((4, 3))
        before = StockEntry.objects.count()
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = self.page(browser, self.admin, 390, 844)
            static_responses = []
            static_failures = []
            navigation_requests = []
            page.on(
                "response",
                lambda r: (
                    static_responses.append((urlsplit(r.url).path, r.status))
                    if urlsplit(r.url).path.startswith("/static/")
                    else None
                ),
            )
            page.on(
                "requestfailed",
                lambda r: (
                    static_failures.append(urlsplit(r.url).path)
                    if urlsplit(r.url).path.startswith("/static/")
                    else None
                ),
            )
            page.on(
                "request",
                lambda r: (
                    navigation_requests.append({"method": r.method, "path": urlsplit(r.url).path})
                    if r.is_navigation_request()
                    else None
                ),
            )
            page.goto(self.live_server_url + f"/dossiers/{case.pk}")
            page.get_by_role("link", name="Confirmer la vérification du manque", exact=True).click()
            count_fields = page.locator("[data-count-product]").all()
            self.assertTrue(count_fields, {"path": urlsplit(page.url).path})
            for n in count_fields:
                expect(n).to_have_value("")
            for n in count_fields:
                n.fill("0")
                expect(n).to_have_value("0")
            self.verification(page)
            counted_values_before_submit = page.locator("[data-count-product]").evaluate_all(
                "nodes => nodes.map(node => node.value)"
            )
            self.assertTrue(
                page.evaluate("typeof window.ReserveRequestOperation === 'function'"),
                {"url": page.url, "static": static_responses, "failed_static": static_failures},
            )
            page.get_by_role("button", name="Vérifier avant de confirmer", exact=True).click()
            page.wait_for_timeout(5000)
            result = page.locator("#operation-result").inner_text()
            self.assertIn(
                "Le comptage diffère",
                result,
                page.evaluate(
                    """extra => ({
                    url: location.pathname,
                    ...extra,
                    formValid: document.querySelector('#case-form').checkValidity(),
                    disabled: [...document.querySelectorAll('#case-form input, #case-form textarea, #case-form button')]
                      .map(n => ({id:n.id,disabled:n.disabled,value:n.type === 'checkbox' ? n.checked : n.value})),
                    result: document.querySelector('#operation-result').innerText,
                    previewHidden: document.querySelector('#case-preview').hidden
                })""",
                    {
                        "static": static_responses,
                        "failed_static": static_failures,
                        "page_error_kinds": [error.split(":", 1)[0][:40] for error in self.errors],
                        "counted_values_before_submit": counted_values_before_submit,
                        "navigation_requests": navigation_requests,
                    },
                ),
            )
            expect(page.locator("#case-confirm")).to_be_hidden()
            for product in products:
                page.locator(f'[data-count-product="{product.pk}"]').fill(
                    str(self.db(lambda product=product: self.stock(product)[0]))
                )
            page.get_by_role("button", name="Vérifier avant de confirmer", exact=True).click()
            expect(page.locator("#case-preview")).to_contain_text(
                "La demande, ses quantités et son état restent inchangés."
            )
            self.check_screen(page, "manque-verifie-preview-390")

            def lose(route):
                response = route.fetch()
                self.assertEqual(response.status, 200)
                route.abort("failed")

            page.route("**/api/v1/cases/*/resolve", lose)
            page.locator("#case-confirm").evaluate("n=>{n.click();n.click();}")
            expect(page.locator("#operation-result")).to_contain_text("Vérifiez")
            page.unroute("**/api/v1/cases/*/resolve", lose)
            page.get_by_role("button", name="Vérifier l'enregistrement", exact=True).click()
            page.wait_for_url("**/dossiers/*?from=deliveries")
            expect(page.locator(".dossier-summary")).to_contain_text("Résolu")
            expect(
                page.get_by_role("link", name="Confirmer la vérification du manque", exact=True)
            ).to_have_count(0)
            self.assertEqual(self.db(lambda: StockEntry.objects.count()), before)
            self.assertEqual(
                self.db(
                    lambda: CaseEvent.objects.filter(case=case, kind="SHORTAGE_VERIFIED").count()
                ),
                1,
            )
            self.check_screen(page, "manque-resolu-390")
            browser.close()
        self.assertEqual(self.errors, [])
        self.assertEqual(self.failures, [])

    def test_shortage_obsolete_version_and_offline_preserve_input(self):
        from playwright.sync_api import expect

        from apps.cases.models import Case

        _, products, case = self.shortage()
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = self.page(browser, self.admin)
            page.goto(self.live_server_url + f"/dossiers/{case.pk}/verifier-manque")
            page.locator("[data-count-product]").fill("6")
            self.verification(page)
            page.context.set_offline(True)
            expect(page.locator("#case-submit")).to_be_disabled()
            expect(page.locator("[data-count-product]")).to_have_value("6")
            page.context.set_offline(False)
            page.get_by_role("button", name="Vérifier avant de confirmer", exact=True).click()
            self.db(lambda: self.adjust(products, [9]))
            page.get_by_role(
                "button", name="Confirmer la vérification du manque", exact=True
            ).click()
            expect(page.locator("#operation-result")).to_contain_text("Les données ont changé")
            self.assertEqual(self.db(lambda: Case.objects.get(pk=case.pk).status), "OPEN")
            self.check_screen(page, "manque-version-perimee")
            browser.close()
        self.assertEqual(self.errors, [])
        self.assertEqual(self.failures, [])

    def test_many_products_long_history_keyboard_and_read_error(self):
        from playwright.sync_api import expect

        from apps.cases.models import Case

        request, products = self.requested((8, 8, 8, 8, 8))
        response = self.adjust(products, [6] * 5)
        case = Case.objects.get(pk=response.json()["case_id"])
        for version in range(12):
            response = self.post_json(
                f"/api/v1/cases/{case.pk}/review",
                {
                    "expected_version": version,
                    "verification": True,
                    "message": f"Note fictive {version + 1} : comptage et engagements en cours de vérification.",
                },
                self.admin_client,
            )
            self.assertEqual(response.status_code, 200, response.content)
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = self.page(browser, self.admin, 390, 844)
            page.goto(self.live_server_url + f"/dossiers/{case.pk}")
            expect(page.locator(".signalement-history li")).to_have_count(13)
            page.locator(".case-review summary").focus()
            page.keyboard.press("Enter")
            expect(page.locator("#case-message")).to_be_visible()
            page.keyboard.press("Tab")
            expect(page.locator("#case-message")).to_be_focused()
            self.check_screen(page, "cinq-produits-historique-long-390")
            page.goto(self.live_server_url + "/stocks/signalements")

            def failure(route):
                route.fulfill(
                    status=503, content_type="text/plain", body="Indisponibilité de recette simulée"
                )

            page.route("**/stocks/signalements*", failure)
            expect(page.locator("#live-status")).to_contain_text(
                "Actualisation indisponible", timeout=12000
            )
            # Le problème réseau n'est jamais transformé en liste vide ou en résolution.
            expect(
                page.get_by_role("link", name="Examiner le signalement", exact=True)
            ).to_be_visible()
            self.check_screen(page, "liste-erreur-serveur-503")
            page.unroute("**/stocks/signalements*", failure)
            browser.close()
        self.assertEqual(self.errors, [])
        self.assertTrue(self.failures)
        self.assertEqual(set(self.failures), {503})

    def test_real_chromium_zoom_200_percent_on_dossier(self):
        import json
        from tempfile import TemporaryDirectory

        _, _, case = self.conflict()
        extension = Path("tests/assets/zoom-extension").resolve()
        with sync_playwright() as pw, TemporaryDirectory() as profile:
            context = pw.chromium.launch_persistent_context(
                profile,
                channel="chromium",
                headless=True,
                viewport={"width": 1440, "height": 900},
                args=[f"--disable-extensions-except={extension}", f"--load-extension={extension}"],
            )
            page = context.new_page()
            self.login(page, self.admin)
            page.goto(self.live_server_url + f"/dossiers/{case.pk}")
            worker = (
                context.service_workers[0]
                if context.service_workers
                else context.wait_for_event("serviceworker")
            )
            self.assertEqual(worker.evaluate("(factor)=>setRecetteZoom(factor)", 2), 2)
            self.assertLessEqual(
                page.evaluate("document.documentElement.scrollWidth"), page.evaluate("innerWidth")
            )
            target = ROOT / os.environ.get("UI_FIX_PHASE", "after") / "captures"
            target.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(target / "dossier-zoom200.png"), full_page=True)
            (target / "dossier-zoom200.json").write_text(
                json.dumps(
                    {
                        "chrome_zoom": 2,
                        "innerWidth": page.evaluate("innerWidth"),
                        "physical_iphone": False,
                    }
                )
                + "\n"
            )
            context.close()
