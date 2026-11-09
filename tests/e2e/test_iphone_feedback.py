"""Retours physiques du porteur : corrections vérifiées sur navigateur, DB isolée."""

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.db import close_old_connections, connection
from playwright.sync_api import expect, sync_playwright

from apps.accounts.models import User
from apps.inventory.models import StockBalance
from apps.requests.models import InternalRequest
from tests.test_accounts import FICTIVE_PASSWORD
from tests.test_deliveries import DeliveryFixtures


class IphoneFeedbackBrowserTests(DeliveryFixtures, StaticLiveServerTestCase):
    def db(self, action):
        def run():
            close_old_connections()
            try:
                return action()
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(run).result()

    def page(self, browser, user, path):
        context = browser.new_context(viewport={"width": 390, "height": 844}, has_touch=True)
        page = context.new_page()
        page.goto(self.live_server_url + "/connexion")
        page.get_by_label("Identifiant", exact=True).fill(user.username)
        page.get_by_label("Mot de passe", exact=True).fill(FICTIVE_PASSWORD)
        page.get_by_role("button", name="Se connecter", exact=True).click()
        page.wait_for_url("**/mon-compte")
        page.goto(self.live_server_url + path)
        page.wait_for_load_state("networkidle")
        return page

    def capture(self, page, name):
        folder = Path("reports/iphone-feedback")
        folder.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(folder / (name + ".png")), full_page=True)

    def send_ui(self, page):
        page.get_by_role("button", name="Vérifier ma demande", exact=True).click()
        page.get_by_role("button", name="Envoyer la demande", exact=True).click()
        expect(page.locator("#operation-result")).to_contain_text("Quantités réservées")

    def test_navigation_visible_filter_one_parent_and_back_to_top(self):
        product = self.demandable_product()
        for i in range(16):
            self.create_product(code=f"SCROLL-{i}")
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = self.page(browser, self.replenisher, "/stocks")
            self.assertEqual(
                [text.strip() for text in page.locator(".bottom-navigation a").all_text_contents()],
                ["Stocks", "Demandes", "Réceptions", "Mon activité", "Signalements"],
            )
            expect(page.get_by_text("Autres actions de réserve", exact=True)).to_have_count(0)
            expect(page.get_by_label("Filtre", exact=True)).to_be_visible()
            expect(page.get_by_role("link", name="Inventaire initial", exact=True)).to_be_visible()
            page.evaluate("scrollTo(0,document.body.scrollHeight)")
            expect(page.get_by_role("button", name="↑ Haut de page", exact=True)).to_be_visible()
            page.get_by_role("button", name="↑ Haut de page", exact=True).click()
            page.wait_for_function("() => scrollY < 10")
            self.capture(page, "stocks-filtre-navigation")
            page.goto(self.live_server_url + "/mon-compte")
            expect(page.get_by_text("Mes espaces autorisés", exact=True)).to_have_count(0)
            self.capture(page, "compte-simplifie")
            admin = self.page(browser, self.admin, f"/catalogue/{product.pk}/droits")
            self.assertEqual(admin.locator("main > a.back-link").count(), 1)
            admin.goto(self.live_server_url + "/mon-compte")
            expect(
                admin.get_by_role("link", name="Consulter les produits", exact=True)
            ).to_be_visible()
            admin.get_by_role("button", name="Ouvrir la navigation", exact=True).click()
            expect(admin.get_by_role("link", name="Catalogue", exact=True)).to_have_count(1)
            admin.keyboard.press("Escape")
            self.assertTrue(admin.evaluate("document.documentElement.scrollWidth <= innerWidth"))
            browser.close()

    def test_three_accounts_refresh_without_reload_and_preserve_basket(self):
        product = self.demandable_product(maximum=10)
        other = User.objects.create_user(
            "autre-assistante",
            FICTIVE_PASSWORD,
            role="ASSISTANT",
            display_name="Autre assistante fictive",
            floor=self.floor,
            must_change_password=False,
        )
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            first = self.page(browser, self.assistant, "/demandes/nouvelle")
            second = self.page(browser, other, "/demandes/nouvelle")
            queue = self.page(browser, self.replenisher, "/demandes")
            stock = self.page(browser, self.replenisher, "/stocks")
            for _ in range(3):
                second.get_by_role("button", name=f"Augmenter — {product.name}", exact=True).click()
            for _ in range(2):
                first.get_by_role("button", name=f"Augmenter — {product.name}", exact=True).click()
            self.send_ui(first)
            expect(second.locator(".availability")).to_contain_text("Disponible : 8", timeout=10000)
            expect(
                second.get_by_label(f"Quantité demandée — {product.name}", exact=True)
            ).to_have_value("3")
            expect(stock.locator("[data-live-quantities]")).to_contain_text(
                "Réservé 2 · Disponible 8", timeout=10000
            )
            expect(queue.locator(".request-card-link")).to_have_count(1, timeout=10000)
            first.get_by_role("link", name="Suivre cette demande", exact=True).click()
            queue.locator(".request-card-link").click()
            queue.get_by_role("button", name="Prendre en charge", exact=True).click()
            expect(first.locator("#request-status")).to_have_text("En préparation", timeout=10000)
            queue.get_by_role("button", name="Vérifier la remise", exact=True).click()
            queue.get_by_role("button", name="Confirmer la remise", exact=True).click()
            expect(first.locator("#request-status")).to_have_text("Livrée", timeout=10000)
            expect(
                stock.get_by_label(f"Quantité physique — {product.name}", exact=True)
            ).to_have_value("8", timeout=10000)
            self.capture(first, "suivi-livre-automatique")
            self.capture(second, "autre-assistante-panier-conserve")
            self.assertEqual(
                self.db(lambda: list(StockBalance.objects.values_list("physical", "reserved"))),
                [(8, 0)],
            )
            browser.close()

    def test_counting_draft_is_not_overwritten_and_old_version_refused(self):
        product = self.demandable_product(maximum=10)
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = self.page(browser, self.replenisher, "/stocks")
            page.get_by_role("button", name=f"Augmenter — {product.name}", exact=True).click()
            self.db(lambda: self.send([product], [2]))
            expect(page.locator("[data-live-quantities]")).to_contain_text(
                "Réservé 2 · Disponible 8", timeout=10000
            )
            expect(
                page.get_by_label(f"Quantité physique — {product.name}", exact=True)
            ).to_have_value("11")
            page.get_by_role("button", name="Confirmer la mise à jour", exact=True).click()
            expect(page.locator("#stock-conflicts")).to_be_visible()
            self.assertEqual(
                self.db(lambda: list(StockBalance.objects.values_list("physical", "reserved"))),
                [(10, 2)],
            )
            self.capture(page, "comptage-conserve-conflit")
            browser.close()

    def test_unsubmitted_filters_and_selected_hidden_rights_are_safe(self):
        product = self.demandable_product(maximum=10)
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = self.page(browser, self.assistant, "/demandes/nouvelle")
            page.get_by_role("button", name=f"Augmenter — {product.name}", exact=True).click()
            page.get_by_label("Rechercher un produit", exact=True).fill("rien-de-correspondant")
            page.wait_for_timeout(2600)
            expect(page.locator(".request-product")).to_have_count(1)
            expect(
                page.get_by_role("button", name="Vérifier ma demande", exact=True)
            ).to_be_enabled()
            page.get_by_role("button", name="Rechercher", exact=True).click()
            expect(page.locator(".request-product")).to_have_count(0)
            page.wait_for_timeout(2600)
            expect(
                page.get_by_role("button", name="Vérifier ma demande", exact=True)
            ).to_be_enabled()

            def revoke():
                product.refresh_from_db()
                return self.rights(
                    product,
                    [
                        {
                            "floor_id": str(self.floor.pk),
                            "visible": False,
                            "demandable": False,
                            "maximum": None,
                        }
                    ],
                )

            self.assertEqual(self.db(revoke).status_code, 200)
            expect(page.locator("#cart-lines")).to_contain_text(
                "Quantité à recontrôler", timeout=10000
            )
            expect(
                page.get_by_role("button", name="Vérifier ma demande", exact=True)
            ).to_be_disabled()
            self.capture(page, "selection-masquee-droits-revoques")
            browser.close()

    def test_invalid_input_focus_and_remove_focus_survive_live_changes(self):
        product = self.demandable_product(maximum=10)
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = self.page(browser, self.assistant, "/demandes/nouvelle")
            field = page.get_by_label(f"Quantité demandée — {product.name}", exact=True)
            field.fill("")
            self.db(lambda: self.send([product], [1]))
            expect(page.locator(".availability")).to_contain_text("Disponible : 9", timeout=10000)
            expect(field).to_have_value("")
            expect(field).to_be_focused()
            field.fill("2")
            remove = page.get_by_role("button", name=f"Retirer — {product.name}", exact=True)
            remove.focus()
            self.db(lambda: self.send([product], [1]))
            expect(page.locator(".availability")).to_contain_text("Disponible : 8", timeout=10000)
            expect(remove).to_be_focused()
            browser.close()

    def test_offline_hidden_pause_and_automatic_recovery(self):
        product = self.demandable_product(maximum=10)
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = self.page(browser, self.assistant, "/demandes/nouvelle")
            reads = []
            page.on(
                "request",
                lambda request: (
                    reads.append(request.url)
                    if request.url.startswith(self.live_server_url + "/api/v1/request-catalog")
                    else None
                ),
            )
            page.evaluate(
                "Object.defineProperty(document,'hidden',{configurable:true,value:true});document.dispatchEvent(new Event('visibilitychange'))"
            )
            before = len(reads)
            page.wait_for_timeout(2400)
            self.assertEqual(len(reads), before)
            page.context.set_offline(True)
            page.evaluate(
                "Object.defineProperty(document,'hidden',{configurable:true,value:false});document.dispatchEvent(new Event('visibilitychange'))"
            )
            expect(page.locator("#live-status")).to_contain_text("Hors connexion")
            self.db(lambda: self.send([product], [2]))
            page.context.set_offline(False)
            expect(page.locator(".availability")).to_contain_text("Disponible : 8", timeout=10000)
            expect(page.locator("#live-status")).to_be_hidden()
            browser.close()

    def test_uncertain_commit_is_not_repeated_by_background_reads(self):
        product = self.demandable_product(maximum=10)
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = self.page(browser, self.assistant, "/demandes/nouvelle")

            def lose_response(route):
                route.fetch()
                route.abort()

            page.route("**/api/v1/requests", lose_response)
            page.get_by_role("button", name=f"Augmenter — {product.name}", exact=True).click()
            page.get_by_role("button", name="Vérifier ma demande", exact=True).click()
            page.get_by_role("button", name="Envoyer la demande", exact=True).click()
            expect(page.locator("#operation-recovery")).to_be_visible()
            page.wait_for_timeout(2600)
            self.assertEqual(self.db(lambda: InternalRequest.objects.count()), 1)
            page.get_by_role("button", name="Vérifier l'enregistrement", exact=True).click()
            expect(page.locator("#operation-result")).to_contain_text("Quantités réservées")
            self.assertEqual(self.db(lambda: InternalRequest.objects.count()), 1)
            self.assertEqual(
                self.db(lambda: list(StockBalance.objects.values_list("physical", "reserved"))),
                [(10, 1)],
            )
            (Path("reports/iphone-feedback") / "lost-response.json").write_text(
                json.dumps({"requests": 1, "P": 10, "R": 1, "automatic_confirmation": False})
            )
            browser.close()

    def test_remote_cancellation_keeps_handover_draft_but_blocks_confirmation(self):
        request, products = self.prepare(quantities=(2,))
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = self.page(browser, self.replenisher, f"/demandes/{request.pk}")
            field = page.get_by_label(f"Quantité remise — {products[0].name}", exact=True)
            field.fill("1")
            self.assertEqual(self.db(lambda: self.cancel_request(request)).status_code, 200)
            expect(page.locator("#operation-result")).to_contain_text(
                "Cette demande a changé", timeout=10000
            )
            expect(field).to_have_value("1")
            expect(
                page.get_by_role("button", name="Vérifier la remise", exact=True)
            ).to_be_disabled()
            page.evaluate("dispatchEvent(new Event('online'))")
            expect(
                page.get_by_role("button", name="Vérifier la remise", exact=True)
            ).to_be_disabled()
            self.assertEqual(
                self.db(lambda: list(StockBalance.objects.values_list("physical", "reserved"))),
                [(10, 0)],
            )
            self.capture(page, "remise-provisoire-annulation-distante")
            browser.close()

    def test_new_basket_item_during_targeted_read_is_not_marked_unavailable(self):
        first = self.demandable_product(code="POLL-1", maximum=10)
        second = self.demandable_product(code="POLL-2", maximum=10)
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = self.page(browser, self.assistant, "/demandes/nouvelle")
            page.get_by_role("button", name=f"Augmenter — {first.name}", exact=True).click()
            held = []
            page.route("**/api/v1/request-catalog?product_ids=*", lambda route: held.append(route))
            page.wait_for_event("request", predicate=lambda request: "?product_ids=" in request.url)
            page.get_by_role("button", name=f"Augmenter — {second.name}", exact=True).click()
            self.assertEqual(len(held), 1)
            response = held[0].fetch()
            held[0].fulfill(response=response)
            page.unroute("**/api/v1/request-catalog?product_ids=*")
            page.wait_for_timeout(300)
            expect(
                page.get_by_role("button", name="Vérifier ma demande", exact=True)
            ).to_be_enabled()
            expect(page.locator("#cart-lines")).not_to_contain_text("Quantité à recontrôler")
            self.assertEqual(page.locator("#cart-lines li").count(), 2)
            browser.close()

    def test_pagination_uses_applied_filter_when_search_is_only_typed(self):
        for i in range(51):
            self.demandable_product(code=f"PAGINATION-{i:03}")
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            for user, path, search, more, rows in [
                (
                    self.assistant,
                    "/demandes/nouvelle",
                    "request-search",
                    "request-more",
                    ".request-product",
                ),
                (self.replenisher, "/stocks", "stock-search", "stock-more", ".stock-row"),
            ]:
                page = self.page(browser, user, path)
                expect(page.locator(rows)).to_have_count(50)
                page.locator("#" + search).fill("aucun-resultat-non-soumis")
                page.locator("#" + more).click()
                expect(page.locator(rows)).to_have_count(51)
                expect(page.locator("#" + more)).to_be_hidden()
                page.context.close()
            browser.close()
