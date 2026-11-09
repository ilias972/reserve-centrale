"""Parcours réels des libellés, du droit unique et des suggestions partagées."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.db import close_old_connections, connection
from playwright.sync_api import expect, sync_playwright

from apps.catalog.models import Category, FloorProduct
from apps.inventory.models import StockEntry
from apps.requests.models import InternalRequest
from tests.test_accounts import FICTIVE_PASSWORD
from tests.test_deliveries import DeliveryFixtures


class SearchUsabilityBrowserTests(DeliveryFixtures, StaticLiveServerTestCase):
    def db(self, action):
        def run():
            close_old_connections()
            try:
                return action()
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(run).result()

    def page(self, browser, user, width=1440):
        page = browser.new_page(
            viewport={"width": width, "height": 844 if width < 600 else 900}, has_touch=width < 600
        )
        page.goto(self.live_server_url + "/connexion")
        page.get_by_label("Identifiant", exact=True).fill(user.username)
        page.get_by_label("Mot de passe", exact=True).fill(FICTIVE_PASSWORD)
        page.get_by_role("button", name="Se connecter", exact=True).click()
        page.wait_for_url("**/mon-compte")
        return page

    def capture(self, page, name):
        path = Path("reports/search-usability")
        path.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(path / (name + ".png")), full_page=True)

    def test_one_checkbox_saves_both_permissions_without_changing_old_request(self):
        request, products = self.requested(quantities=(2,))
        product = products[0]
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = self.page(browser, self.admin)
            page.goto(self.live_server_url + f"/catalogue/{product.pk}/droits")
            row = page.locator("tbody tr").filter(has_text=self.floor.name)
            self.assertEqual(row.locator("input[type=checkbox]").count(), 1)
            expect(page.get_by_role("columnheader", name="Visible", exact=True)).to_have_count(0)
            row.get_by_label("Demande autorisée", exact=True).uncheck()
            page.get_by_role(
                "button", name="Enregistrer les droits et plafonds", exact=True
            ).click()
            self.assertEqual(
                self.db(
                    lambda: list(
                        FloorProduct.objects.filter(product=product, floor=self.floor).values_list(
                            "visible", "demandable"
                        )
                    )
                ),
                [(False, False)],
            )
            self.assertEqual(
                self.db(
                    lambda: list(
                        InternalRequest.objects.get(pk=request.pk).lines.values_list(
                            "quantity", "reservation__remaining"
                        )
                    )
                ),
                [(2, 2)],
            )
            page.goto(self.live_server_url + f"/catalogue/{product.pk}/droits")
            row = page.locator("tbody tr").filter(has_text=self.floor.name)
            row.get_by_label("Demande autorisée", exact=True).check()
            row.get_by_label("Maximum par demande", exact=True).fill("3")
            page.get_by_role(
                "button", name="Enregistrer les droits et plafonds", exact=True
            ).click()
            self.assertEqual(
                self.db(
                    lambda: list(
                        FloorProduct.objects.filter(product=product, floor=self.floor).values_list(
                            "visible", "demandable", "maximum"
                        )
                    )
                ),
                [(True, True, 3)],
            )
            page.goto(self.live_server_url + f"/catalogue/{product.pk}/droits")
            self.capture(page, "droit-unique")
            browser.close()

    def test_human_labels_request_product_names_and_no_operation_uuid(self):
        request, products = self.requested(quantities=(2, 3))
        operation_ids = list(StockEntry.objects.values_list("operation_id", flat=True))
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            for user in [self.admin, self.assistant]:
                page = self.page(browser, user)
                page.goto(self.live_server_url + "/demandes")
                cell = (
                    page.locator("tbody tr")
                    .filter(has_text=request.reference)
                    .locator("td")
                    .nth(4 if user.role == "ADMIN" else 3)
                )
                for product in products:
                    expect(cell).to_contain_text(product.name)
                expect(cell).not_to_contain_text("référence")
                self.capture(page, "demandes-" + user.role.lower())
                page.close()
            page = self.page(browser, self.admin)
            page.goto(self.live_server_url + "/stocks/mouvements")
            for title in [
                "Quantité en réserve",
                "Quantité réservée aux demandes",
                "Changement des quantités",
            ]:
                expect(page.get_by_role("columnheader", name=title, exact=True)).to_be_visible()
            text = page.locator("#movement-table").inner_text()
            self.assertNotIn("Différences P / R", text)
            self.assertTrue(all(str(identifier) not in text for identifier in operation_ids))
            self.capture(page, "mouvements-comprehensibles")
            page.goto(self.live_server_url + f"/catalogue/{products[0].pk}/modifier")
            expect(page.get_by_label("Stock bas à", exact=True)).to_be_visible()
            expect(page.get_by_label("Quantité souhaitée", exact=True)).to_be_visible()
            browser.close()

    def test_suggestions_work_on_every_existing_search_surface_and_role(self):
        request, products = self.requested(quantities=(2,))
        category = Category.objects.create(name="Hygiène générale fictive")
        self.assertEqual(
            self.post_json(
                "/api/v1/suppliers",
                {"code": "FOURN-TEST", "name": "Équipement fictif", "active": True},
            ).status_code,
            201,
        )
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            cases = [
                (
                    self.admin,
                    [
                        ("/catalogue", "produit", products[0].code),
                        ("/administration/utilisateurs", "demandeur", "demandeur-a-fictif"),
                        ("/configuration/floor", "unite", "FICTIF-A"),
                        ("/configuration/category", "hygiene", category.name),
                        ("/configuration/supplier", "equip", "FOURN-TEST"),
                        ("/demandes", "produit", request.reference),
                    ],
                ),
                (
                    self.assistant,
                    [
                        ("/catalogue", "produit", products[0].code),
                        ("/demandes/nouvelle", "produit", products[0].code),
                        ("/demandes", "produit", request.reference),
                    ],
                ),
                (
                    self.replenisher,
                    [
                        ("/stocks", "produit", products[0].code),
                        ("/demandes", "produit", request.reference),
                    ],
                ),
            ]
            for user, paths in cases:
                page = self.page(browser, user)
                for path, text, expected in paths:
                    page.goto(self.live_server_url + path)
                    if user.role == "REPLENISHER" and path == "/demandes":
                        page.locator(".queue-filters summary").click()
                    field = page.locator("input[data-search-scope]")
                    field.fill(text)
                    expect(
                        page.get_by_role("option").filter(has_text=expected).first
                    ).to_be_visible(timeout=6000)
                    page.keyboard.press("Escape")
                    expect(page.get_by_role("listbox")).to_be_hidden()
                page.close()
            browser.close()

    def test_single_category_filter_and_keyboard_touch_suggestions_preserve_basket(self):
        product = self.demandable_product(code="GANTS-BLEUS")
        self.assertEqual(self.modify(product, name="Gants stériles bleus").status_code, 200)
        category = Category.objects.create(name="Protection")
        product.refresh_from_db()
        self.assertEqual(self.modify(product, category_id=str(category.pk)).status_code, 200)
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = self.page(browser, self.assistant, 390)
            page.goto(self.live_server_url + "/demandes/nouvelle")
            self.assertEqual(page.locator("#request-category").count(), 1)
            self.assertEqual(page.locator("#request-category-pills").count(), 0)
            page.get_by_role("button", name="Augmenter — Gants stériles bleus", exact=True).click()
            field = page.get_by_label("Rechercher un produit", exact=True)
            self.assertGreaterEqual(field.bounding_box()["width"], 300)
            self.assertLess(
                page.locator("label[for=request-search]").bounding_box()["y"],
                field.bounding_box()["y"],
            )
            field.fill("steri ga")
            expect(
                page.get_by_role("option", name="Gants stériles bleus · GANTS-BLEUS", exact=True)
            ).to_be_visible()
            self.capture(page, "suggestions-iphone-emule")
            page.keyboard.press("ArrowDown")
            page.keyboard.press("Enter")
            expect(field).to_have_value("GANTS-BLEUS")
            expect(page.locator(".request-product")).to_have_count(1)
            expect(page.locator("#cart-count")).to_contain_text("1 référence")
            field.fill("gan")
            page.get_by_role("option", name="Gants stériles bleus · GANTS-BLEUS", exact=True).tap()
            page.locator("#request-category").select_option(str(category.pk))
            expect(page.locator(".request-product")).to_have_count(1)
            expect(page.locator("#cart-count")).to_contain_text("1 référence")
            self.assertEqual(self.db(lambda: InternalRequest.objects.count()), 0)
            self.assertTrue(page.evaluate("document.documentElement.scrollWidth <= innerWidth"))
            page.evaluate(Path("tests/assets/axe.min.js").read_text())
            issues = page.evaluate(
                "async()=> (await axe.run()).violations.filter(v=>['serious','critical'].includes(v.impact)).map(v=>v.id)"
            )
            self.assertEqual(issues, [])
            browser.close()

    def test_suggestion_network_error_keeps_regular_search_usable(self):
        product = self.demandable_product()
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = self.page(browser, self.assistant)
            page.goto(self.live_server_url + "/demandes/nouvelle")
            page.route("**/api/v1/search-suggestions?*", lambda route: route.abort())
            page.get_by_label("Rechercher un produit", exact=True).fill(product.code)
            expect(page.locator(".search-suggestion-status")).to_contain_text(
                "Suggestions indisponibles"
            )
            page.get_by_role("button", name="Rechercher", exact=True).click()
            expect(page.locator(".request-product")).to_have_count(1)
            self.assertEqual(self.db(lambda: InternalRequest.objects.count()), 0)
            browser.close()
