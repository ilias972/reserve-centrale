"""Parcours réels Chromium PC/téléphone ; aucune réussite simulée."""

import json
from pathlib import Path

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from playwright.sync_api import expect, sync_playwright

from apps.accounts.models import User
from tests.test_accounts import FICTIVE_PASSWORD, SECOND_PASSWORD, AccountFixtures


class BrowserAuthTests(AccountFixtures, StaticLiveServerTestCase):
    def test_desktop_admin_creates_access_mobile_changes_temporary_and_session_survives_browser_restart(
        self,
    ):
        proof_dir = Path("reports/m1-browser")
        proof_dir.mkdir(parents=True, exist_ok=True)
        console_errors, failures = [], []
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            desktop = browser.new_context(viewport={"width": 1365, "height": 950})
            page = desktop.new_page()
            page.on("pageerror", lambda error: console_errors.append(str(error)))
            page.on(
                "response",
                lambda response: (
                    failures.append(response.status) if response.status >= 500 else None
                ),
            )
            page.goto(self.live_server_url + "/connexion")
            page.wait_for_load_state("networkidle")
            expect(page.get_by_role("heading", name="Se connecter")).to_be_visible()
            page.get_by_role("button", name="Afficher : Mot de passe", exact=True).click()
            expect(page.get_by_label("Mot de passe", exact=True)).to_have_attribute("type", "text")
            page.get_by_role("button", name="Masquer : Mot de passe", exact=True).click()
            page.get_by_label("Identifiant", exact=True).fill(self.admin.username)
            page.get_by_label("Mot de passe", exact=True).fill(FICTIVE_PASSWORD)
            page.get_by_label("Rester connecté sur cet appareil individuel").check()
            page.get_by_role("button", name="Se connecter", exact=True).click()
            page.wait_for_url("**/mon-compte")
            page.get_by_role("link", name="Utilisateurs", exact=True).click()
            page.get_by_role("link", name="Créer un compte", exact=True).click()
            # Reconnaissance sur le DOM réellement rendu avant remplissage.
            expect(page.get_by_label("Nom affiché", exact=True)).to_be_visible()
            page.get_by_label("Nom affiché", exact=True).fill("Demandeur A navigateur fictive")
            page.get_by_label("Rôle", exact=True).select_option("ASSISTANT")
            page.get_by_label("Unité d’affectation", exact=True).select_option(str(self.floor.pk))
            page.get_by_label("Identifiant de connexion", exact=True).fill(
                "demandeur-a-navigateur-fictive"
            )
            desktop.set_offline(True)
            page.get_by_role("button", name="Créer le compte", exact=True).click()
            expect(page.locator("main .submit-status")).to_contain_text("Connexion indisponible")
            desktop.set_offline(False)
            page.get_by_role("button", name="Créer le compte", exact=True).click()
            expect(page.get_by_role("heading", name="Accès enregistré")).to_be_visible()
            temporary = page.get_by_label("Mot de passe temporaire", exact=True).input_value()
            page.get_by_role("link", name="Retour aux utilisateurs").click()
            expect(page.get_by_text("Demandeur A navigateur fictive", exact=True)).to_be_visible()
            page.screenshot(path=str(proof_dir / "administration.png"), full_page=True)
            mobile = browser.new_context(
                viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True
            )
            phone = mobile.new_page()
            phone.on("pageerror", lambda error: console_errors.append(str(error)))
            phone.on(
                "response",
                lambda response: (
                    failures.append(response.status) if response.status >= 500 else None
                ),
            )
            phone.goto(self.live_server_url + "/connexion")
            phone.wait_for_load_state("networkidle")
            phone.get_by_label("Identifiant", exact=True).fill("demandeur-a-navigateur-fictive")
            phone.get_by_label("Mot de passe", exact=True).fill(temporary)
            phone.get_by_label("Rester connecté sur cet appareil individuel").check()
            phone.get_by_role("button", name="Se connecter", exact=True).click()
            phone.wait_for_url("**/mot-de-passe")
            phone.goto(self.live_server_url + "/mon-compte")
            phone.wait_for_url("**/mot-de-passe")
            phone.get_by_label("Mot de passe actuel ou temporaire", exact=True).fill(temporary)
            phone.get_by_label("Nouveau mot de passe", exact=True).fill(SECOND_PASSWORD)
            phone.get_by_label("Confirmer le nouveau mot de passe", exact=True).fill(
                SECOND_PASSWORD
            )
            phone.get_by_role("button", name="Enregistrer mon nouveau mot de passe").click()
            phone.wait_for_url("**/mon-compte")
            expect(phone.get_by_role("heading", name="Mon compte", exact=True)).to_be_visible()
            self.assertEqual(phone.get_by_role("link", name="Utilisateurs", exact=True).count(), 0)
            self.assertTrue(phone.evaluate("document.documentElement.scrollWidth <= innerWidth"))
            phone.screenshot(path=str(proof_dir / "compte-mobile.png"), full_page=True)
            state = mobile.storage_state()  # seulement en mémoire, aucun cookie archivé
            browser.close()
            reopened = playwright.chromium.launch(headless=True)
            context = reopened.new_context(
                storage_state=state, viewport={"width": 390, "height": 844}
            )
            phone = context.new_page()
            phone.goto(self.live_server_url + "/mon-compte")
            phone.wait_for_load_state("networkidle")
            expect(phone.get_by_role("heading", name="Mon compte", exact=True)).to_be_visible()
            phone.get_by_role("button", name="Se déconnecter", exact=True).click()
            phone.wait_for_url("**/connexion")
            phone.goto(self.live_server_url + "/mon-compte")
            phone.wait_for_url("**/connexion")
            reopened.close()
        self.assertFalse(console_errors, console_errors)
        self.assertFalse(failures, failures)
        self.assertTrue(
            User.objects.get(username="demandeur-a-navigateur-fictive").check_password(
                SECOND_PASSWORD
            )
        )
        (proof_dir / "observations.json").write_text(
            json.dumps(
                {
                    "desktop": "1365x950",
                    "mobile": "390x844",
                    "browser_restart_persistence": True,
                    "page_errors": console_errors,
                    "http_5xx": failures,
                    "secrets_archived": False,
                },
                indent=2,
            )
            + "\n"
        )

    def test_ui_validation_reset_disable_and_reactivation(self):
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context()
            page = context.new_page()
            page.goto(self.live_server_url + "/connexion")
            page.wait_for_load_state("networkidle")
            page.get_by_label("Identifiant", exact=True).fill(self.admin.username)
            page.get_by_label("Mot de passe", exact=True).fill(FICTIVE_PASSWORD)
            page.get_by_role("button", name="Se connecter", exact=True).click()
            page.wait_for_url("**/mon-compte")
            page.goto(f"{self.live_server_url}/administration/utilisateurs/{self.assistant.pk}")
            page.get_by_role("button", name="Fermer cette session", exact=True).click()
            page.wait_for_url(f"**/administration/utilisateurs/{self.assistant.pk}")
            expect(page.get_by_text("Aucune session active.")).to_be_visible()
            page.get_by_label("Compte actif", exact=True).uncheck()
            page.get_by_role("button", name="Enregistrer").click()
            page.wait_for_url("**/administration/utilisateurs")
            page.goto(f"{self.live_server_url}/administration/utilisateurs/{self.assistant.pk}")
            page.get_by_label("Compte actif", exact=True).check()
            page.get_by_role("button", name="Enregistrer").click()
            page.wait_for_url("**/administration/utilisateurs")
            page.goto(
                f"{self.live_server_url}/administration/utilisateurs/{self.assistant.pk}/reinitialiser"
            )
            page.get_by_label("J'ai vérifié l'identité de cette personne").check()
            page.get_by_role("button", name="Confirmer la réinitialisation").click()
            expect(page.get_by_role("heading", name="Accès enregistré")).to_be_visible()
            expect(page.get_by_label("Mot de passe temporaire", exact=True)).to_have_value(
                __import__("re").compile(r".{30,}")
            )
            browser.close()
        self.assertEqual(self.user_client.get("/api/v1/me").status_code, 401)
