"""Réseau A14 : interactions Chromium réelles, données PostgreSQL de test seulement."""

import json
from pathlib import Path

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from playwright.sync_api import expect, sync_playwright

from apps.requests.models import Delivery, InternalRequest
from tests.e2e import test_deliveries as delivery_browser
from tests.test_deliveries import DeliveryFixtures

ROOT = Path("docs/recette/A14-FINAL/network")


class A14NetworkTests(DeliveryFixtures, StaticLiveServerTestCase):
    db = delivery_browser.BrowserDeliveryTests.db
    login = delivery_browser.BrowserDeliveryTests.login
    phone = delivery_browser.BrowserDeliveryTests.phone
    review = delivery_browser.BrowserDeliveryTests.review

    def arrival(self):
        p = self.demandable_product(physical=100, maximum=100)
        r = self.send([p], [12])
        self.assertEqual(r.status_code, 201)
        request = InternalRequest.objects.get(pk=r.json()["resource"]["id"])
        self.assertEqual(self.claim(request).status_code, 200)
        request.refresh_from_db()
        return request, p

    def evidence(self, page, name, values):
        ROOT.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(ROOT / (name + ".png")), full_page=True)
        (ROOT / (name + ".json")).write_text(json.dumps(values, indent=2))

    def test_cut_before_delivery_no_fact_until_explicit_same_intent_resolution(self):
        request, product = self.arrival()
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = self.phone(browser)
            self.login(page, self.replenisher)
            page.goto(self.live_server_url + f"/demandes/{request.pk}")
            self.review(page)
            page.route(
                f"**/api/v1/requests/{request.pk}/deliveries",
                lambda route: route.abort("failed"),
                times=1,
            )
            page.locator("#delivery-confirm").click()
            expect(page.locator("#operation-result")).to_contain_text(
                "Connexion indisponible pendant"
            )
            expect(page.locator("#operation-recovery")).to_be_visible()
            self.assertEqual(self.db(lambda: Delivery.objects.count()), 0)
            self.assertEqual(self.db(lambda: self.stock(product)[:3]), (100, 12, 88))
            saved = page.evaluate(
                'Object.values(sessionStorage).filter(x=>x.includes("requests.deliver"))'
            )
            self.evidence(
                page,
                "before-processing",
                {
                    "delivery_count": 0,
                    "P": 100,
                    "R": 12,
                    "stored_intentions": len(saved),
                    "no_automatic_replay": True,
                },
            )
            page.reload()
            expect(page.locator("#operation-recovery")).to_be_visible()
            self.assertEqual(self.db(lambda: Delivery.objects.count()), 0)
            browser.close()

    def test_committed_twelve_lost_response_reload_and_double_click_once(self):
        request, product = self.arrival()
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = self.phone(browser)
            self.login(page, self.replenisher)
            page.goto(self.live_server_url + f"/demandes/{request.pk}")
            self.review(page)

            def lose(route):
                result = route.fetch()
                self.assertEqual(result.status, 201)
                route.abort("failed")

            page.route(f"**/api/v1/requests/{request.pk}/deliveries", lose, times=1)
            page.locator("#delivery-confirm").evaluate("(button)=>{button.click();button.click()}")
            expect(page.locator("#operation-result")).to_contain_text(
                "Connexion indisponible pendant"
            )
            self.assertEqual(self.db(lambda: Delivery.objects.count()), 1)
            self.assertEqual(self.db(lambda: self.stock(product)[:3]), (88, 0, 88))
            page.context.set_offline(True)
            expect(page.locator("#operation-network")).to_be_visible()
            self.evidence(
                page,
                "committed-response-lost",
                {"delivered": 12, "delivery_count": 1, "P": 88, "R": 0, "physical_iphone": False},
            )
            page.context.set_offline(False)
            page.reload()
            expect(page.locator("#request-status")).to_have_text("Livrée")
            expect(page.locator("#operation-recovery")).to_be_hidden()
            self.assertEqual(self.db(lambda: Delivery.objects.count()), 1)
            self.evidence(
                page,
                "reconnected-confirmed-once",
                {"delivery_count": 1, "confirmed_by_server_lookup": True},
            )
            browser.close()

    def test_network_profiles_navigation_and_recovery_without_offline_queue(self):
        self.demandable_product(physical=100, maximum=100)
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = self.phone(browser)
            self.login(page, self.assistant)
            cdp = page.context.new_cdp_session(page)
            profiles = [
                ("normal", 0, 0),
                ("slow", 150, 100000),
                ("high-latency", 800, 200000),
                ("jitter-low", 200, 100000),
                ("jitter-high", 600, 100000),
            ]
            values = []
            for name, latency, rate in profiles:
                cdp.send(
                    "Network.emulateNetworkConditions",
                    {
                        "offline": False,
                        "latency": latency,
                        "downloadThroughput": rate or -1,
                        "uploadThroughput": rate or -1,
                    },
                )
                page.goto(
                    self.live_server_url + "/demandes/nouvelle", wait_until="domcontentloaded"
                )
                expect(
                    page.get_by_role("button", name="Vérifier ma demande", exact=True)
                ).to_be_visible()
                values.append(
                    {
                        "profile": name,
                        "latency_ms": latency,
                        "bytes_per_second": rate or None,
                        "timing": page.evaluate(
                            '(()=>{const n=performance.getEntriesByType("navigation")[0];return {dom_ms:n.domContentLoadedEventEnd,ttfb_ms:n.responseStart}})()'
                        ),
                    }
                )
            page.context.set_offline(True)
            expect(page.locator("#operation-network")).to_be_visible()
            self.assertEqual(self.db(lambda: InternalRequest.objects.count()), 0)
            page.context.set_offline(False)
            cdp.send(
                "Network.emulateNetworkConditions",
                {"offline": False, "latency": 0, "downloadThroughput": -1, "uploadThroughput": -1},
            )
            self.evidence(
                page,
                "profiles",
                {
                    "profiles": values,
                    "jitter": "alternation déterministe 200/600 ms, pas une mesure Wi-Fi",
                    "loss": "pertes HTTP contrôlées dans les deux autres scénarios ; pas une capture de paquets IP",
                    "no_offline_transaction_queue": True,
                },
            )
            browser.close()

    def test_disconnect_during_blocked_confirmation_then_lookup_once(self):
        import time

        import psycopg
        from django.db import connection

        request, product = self.arrival()
        config = connection.settings_dict
        params = {
            key: config[value]
            for key, value in [
                ("host", "HOST"),
                ("port", "PORT"),
                ("dbname", "NAME"),
                ("user", "USER"),
                ("password", "PASSWORD"),
            ]
        }
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = self.phone(browser)
            self.login(page, self.replenisher)
            page.goto(self.live_server_url + f"/demandes/{request.pk}")
            self.review(page)
            with psycopg.connect(**params) as blocker:
                blocker.execute(
                    "SELECT product_id FROM inventory_stockbalance WHERE product_id=%s FOR UPDATE",
                    [product.pk],
                )
                page.locator("#delivery-confirm").click()

                def waiting():
                    with connection.cursor() as cursor:
                        cursor.execute(
                            "SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND wait_event_type='Lock' AND pid<>pg_backend_pid()"
                        )
                        return cursor.fetchone()[0] > 0

                until = time.monotonic() + 8
                while time.monotonic() < until and not self.db(waiting):
                    page.wait_for_timeout(50)
                self.assertTrue(self.db(waiting))
                self.assertEqual(self.db(lambda: Delivery.objects.count()), 0)
                page.context.set_offline(True)
                blocker.commit()
            # Chromium peut conserver le HTTP déjà engagé : la page reste incertaine,
            # sans succès, jusqu'à une consultation serveur après reconnexion.
            expect(page.locator("#operation-recovery")).to_be_visible()
            expect(page.locator("#operation-network")).to_be_visible()
            until = time.monotonic() + 8
            while time.monotonic() < until and not self.db(lambda: Delivery.objects.exists()):
                page.wait_for_timeout(50)
            self.assertEqual(self.db(lambda: Delivery.objects.count()), 1)
            self.assertEqual(self.db(lambda: self.stock(product)[:3]), (88, 0, 88))
            page.context.set_offline(False)
            page.reload()
            expect(page.locator("#request-status")).to_have_text("Livrée")
            self.evidence(
                page,
                "during-server-processing",
                {
                    "actual_lock_wait_observed": True,
                    "delivery_count": 1,
                    "quantity": 12,
                    "P": 88,
                    "R": 0,
                    "resolved_by_existing_intention": True,
                },
            )
            browser.close()
