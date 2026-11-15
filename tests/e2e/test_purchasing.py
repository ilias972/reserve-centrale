"""Parcours réels M7 Gestionnaire ; aucun fournisseur externe, achat réel ou e-mail."""

import json
import os
from pathlib import Path
from urllib.parse import urlsplit

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from playwright.sync_api import expect, sync_playwright

from apps.notifications.models import BusinessEvent
from apps.purchasing.models import PurchaseOrder, RecommendationEvent
from tests.e2e import test_deliveries as delivery_browser
from tests.test_purchasing import PurchaseFixtures

ROOT = Path(os.environ.get("M7_BROWSER_CAPTURE_ROOT", "docs/recette/M7/browser"))


class BrowserPurchaseTests(PurchaseFixtures, StaticLiveServerTestCase):
    db = delivery_browser.BrowserDeliveryTests.db
    login = delivery_browser.BrowserDeliveryTests.login

    def setUp(self):
        super().setUp()
        ROOT.mkdir(parents=True, exist_ok=True)
        self.errors = []
        self.http_errors = []

    def page(self, browser, width=1440, height=900):
        page = browser.new_page(
            viewport={"width": width, "height": height},
            locale="fr-FR",
            is_mobile=width == 390,
            has_touch=width == 390,
        )
        page.on("pageerror", lambda e: self.errors.append(str(e)))
        page.on(
            "response", lambda r: self.http_errors.append(r.status) if r.status >= 500 else None
        )
        self.login(page, self.admin)
        return page

    def inspect(self, page, name):
        page.wait_for_load_state("networkidle")
        page.evaluate("document.fonts.ready")
        self.assertLessEqual(
            page.evaluate("document.documentElement.scrollWidth"), page.viewport_size["width"]
        )
        expect(page.locator('a[href="#"],a[href^="javascript:"]')).to_have_count(0)
        page.evaluate(Path("tests/assets/axe.min.js").read_text())
        serious = page.evaluate(
            "async()=>{const r=await axe.run();return r.violations.filter(v=>['serious','critical'].includes(v.impact)).map(v=>({id:v.id,targets:v.nodes.map(n=>n.target)}));}"
        )
        self.assertEqual(serious, [], name)
        page.screenshot(path=str(ROOT / (name + ".png")), full_page=True)
        (ROOT / (name + ".json")).write_text(
            json.dumps(
                {
                    "serious_or_critical_axe": serious,
                    "width": page.viewport_size["width"],
                    "physical_iphone": False,
                },
                indent=2,
            )
            + "\n"
        )

    def follow(self, page):
        expect(page.locator("#operation-result")).to_contain_text("historisé")
        page.get_by_role("link", name="Consulter les données à jour", exact=True).click()
        page.wait_for_load_state("networkidle")

    def attest_and_review(self, page):
        page.get_by_label(
            "Je confirme que cette commande a réellement été passée auprès du fournisseur.",
            exact=True,
        ).check()
        page.get_by_role("button", name="Vérifier le récapitulatif", exact=True).click()
        expect(page.locator("#purchase-review")).to_be_visible()

    def test_need_defer_reactivate_external_site_then_actual_multi_product_declaration(self):
        p = self.demandable_product(code="FICTIF-BESOIN", physical=2)
        second = self.demandable_product(code="FICTIF-SECOND", physical=1)
        p.refresh_from_db()
        response = self.post_json(
            f"/api/v1/products/{p.pk}/supplier-links",
            {
                "expected_version": p.version,
                "suppliers": [
                    {
                        "supplier_id": str(self.supplier.pk),
                        "preferred": True,
                        "reference": "FOURN-CARTON",
                        "website": "",
                        "indicative_price": None,
                        "currency": "",
                    }
                ],
            },
            method="put",
        )
        self.assertEqual(response.status_code, 200, response.content)
        before = (self.stock(p), self.stock(second))
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = self.page(browser)
            page.goto(self.live_server_url + "/achats/besoins")
            self.inspect(page, "besoins-1440")
            page.get_by_role("link", name=p.name, exact=False).first.click()
            expect(page.get_by_role("heading", name=p.name, exact=True)).to_be_visible()
            page.context.route(
                "https://example.invalid/**",
                lambda route: route.fulfill(
                    status=200, body="<title>Fournisseur fictif de test</title>"
                ),
            )
            with page.expect_popup() as event:
                page.get_by_role(
                    "link", name="Ouvrir le site de Fournisseur fictif", exact=True
                ).click()
            popup = event.value
            popup.wait_for_load_state("load")
            self.assertTrue(popup.url.startswith("https://example.invalid/"))
            popup.close()
            self.assertEqual(self.db(lambda: PurchaseOrder.objects.count()), 0)
            self.assertEqual(
                self.db(
                    lambda: BusinessEvent.objects.filter(event_type="purchase.recorded").count()
                ),
                0,
            )
            page.get_by_role("link", name="Mettre en attente", exact=True).click()
            page.get_by_role("button", name="Confirmer la mise en attente", exact=True).click()
            self.follow(page)
            expect(page.locator("main")).to_contain_text("Reportée")
            page.get_by_role("link", name="Réactiver", exact=True).click()
            page.get_by_role("button", name="Confirmer la réactivation", exact=True).click()
            self.follow(page)
            expect(page.locator("main")).to_contain_text("À commander")
            page.get_by_role(
                "link", name="Enregistrer un achat chez ce fournisseur", exact=True
            ).click()
            page.get_by_label("Quantité réellement achetée", exact=True).fill("6")
            self.attest_and_review(page)
            held = []
            page.route("**/api/v1/purchase-products?*", lambda route: held.append(route))
            search = page.get_by_label("Ajouter un produit par nom ou référence", exact=True)
            search.fill(second.code)
            page.get_by_role("option", name=f"{second.name} · {second.code}", exact=True).click()
            expect(page.locator("#purchase-confirm")).to_be_disabled()
            expect(page.locator("#purchase-review")).to_be_hidden()
            self.assertTrue(held)
            pending = held.pop()
            pending.fulfill(response=pending.fetch())
            page.unroute("**/api/v1/purchase-products?*")
            expect(page.get_by_label("Quantité réellement achetée", exact=True)).to_have_count(2)
            page.get_by_label("Quantité réellement achetée", exact=True).nth(1).fill("3")
            page.get_by_label(
                "Numéro de commande chez le fournisseur — facultatif", exact=True
            ).fill("ACHAT-DEMO-FICTIF")
            page.get_by_label("Montant total — facultatif", exact=True).fill("45,50")
            self.attest_and_review(page)
            self.inspect(page, "declaration-1440")
            for width, height in [(1280, 800), (390, 844)]:
                page.set_viewport_size({"width": width, "height": height})
                self.inspect(page, f"declaration-{width}")
            page.get_by_role("button", name="Enregistrer l’achat", exact=True).click()
            self.follow(page)
            expect(page.locator("main")).to_contain_text("ACHAT-DEMO-FICTIF")
            expect(page.locator("main")).to_contain_text("45.50 EUR")
            self.assertEqual(self.db(lambda: PurchaseOrder.objects.count()), 1)
            self.assertEqual(self.db(lambda: (self.stock(p), self.stock(second))), before)
            page.goto(self.live_server_url + f"/achats/besoins/{p.pk}")
            expect(page.locator("main")).to_contain_text("2 carton")
            self.assertEqual(self.db(lambda: RecommendationEvent.objects.count()), 2)
            self.assertEqual(self.errors, [])
            self.assertEqual(self.http_errors, [])
            browser.close()

    def test_duplicate_warning_rechecks_new_candidates_before_distinct_confirmation(self):
        p = self.create_product()
        first = self.buy([p], [3])
        body = self.purchase_body([p], [3])
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = self.page(browser)
            page.goto(
                self.live_server_url
                + f"/achats/enregistrer?supplier={self.supplier.pk}&product={p.pk}"
            )
            page.get_by_label("Quantité réellement achetée", exact=True).fill("3")
            self.attest_and_review(page)
            page.get_by_role("button", name="Enregistrer l’achat", exact=True).click()
            expect(page.locator("#purchase-duplicates")).to_be_visible()
            expect(page.locator("#purchase-candidates")).to_contain_text(first.reference)
            expect(page.locator("#purchase-distinct")).to_be_disabled()
            self.assertEqual(self.db(lambda: PurchaseOrder.objects.count()), 1)
            ack = self.duplicate_ack(self.db(lambda: self.post_json("/api/v1/orders", body)))
            self.assertEqual(
                self.db(
                    lambda: self.post_json("/api/v1/orders", {**body, "distinct_purchase_ack": ack})
                ).status_code,
                201,
            )
            page.locator("#purchase-distinct-check").check()
            page.locator("#purchase-distinct").click()
            expect(page.locator("#purchase-candidates a")).to_have_count(2)
            expect(page.locator("#purchase-distinct-check")).not_to_be_checked()
            self.assertEqual(self.db(lambda: PurchaseOrder.objects.count()), 2)
            self.inspect(page, "doublon-nouveaux-candidats")
            page.locator("#purchase-distinct-check").check()
            page.locator("#purchase-distinct").click()
            self.follow(page)
            expect(page.locator("main")).to_contain_text("achat réellement distinct")
            self.assertEqual(self.db(lambda: PurchaseOrder.objects.count()), 3)
            self.assertEqual(self.errors, [])
            self.assertEqual(self.http_errors, [])
            browser.close()

    def test_cancel_partial_then_all_remainder_no_stock_movement(self):
        p = self.demandable_product(physical=1)
        other = self.create_product(code="AUTRE-FICTIF")
        order = self.buy([p, other], [5, 4])
        before = self.stock(p)
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = self.page(browser, 1280, 800)
            static_responses = []
            navigation_requests = []
            page.on(
                "response",
                lambda response: (
                    static_responses.append((urlsplit(response.url).path, response.status))
                    if "/static/" in response.url
                    else None
                ),
            )
            page.on(
                "request",
                lambda request: (
                    navigation_requests.append(
                        {"method": request.method, "path": urlsplit(request.url).path}
                    )
                    if request.is_navigation_request()
                    else None
                ),
            )
            page.goto(self.live_server_url + f"/achats/{order.pk}")
            page.get_by_role("link", name="Annuler les quantités restantes", exact=True).click()
            line = self.db(lambda: order.lines.get(product=p))
            page.locator(f'[data-order-line="{line.pk}"]').fill("2")
            page.locator("#purchase-verification").check()
            page.get_by_role("button", name="Vérifier le récapitulatif", exact=True).click()
            self.assertTrue(
                page.locator("#purchase-review").is_visible(),
                page.evaluate("""() => ({
                    url: location.pathname,
                    valid: document.querySelector('#purchase-form').checkValidity(),
                    invalid: [...document.querySelector('#purchase-form').elements]
                      .filter(node => !node.checkValidity())
                      .map(node => ({id: node.id, name: node.name})),
                    lines: [...document.querySelectorAll('[data-order-line]')]
                      .map(node => ({id: node.dataset.orderLine, value: node.value, max: node.max}))
                })"""),
            )
            self.inspect(page, "annulation-reliquat-preview")
            page.get_by_role(
                "button", name="Confirmer l’annulation du reliquat", exact=True
            ).click()
            self.follow(page)
            expect(page.locator("main")).to_contain_text("2 carton annulés")
            page.get_by_role("link", name="Annuler les quantités restantes", exact=True).click()
            page.wait_for_load_state("networkidle")
            page.locator("[data-order-line]").first.wait_for(state="visible")
            for field in page.locator("[data-order-line]").all():
                value = field.get_attribute("max")
                field.fill(value)
                self.assertEqual(field.input_value(), value)
            values_before_check = page.locator("[data-order-line]").evaluate_all(
                "nodes => nodes.map(node => ({value: node.value, max: node.max}))"
            )
            page.locator("#purchase-verification").check()
            self.assertEqual(
                page.locator("[data-order-line]").evaluate_all(
                    "nodes => nodes.map(node => ({value: node.value, max: node.max}))"
                ),
                values_before_check,
            )
            before_submit = page.locator("[data-order-line]").evaluate_all(
                "nodes => nodes.map(node => ({value: node.value, max: node.max}))"
            )
            page.get_by_role("button", name="Vérifier le récapitulatif", exact=True).click()
            self.assertTrue(
                page.locator("#purchase-review").is_visible(),
                page.evaluate(
                    """extra => ({
                    url: location.pathname,
                    search_present: Boolean(location.search),
                    static_responses: extra.static_responses,
                    page_error_kinds: extra.page_error_kinds,
                    before_submit: extra.before_submit,
                    navigation_requests: extra.navigation_requests,
                    result: document.querySelector('#operation-result')?.textContent?.slice(0, 160) ?? null,
                    valid: document.querySelector('#purchase-form').checkValidity(),
                    reviewHidden: document.querySelector('#purchase-review').hidden,
                    confirmExists: Boolean(document.querySelector('#purchase-confirm')),
                    lines: [...document.querySelectorAll('[data-order-line]')]
                      .map(node => ({id: node.dataset.orderLine, value: node.value, max: node.max}))
                })""",
                    {
                        "static_responses": static_responses,
                        "page_error_kinds": [error.split(":", 1)[0][:40] for error in self.errors],
                        "before_submit": before_submit,
                        "navigation_requests": navigation_requests,
                    },
                ),
            )
            page.locator("#purchase-confirm").click()
            self.follow(page)
            expect(page.locator("main")).to_contain_text("Annulée sans réception")
            expect(
                page.get_by_role("link", name="Annuler les quantités restantes", exact=True)
            ).to_have_count(0)
            self.assertEqual(self.db(lambda: self.stock(p)), before)
            self.inspect(page, "achat-annule-1280")
            self.assertEqual(self.errors, [])
            self.assertEqual(self.http_errors, [])
            browser.close()

    def test_offline_and_lost_response_double_click_recover_once_after_reload(self):
        p = self.create_product()
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = self.page(browser, 390, 844)
            page.goto(
                self.live_server_url
                + f"/achats/enregistrer?supplier={self.supplier.pk}&product={p.pk}"
            )
            page.get_by_label("Quantité réellement achetée", exact=True).fill("3")
            page.locator("#purchase-attestation").check()
            page.context.set_offline(True)
            expect(page.locator("#purchase-submit")).to_be_disabled()
            expect(page.get_by_label("Quantité réellement achetée", exact=True)).to_have_value("3")
            page.context.set_offline(False)
            page.locator("#purchase-submit").click()

            def lose(route):
                response = route.fetch()
                self.assertEqual(response.status, 201)
                route.abort("failed")

            page.route("**/api/v1/orders", lose)
            page.locator("#purchase-confirm").evaluate("n=>{n.click();n.click();}")
            expect(page.locator("#operation-result")).to_contain_text("Vérifiez")
            self.assertEqual(self.db(lambda: PurchaseOrder.objects.count()), 1)
            page.unroute("**/api/v1/orders", lose)
            page.reload()
            expect(
                page.get_by_role("link", name="Consulter les données à jour", exact=True)
            ).to_be_visible()
            self.follow(page)
            expect(page.locator("main")).to_contain_text("3")
            self.assertEqual(self.db(lambda: PurchaseOrder.objects.count()), 1)
            self.inspect(page, "reprise-reseau-mobile")
            self.assertEqual(self.errors, [])
            self.assertEqual(self.http_errors, [])
            browser.close()

    def test_empty_states_mobile_navigation_and_failed_product_search_keep_form(self):
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = self.page(browser, 390, 844)
            page.goto(self.live_server_url + "/achats/besoins")
            expect(
                page.get_by_role(
                    "heading", name="Aucun réapprovisionnement à prévoir pour le moment", exact=True
                )
            ).to_be_visible()
            page.get_by_role("button", name="Ouvrir la navigation", exact=True).click()
            page.get_by_role("link", name="Achats fournisseurs", exact=True).click()
            expect(page.locator("main")).to_contain_text("Aucun achat correspondant.")
            self.inspect(page, "etats-vides-390")
            page.get_by_role("link", name="Enregistrer un achat réel", exact=True).click()
            page.locator("#purchase-supplier").select_option(str(self.supplier.pk))
            page.locator("#purchase-reference").fill("FICTIF-NON-ENREGISTRE")
            page.locator("#purchase-attestation").check()
            page.locator("#purchase-submit").click()
            expect(page.locator("#operation-result")).to_contain_text(
                "Sélectionnez au moins un produit"
            )
            page.route("**/api/v1/purchase-products?*", lambda route: route.abort("failed"))
            page.locator("#purchase-product-search").fill("FICTIF-INCONNU")
            page.locator("#purchase-add").click()
            expect(page.locator("#purchase-picker-status")).to_contain_text(
                "Recherche indisponible"
            )
            expect(page.locator("#purchase-reference")).to_have_value("FICTIF-NON-ENREGISTRE")
            self.assertEqual(self.db(lambda: PurchaseOrder.objects.count()), 0)
            self.assertEqual(self.errors, [])
            self.assertEqual(self.http_errors, [])
            browser.close()
