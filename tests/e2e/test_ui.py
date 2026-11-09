"""UI-M5 : vrais écrans PostgreSQL, contrôles rendus et erreurs de présentation."""

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.db import close_old_connections, connection
from django.urls import resolve
from playwright.sync_api import expect, sync_playwright

from apps.catalog.models import Category
from apps.inventory.models import StockEntry
from tests.test_accounts import FICTIVE_PASSWORD
from tests.test_deliveries import DeliveryFixtures


class BrowserUIContractTests(DeliveryFixtures, StaticLiveServerTestCase):
    def db(self, action):
        def run():
            close_old_connections()
            try:
                return action()
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(run).result()

    def login(self, page, user):
        page.goto(self.live_server_url + "/connexion")
        page.get_by_label("Identifiant", exact=True).fill(user.username)
        page.get_by_label("Mot de passe", exact=True).fill(FICTIVE_PASSWORD)
        page.get_by_role("button", name="Se connecter", exact=True).click()
        page.wait_for_url("**/mon-compte")

    def folder(self):
        folder = Path("reports/ui-m5-browser")
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def test_real_surfaces_navigation_forms_assets_and_private_links(self):
        request, products = self.prepare(quantities=(2, 3))
        product = products[0]
        roles = [
            (
                self.admin,
                [
                    "/mon-compte",
                    "/mot-de-passe",
                    "/administration/utilisateurs",
                    "/administration/utilisateurs/creer",
                    f"/administration/utilisateurs/{self.assistant.pk}",
                    f"/administration/utilisateurs/{self.assistant.pk}/reinitialiser",
                    "/catalogue",
                    "/catalogue?configuration=droits",
                    "/catalogue?configuration=stock",
                    "/catalogue/creer",
                    f"/catalogue/{product.pk}",
                    f"/catalogue/{product.pk}/modifier",
                    f"/catalogue/{product.pk}/droits",
                    f"/catalogue/{product.pk}/fournisseurs",
                    f"/catalogue/{product.pk}/historique",
                    "/configuration/floor",
                    "/configuration/floor/creer",
                    f"/configuration/floor/{self.floor.pk}",
                    "/configuration/category",
                    "/configuration/category/creer",
                    "/configuration/supplier",
                    "/configuration/supplier/creer",
                    "/stocks",
                    "/stocks/inventaire-initial",
                    "/stocks/mouvements",
                    "/stocks/signalements",
                    "/livraisons/signalements",
                    "/demandes",
                    f"/demandes/{request.pk}",
                ],
            ),
            (
                self.assistant,
                [
                    "/mon-compte",
                    "/demandes/nouvelle",
                    "/demandes",
                    "/demandes/historique",
                    f"/demandes/{request.pk}",
                    "/catalogue",
                ],
            ),
            (
                self.replenisher,
                [
                    "/mon-compte",
                    "/stocks",
                    "/stocks/inventaire-initial",
                    "/stocks/mouvements",
                    "/stocks/signalements",
                    "/demandes",
                    f"/demandes/{request.pk}",
                    f"/demandes/{request.pk}/refuser",
                    "/catalogue",
                ],
            ),
        ]
        observations, errors, failures = [], [], []
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            for user, paths in roles:
                context = browser.new_context(
                    viewport={"width": 390 if user == self.replenisher else 1440, "height": 1000}
                )
                context.add_init_script("""(() => {
                    const listeners = new WeakMap();
                    const original = EventTarget.prototype.addEventListener;
                    EventTarget.prototype.addEventListener = function(type, callback, options) {
                        const types = listeners.get(this) || new Set(); types.add(type);
                        listeners.set(this, types); return original.call(this, type, callback, options);
                    };
                    window.uiHasListener = (node, type) => listeners.get(node)?.has(type) || false;
                })();""")
                page = context.new_page()
                page.on("pageerror", lambda e: errors.append(str(e)))
                page.on("response", lambda r: failures.append(r.url) if r.status >= 500 else None)
                self.login(page, user)
                checked_links = {}
                for index, path in enumerate(paths):
                    response = page.goto(self.live_server_url + path)
                    self.assertEqual(response.status, 200, path)
                    self.assertIn("font-src 'self'", response.headers["content-security-policy"])
                    page.wait_for_load_state("networkidle")
                    page.evaluate(
                        "async()=>{await document.fonts.load('400 14px \"Manrope\"');await document.fonts.ready;}"
                    )
                    self.assertTrue(page.evaluate("document.fonts.check('400 14px \"Manrope\"')"))
                    self.assertEqual(
                        page.locator(
                            'a[href="#"],a[href^="javascript:"],button[onclick=""]'
                        ).count(),
                        0,
                    )
                    self.assertTrue(
                        page.evaluate("document.documentElement.scrollWidth <= innerWidth"), path
                    )
                    controls = page.locator(
                        "a:visible,button:visible,input:visible,select:visible,textarea:visible"
                    ).evaluate_all(
                        "nodes=>nodes.map(n=>({tag:n.tagName,text:n.textContent?.trim(),label:n.getAttribute('aria-label'),href:n.getAttribute('href'),id:n.id,type:n.type,disabled:n.disabled,action:n.form?.action,method:n.form?.method}))"
                    )
                    orphans = page.locator(
                        "button:visible"
                    ).evaluate_all("""nodes => nodes.filter(n => {
                        if(n.type === 'submit')return !n.form;
                        if(n.dataset.stepInput)return !document.getElementById(n.dataset.stepInput);
                        for(let target=n;target && target !== document.body;target=target.parentElement){
                            if(window.uiHasListener(target,'click'))return false;
                        }
                        return true;
                    }).map(n => ({id:n.id,text:n.textContent,label:n.getAttribute('aria-label')}))""")
                    self.assertEqual(orphans, [], path)
                    for anchor in page.locator("a[href]:visible").all():
                        href = anchor.get_attribute("href")
                        if href.startswith("#") or urlsplit(href).scheme:
                            continue
                        resolve(urlsplit(href).path)
                        if href not in checked_links:
                            target_response = page.request.get(self.live_server_url + href)
                            self.assertEqual(target_response.status, 200, f"{user.role}: {href}")
                            checked_links[href] = target_response.status
                    for form in page.locator("form").all():
                        action = form.get_attribute("action")
                        if action:
                            resolve(urlsplit(action).path)
                        if form.get_attribute("method") == "post":
                            self.assertEqual(
                                form.locator('[name="csrfmiddlewaretoken"]').count(), 1
                            )
                    page.evaluate("scrollTo(0,0)")
                    page.screenshot(
                        path=str(self.folder() / f"{user.role.lower()}-{index:02}.png"),
                        full_page=True,
                    )
                    observations.append({"role": user.role, "path": path, "controls": controls})
                context.close()
            browser.close()
        self.assertEqual(errors, [])
        self.assertEqual(failures, [])
        (self.folder() / "rendered-controls.json").write_text(
            json.dumps(
                {"screens": observations, "page_errors": errors, "http_5xx": failures},
                ensure_ascii=False,
                indent=2,
            )
        )

    def test_mobile_admin_menu_hides_closed_links_and_restores_keyboard_focus(self):
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page(viewport={"width": 390, "height": 844})
            self.login(page, self.admin)
            expect(page.locator("#admin-navigation")).to_be_hidden()
            page.get_by_role("button", name="Ouvrir la navigation", exact=True).click()
            expect(page.get_by_role("link", name="Catalogue", exact=True)).to_be_visible()
            page.keyboard.press("Escape")
            expect(page.locator("#admin-navigation")).to_be_hidden()
            expect(
                page.get_by_role("button", name="Ouvrir la navigation", exact=True)
            ).to_be_focused()
            page.get_by_role("button", name="Ouvrir la navigation", exact=True).click()
            page.get_by_role("link", name="Catalogue", exact=True).click()
            expect(page.get_by_role("heading", name="Catalogue", exact=True)).to_be_visible()
            browser.close()

    def test_modified_filter_keeps_invalid_row_visible_and_cancel_discards_all(self):
        products = [self.demandable_product(code=f"UI-{i}") for i in range(2)]
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page(viewport={"width": 390, "height": 844})
            self.login(page, self.replenisher)
            page.goto(self.live_server_url + "/stocks")
            for product in products:
                page.get_by_role("button", name=f"Augmenter — {product.name}", exact=True).click()
            page.locator("#stock-show-modified").click()
            field = page.get_by_label(f"Quantité physique — {products[0].name}", exact=True)
            field.fill("")
            expect(field).to_be_visible()
            expect(page.locator(".stock-invalid")).to_contain_text("Saisissez une quantité entière")
            expect(page.locator("#stock-confirm")).to_be_disabled()
            page.locator("#stock-cancel").click()
            expect(page.locator(".stock-dirty")).to_have_count(0)
            expect(page.locator("#stock-confirm")).to_be_hidden()
            page.locator("#stock-show-all").click()
            expect(field).to_have_value("10")
            self.assertEqual(
                self.db(lambda: StockEntry.objects.filter(kind="MANUAL_ADJUSTMENT").count()), 0
            )
            browser.close()

    def test_stock_confirmed_result_survives_refresh_failure(self):
        product = self.demandable_product()
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page()
            self.login(page, self.replenisher)
            page.goto(self.live_server_url + "/stocks")
            page.route("**/api/v1/stocks?*", lambda route: route.abort("failed"))
            held = []
            page.route("**/api/v1/stocks/adjustments", lambda route: held.append(route), times=1)
            page.get_by_label(f"Quantité physique — {product.name}", exact=True).fill("8")
            with page.expect_request("**/api/v1/stocks/adjustments"):
                page.locator("#stock-confirm").click()
            expect(page.locator("#stock-busy")).to_be_visible()
            page.screenshot(path=str(self.folder() / "chargement-mobile.png"), full_page=True)
            held[0].continue_()
            expect(page.locator("#stock-busy")).to_be_hidden()
            expect(page.locator("#stock-result")).to_contain_text("Lot enregistré une seule fois")
            expect(page.locator("#stock-result")).to_contain_text("actualisation indisponible")
            self.assertEqual(self.db(lambda: self.stock(product)[0]), 8)
            self.assertEqual(
                self.db(lambda: StockEntry.objects.filter(kind="MANUAL_ADJUSTMENT").count()), 1
            )
            browser.close()

    def test_real_search_empty_category_sync_and_mobile_single_column(self):
        product = self.demandable_product()
        category = Category.objects.create(name="Hygiène fictive")
        self.assertEqual(self.modify(product, category_id=str(category.pk)).status_code, 200)
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page(viewport={"width": 390, "height": 844})
            self.login(page, self.assistant)
            page.goto(self.live_server_url + "/demandes/nouvelle")
            page.get_by_role("button", name=f"Augmenter — {product.name}", exact=True).click()
            expect(page.locator("#cart-count")).to_contain_text("1 référence")
            page.locator("#request-category").select_option(str(category.pk))
            page.get_by_role("button", name="Rechercher", exact=True).click()
            expect(page.locator("#request-category")).to_have_value(str(category.pk))
            expect(page.locator("#request-category-pills")).to_have_count(0)
            page.locator("#request-search").fill("aucune-correspondance-fictive")
            page.get_by_role("button", name="Rechercher", exact=True).click()
            expect(page.locator("#request-empty")).to_be_visible()
            expect(page.locator("#cart-count")).to_contain_text("1 référence")
            page.locator("#request-search").fill("")
            page.locator("#request-category").select_option("")
            expect(page.locator(".request-product")).to_have_count(1)
            expect(page.locator("#request-category")).to_have_value("")
            self.assertEqual(
                page.locator("#request-products").evaluate(
                    "n=>getComputedStyle(n).gridTemplateColumns.split(' ').length"
                ),
                1,
            )
            page.get_by_role("button", name=f"Retirer — {product.name}", exact=True).click()
            expect(page.locator("#cart-count")).to_have_text("Aucun produit sélectionné")
            browser.close()

    def test_activity_filters_use_real_ledger_rows(self):
        product = self.demandable_product()
        self.assertEqual(self.adjust([product], [8]).status_code, 200)
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page()
            self.login(page, self.admin)
            page.goto(self.live_server_url + "/stocks/mouvements")
            page.get_by_role("button", name="Ajustements manuels", exact=True).click()
            expect(page.locator("[data-movement-kind]:visible")).to_have_count(1)
            page.get_by_role("button", name="Baisses de stock", exact=True).click()
            expect(page.locator("[data-movement-kind]:visible")).to_have_count(1)
            page.get_by_role("button", name="Tous", exact=True).click()
            expect(page.locator("[data-movement-kind]:visible")).to_have_count(2)
            browser.close()
