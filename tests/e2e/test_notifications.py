"""Chromium réel, données PostgreSQL isolées. Aucun appareil physique ni service Push réel."""

import json
from pathlib import Path
from unittest.mock import patch

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import override_settings
from playwright.sync_api import expect, sync_playwright

from apps.notifications.models import Notification
from tests.e2e import test_corrections as browser_helpers
from tests.test_notifications import NotificationFixtures, subscription_body

ROOT = Path("docs/recette/M9/browser")


class NotificationBrowserTests(NotificationFixtures, StaticLiveServerTestCase):
    db = browser_helpers.BrowserCorrectionTests.db

    def login(self, page, user):
        try:
            browser_helpers.BrowserCorrectionTests.login(self, page, user)
        except Exception:
            # Page de diagnostic : ne jamais conserver les valeurs des mots de passe.
            page.locator('input[type="password"]').evaluate_all(
                "nodes=>nodes.forEach(n=>n.value='')"
            )
            page.screenshot(path=str(ROOT / "login-failure.png"), full_page=True)
            (ROOT / "login-failure.json").write_text(
                json.dumps(
                    {
                        "url": page.url,
                        "body": page.locator("body").inner_text(),
                        "errors": self.errors,
                        "failures": self.failures,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n"
            )
            raise

    page = browser_helpers.BrowserCorrectionTests.page

    def setUp(self):
        super().setUp()
        self.errors = []
        self.failures = []
        ROOT.mkdir(parents=True, exist_ok=True)

    def inspect(self, page, name):
        page.wait_for_load_state("networkidle")
        self.assertLessEqual(
            page.evaluate("document.documentElement.scrollWidth"), page.viewport_size["width"]
        )
        expect(page.get_by_role("link", name="Notifications", exact=True)).to_be_visible()
        page.evaluate(Path("tests/assets/axe.min.js").read_text())
        violations = page.evaluate(
            "async()=>{const r=await axe.run();return r.violations.filter(v=>['serious','critical'].includes(v.impact)).map(v=>({id:v.id,targets:v.nodes.map(n=>n.target)}));}"
        )
        self.assertEqual(violations, [], name)
        expect(page.locator('a[href="#"],button:not([type])')).to_have_count(0)
        controls = page.locator("main a,main button,main input,main select").evaluate_all(
            "nodes=>nodes.map(n=>({tag:n.tagName,text:n.textContent.trim(),visible:n.getClientRects().length>0,href:n.getAttribute('href')}))"
        )
        page.screenshot(path=str(ROOT / (name + ".png")), full_page=True)
        (ROOT / (name + ".json")).write_text(
            json.dumps(
                {"axe_serious_critical": violations, "controls": controls},
                ensure_ascii=False,
                indent=2,
            )
            + "\n"
        )

    def test_request_notification_read_and_live_badge_pc_and_phone(self):
        req, _ = self.requested()
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            operator = self.page(browser, self.replenisher, 390, 844)
            operator.goto(self.live_server_url + "/notifications")
            expect(
                operator.get_by_text("Aucune notification pour le moment", exact=True)
            ).to_be_visible()
            self.inspect(operator, "avant-traitement-390")
            self.db(self.process)
            expect(operator.locator("#notification-count")).to_be_visible(timeout=10000)
            expect(
                operator.get_by_role("heading", name="Nouvelle demande à préparer")
            ).to_be_visible(timeout=10000)
            self.inspect(operator, "nouvelle-demande-390")
            operator.get_by_role("link", name="Consulter la demande", exact=True).click()
            expect(operator).to_have_url(self.live_server_url + f"/demandes/{req.pk}")
            operator.get_by_role("button", name="Prendre en charge", exact=True).click()
            expect(operator.locator("#claim-request")).to_have_count(0)
            self.db(self.process)
            requester = self.page(browser, self.assistant)
            requester.get_by_role("link", name="Notifications", exact=True).click()
            expect(requester.get_by_role("heading", name="Demande prise en charge")).to_be_visible()
            self.inspect(requester, "demandeur — profil A-1440")
            requester.get_by_role("button", name="Tout marquer comme lu", exact=True).click()
            expect(requester.locator("#notification-count")).to_be_hidden()
            self.assertEqual(
                self.db(
                    lambda: Notification.objects.filter(
                        recipient=self.assistant, read_at__isnull=True
                    ).count()
                ),
                0,
            )
            self.assertEqual(self.errors, [])
            self.assertEqual(self.failures, [])
            browser.close()

    def test_responsible_case_direct_link_responsive_keyboard_and_permissions(self):
        _, _, case = self.conflict()
        self.process()
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            admin = self.page(browser, self.admin)
            admin.goto(self.live_server_url + "/notifications")
            for width, height in [(1440, 900), (1280, 800), (768, 1024), (390, 844), (320, 700)]:
                admin.set_viewport_size({"width": width, "height": height})
                self.inspect(admin, f"responsable-{width}")
            admin.get_by_role("link", name="Examiner le signalement", exact=True).focus()
            admin.keyboard.press("Enter")
            expect(admin).to_have_url(self.live_server_url + f"/dossiers/{case.pk}")
            expect(admin.locator(".dossier-summary")).to_be_visible()
            admin.goto(self.live_server_url + "/notifications/diagnostic")
            self.inspect(admin, "diagnostic-320")
            requester = self.page(browser, self.assistant)
            requester.goto(
                self.live_server_url
                + f"/notifications/{self.db(lambda: Notification.objects.get(recipient=self.admin, target_id=case.pk).pk)}/ouvrir"
            )
            expect(requester.get_by_text("Requête refusée.", exact=True)).to_be_visible()
            self.assertEqual(self.errors, [])
            self.assertEqual(self.failures, [])
            browser.close()

    def test_empty_offline_uncertain_read_and_no_fictitious_success(self):
        self.requested()
        self.process()
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = self.page(browser, self.replenisher, 390, 844)
            page.goto(self.live_server_url + "/notifications")

            # Une lecture POST traitée, puis réponse interrompue : le rechargement
            # reflète le seul état serveur, sans refaire de mouvement.
            def lost(route):
                route.fetch()
                route.abort("failed")

            page.route("**/notifications/lire-tout", lost)
            page.get_by_role("button", name="Tout marquer comme lu", exact=True).click()
            page.unroute("**/notifications/lire-tout", lost)
            page.goto(self.live_server_url + "/notifications?vue=non-lues")
            expect(page.get_by_text("Aucune notification non lue", exact=True)).to_be_visible()
            self.inspect(page, "vide-non-lues-390")
            page.context.set_offline(True)
            expect(page.locator("#live-status")).to_contain_text("Hors connexion", timeout=10000)
            page.context.set_offline(False)
            self.assertEqual(
                self.db(
                    lambda: Notification.objects.filter(
                        recipient=self.replenisher, read_at__isnull=True
                    ).count()
                ),
                0,
            )
            browser.close()

    @override_settings(
        NOTIFICATION_PUSH_ENABLED=True,
        WEBPUSH_PUBLIC_KEY="BFakePublicKeyForBrowserOnly",
        WEBPUSH_PRIVATE_KEY_FILE="never-read.pem",
        WEBPUSH_CONTACT="mailto:demo@example.invalid",
    )
    def test_push_permission_refused_unsupported_and_granted_adapter_only(self):
        body = subscription_body()
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = self.page(browser, self.replenisher, 390, 844)
            page.add_init_script(
                "Object.defineProperty(window,'PushManager',{value:undefined});delete window.PushManager;"
            )
            page.goto(self.live_server_url + "/notifications")
            expect(page.locator("#enable-push")).to_be_hidden()
            expect(page.locator("#push-status")).to_contain_text("ne prend pas en charge")
            page.close()
            page = self.page(browser, self.replenisher, 390, 844)
            page.add_init_script(
                "window.permissionCalls=0;Object.defineProperty(Notification,'requestPermission',{value:async()=>{window.permissionCalls++;return 'denied';}});"
            )
            page.goto(self.live_server_url + "/notifications")
            self.assertEqual(page.evaluate("permissionCalls"), 0)
            page.get_by_role("button", name="Activer les notifications", exact=True).click()
            expect(page.locator("#push-status")).to_contain_text("non autorisées")
            self.assertEqual(page.evaluate("permissionCalls"), 1)
            # Adaptateur navigateur volontairement simulé : aucune inscription au
            # service Apple/Google et aucun message réel.
            page.evaluate(
                """body=>{Notification.requestPermission=async()=> 'granted';let oldUnsubscribed=false;let sub={endpoint:body.endpoint,options:{applicationServerKey:new Uint8Array([1,2,3]).buffer},unsubscribe:async()=>{oldUnsubscribed=true;return true;}};const reg={pushManager:{getSubscription:async()=>sub,subscribe:async options=>{window.newServerKey=Array.from(new Uint8Array(options.applicationServerKey));sub={endpoint:body.endpoint,options:{applicationServerKey:options.applicationServerKey},toJSON:()=>({keys:body.keys})};return sub;}}};window.wasOldSubscriptionRemoved=()=>oldUnsubscribed;navigator.serviceWorker.register=async()=>reg;Object.defineProperty(navigator.serviceWorker,'ready',{value:Promise.resolve(reg)});} """,
                body,
            )
            with patch("apps.notifications.senders.push_transport") as external:
                page.get_by_role("button", name="Activer les notifications", exact=True).click()
                expect(page.locator("#push-status")).to_contain_text(
                    "Notifications activées sur cet appareil"
                )
                self.assertTrue(page.evaluate("wasOldSubscriptionRemoved()"))
                self.assertGreater(len(page.evaluate("newServerKey")), 0)
                expect(page.locator("#push-status strong")).to_have_text(
                    "Notifications activées sur cet appareil"
                )
                expect(
                    page.get_by_role("button", name="Désactiver cet abonnement", exact=True)
                ).to_be_visible()
                self.inspect(page, "abonnement-simule-390")
                # Un abonnement existant doit rester révocable même si la capacité
                # Push disparaît : la révocation serveur ne dépend pas de cette API.
                page.add_init_script("delete window.PushManager;")
                page.reload()
                expect(page.locator("#enable-push")).to_be_hidden()
                expect(page.locator("#push-status")).to_contain_text("L'abonnement est enregistré")
                page.get_by_role("button", name="Désactiver cet abonnement", exact=True).click()
                expect(
                    page.get_by_role("button", name="Désactiver cet abonnement", exact=True)
                ).to_have_count(0)
                external.assert_not_called()
            self.assertEqual(self.errors, [])
            self.assertEqual(self.failures, [])
            browser.close()

    @override_settings(
        NOTIFICATION_PUSH_ENABLED=True,
        WEBPUSH_PUBLIC_KEY="BFakePublicKeyForBrowserOnly",
        WEBPUSH_PRIVATE_KEY_FILE="never-read.pem",
        WEBPUSH_CONTACT="mailto:demo@example.invalid",
    )
    def test_active_subscription_with_old_application_key_requests_renewal(self):
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = self.page(browser, self.replenisher, 390, 844)
            self.db(self.register_push)
            page.add_init_script(
                """Object.defineProperty(Notification,'permission',{value:'granted'});const old={options:{applicationServerKey:new Uint8Array([1,2,3]).buffer}};const registration={pushManager:{getSubscription:async()=>old}};navigator.serviceWorker.getRegistration=async()=>registration;"""
            )
            page.goto(self.live_server_url + "/notifications")
            expect(page.locator("#push-status")).to_contain_text("ancienne inscription")
            expect(
                page.get_by_role("button", name="Désactiver cet abonnement", exact=True)
            ).to_be_visible()
            expect(page.locator("#enable-push")).to_be_hidden()
            self.inspect(page, "abonnement-cle-obsolete-390")
            self.assertEqual(self.errors, [])
            self.assertEqual(self.failures, [])
            browser.close()
