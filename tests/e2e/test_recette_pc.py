"""Recette utilisateur PC UI-M5 : interactions nouvelles, PG isolé et preuves."""

import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.db import close_old_connections, connection
from django.utils import timezone
from playwright.sync_api import expect, sync_playwright

from apps.accounts.models import User, UserSession
from apps.cases.models import StockCase
from apps.catalog.models import Category, Product
from apps.inventory.models import StockEntry
from apps.requests.models import Delivery, DeliveryIssue, InternalRequest
from tests.e2e.recette_support import Walk, browser_command, login
from tests.test_accounts import FICTIVE_PASSWORD, SECOND_PASSWORD
from tests.test_catalog import image_file
from tests.test_deliveries import DeliveryFixtures


class RecettePCTests(DeliveryFixtures, StaticLiveServerTestCase):
    def setUp(self):
        expected = os.environ.get("TEST_DATABASE_NAME", "test_stock_app_default")
        assert expected.startswith("test_") and connection.settings_dict["NAME"] == expected, (
            "Base de recette isolée requise"
        )
        super().setUp()

    def db(self, action):
        def run():
            close_old_connections()
            try:
                return action()
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(run).result()

    def page(self, browser, walk, user, width=1440, height=900):
        context = browser.new_context(
            viewport={"width": width, "height": height},
            locale="fr-FR",
            is_mobile=width < 600,
            has_touch=width < 600,
        )
        page = walk.attach(context.new_page(), user.role)
        login(walk, page, self.live_server_url, user, FICTIVE_PASSWORD)
        return page

    def surfaces(self, request, product):
        routes = [
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
                    "/catalogue",
                ],
            ),
        ]
        return routes

    def test_01_accessibility_and_real_controls_discovery(self):
        request, products = self.prepare(quantities=(2, 3))
        product = products[0]
        walk = Walk(self._testMethodName)
        routes = self.surfaces(request, product)
        failures = []
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            for user, paths in routes:
                page = self.page(browser, walk, user)
                for index, path in enumerate(paths):
                    page.goto(self.live_server_url + path)
                    findings = walk.inspect(page, user.role, str(index))
                    failures += [
                        (user.role, path, v["id"], v["impact"])
                        for v in findings["violations"]
                        if v["impact"] in {"serious", "critical"}
                    ]
                    # Une vraie interaction et son retour observable sur les surfaces de lecture.
                    details = page.locator("details:visible").first
                    if details.count():
                        summary = details.locator("summary").first
                        walk.step(
                            page,
                            "Ouvrir puis refermer les informations complémentaires",
                            lambda summary=summary: (summary.click(), summary.click()),
                            lambda details=details: expect(details).not_to_have_attribute(
                                "open", ""
                            ),
                        )
                    elif page.locator(
                        "main input:not([type=hidden]):not([type=password]):not([type=checkbox]):visible"
                    ).count():
                        field = page.locator(
                            "main input:not([type=hidden]):not([type=password]):not([type=checkbox]):visible"
                        ).first
                        if field.is_editable() and field.get_attribute("type") not in {
                            "number",
                            "date",
                            "file",
                        }:
                            walk.step(
                                page,
                                "Saisir une valeur fictive puis revenir à la valeur initiale",
                                lambda field=field: (field.fill("recette fictive"), field.fill("")),
                                lambda field=field: expect(field).to_have_value(""),
                            )
                page.context.close()
            browser.close()
        walk.clean(self)
        self.assertEqual(failures, [])

    def send_ui(self, walk, page, products, quantities):
        walk.step(
            page,
            "Ouvrir la sélection de fournitures",
            lambda: page.get_by_role("link", name="Faire une demande", exact=True).click(),
            lambda: expect(page.locator("#request-products")).to_be_visible(),
        )
        for product, quantity in zip(products, quantities, strict=True):
            walk.step(
                page,
                f"Sélectionner {quantity} {product.unit} de {product.name}",
                lambda p=product, q=quantity: page.get_by_label(
                    f"Quantité demandée — {p.name}", exact=True
                ).fill(str(q)),
                lambda: expect(page.locator("#cart-review")).to_be_enabled(),
            )
        walk.step(
            page,
            "Vérifier le panier multi-produits",
            lambda: page.locator("#cart-review").click(),
            lambda: expect(page.locator("#request-review")).to_be_visible(),
        )
        walk.step(
            page,
            "Envoyer la demande une seule fois",
            lambda: page.locator("#request-send").click(),
            lambda: expect(
                page.get_by_role("link", name="Suivre cette demande", exact=True)
            ).to_be_visible(),
        )
        walk.step(
            page,
            "Consulter la demande enregistrée",
            lambda: page.get_by_role("link", name="Suivre cette demande", exact=True).click(),
            lambda: expect(page.locator("#request-status")).to_have_text("Nouvelle"),
        )
        return page.url.rsplit("/", 1)[-1]

    def claim_ui(self, walk, page, identifier):
        reference = self.db(lambda: InternalRequest.objects.get(pk=identifier).reference)
        walk.step(
            page,
            "Ouvrir la liste opérationnelle des demandes",
            lambda: page.get_by_role("link", name="Demandes", exact=True).click(),
            lambda: expect(page.get_by_role("link", name=reference, exact=True)).to_be_visible(),
        )
        walk.step(
            page,
            "Ouvrir la demande de l'demandeur — profil A",
            lambda: page.get_by_role("link", name=reference, exact=True).click(),
            lambda: expect(page.locator("#claim-request")).to_be_visible(),
        )
        walk.step(
            page,
            "Prendre en charge",
            lambda: page.locator("#claim-request").click(),
            lambda: expect(page.locator("#request-status")).to_have_text("En préparation"),
        )

    def deliver_ui(self, walk, page, mode="COMPLETE", quantities=None):
        if quantities is not None:
            for index, quantity in enumerate(quantities):
                walk.step(
                    page,
                    "Renseigner seulement la quantité réellement remise",
                    lambda i=index, q=quantity: (
                        page.locator(".handed-quantity").nth(i).fill(str(q))
                    ),
                    lambda i=index, q=quantity: expect(
                        page.locator(".handed-quantity").nth(i)
                    ).to_have_value(str(q)),
                )
        walk.step(
            page,
            "Choisir explicitement le traitement du reliquat",
            lambda: page.locator(f'[name="delivery-mode"][value="{mode}"]').check(),
            lambda: expect(page.locator(f'[name="delivery-mode"][value="{mode}"]')).to_be_checked(),
        )
        walk.step(
            page,
            "Vérifier le récapitulatif de remise",
            lambda: page.locator("#delivery-review").click(),
            lambda: expect(page.locator("#delivery-recap")).to_be_visible(),
        )
        if mode == "SHORTAGE":
            walk.step(
                page,
                "Attester le manque physique et sa correction de stock",
                lambda: page.locator("#shortage-ack").check(),
                lambda: expect(page.locator("#delivery-confirm")).to_be_enabled(),
            )
        walk.step(
            page,
            "Confirmer après remise physique fictive",
            lambda: page.locator("#delivery-confirm").click(),
            lambda: expect(page.locator("#delivery-recap")).to_be_hidden(),
        )

    def test_02_scenario_A_complete_three_roles_and_history(self):
        products = [self.demandable_product(code=f"RECETTE-A-{i}") for i in range(3)]
        walk = Walk(self._testMethodName)
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            assistant = self.page(browser, walk, self.assistant)
            operator = self.page(browser, walk, self.replenisher, 1280, 800)
            admin = self.page(browser, walk, self.admin)
            identifier = self.send_ui(walk, assistant, products, [2, 3, 1])
            self.assertEqual(
                self.db(lambda: [self.stock(p)[:3] for p in products]),
                [(10, 2, 8), (10, 3, 7), (10, 1, 9)],
            )
            admin.goto(self.live_server_url + "/stocks")
            walk.inspect(admin, "ADMIN", "reservations")
            self.claim_ui(walk, operator, identifier)
            self.deliver_ui(walk, operator)
            expect(operator.locator("#request-status")).to_have_text("Livrée")
            walk.inspect(operator, "REPLENISHER", "complete")
            self.assertEqual(
                self.db(lambda: [self.stock(p)[:3] for p in products]),
                [(8, 0, 8), (7, 0, 7), (9, 0, 9)],
            )
            walk.step(
                assistant,
                "Revenir dans Mes demandes après remise",
                lambda: (
                    assistant.locator("main")
                    .get_by_role("link", name="Mes demandes", exact=True)
                    .click()
                ),
                lambda: expect(
                    assistant.locator('.badge[data-status="DELIVERED"]')
                ).to_be_visible(),
            )
            walk.step(
                assistant,
                "Ouvrir l'historique personnel",
                lambda: assistant.get_by_role("link", name="Mon historique", exact=True).click(),
                lambda: expect(
                    assistant.get_by_text("Remise confirmée", exact=False)
                ).to_be_visible(),
            )
            walk.inspect(assistant, "ASSISTANT", "historique-livree")
            self.assertEqual(
                self.db(lambda: StockEntry.objects.filter(kind="MANUAL_ADJUSTMENT").count()), 0
            )
            browser.close()
        walk.clean(self)

    def test_03_scenario_B_successive_partial_deliveries(self):
        products = [self.demandable_product(code=f"RECETTE-B-{i}") for i in range(2)]
        walk = Walk(self._testMethodName)
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            assistant = self.page(browser, walk, self.assistant)
            operator = self.page(browser, walk, self.replenisher)
            identifier = self.send_ui(walk, assistant, products, [4, 4])
            self.claim_ui(walk, operator, identifier)
            self.deliver_ui(walk, operator, "LATER", [1, 2])
            expect(operator.locator("#request-status")).to_have_text(
                "Partiellement livrée — reste à livrer"
            )
            self.assertEqual(
                self.db(lambda: [self.stock(p)[:3] for p in products]), [(9, 3, 6), (8, 2, 6)]
            )
            walk.step(
                assistant,
                "Vérifier le reliquat actualisé et l'absence d'annulation rétroactive",
                lambda: expect(assistant.locator("#request-status")).to_have_text(
                    "Partiellement livrée — reste à livrer", timeout=10000
                ),
                lambda: expect(assistant.locator("#cancel-open")).to_have_count(0),
            )
            walk.inspect(assistant, "ASSISTANT", "partielle")
            self.deliver_ui(walk, operator)
            expect(operator.locator("#request-status")).to_have_text("Livrée")
            self.assertEqual(
                self.db(lambda: [self.stock(p)[:3] for p in products]), [(6, 0, 6), (6, 0, 6)]
            )
            self.assertEqual(self.db(lambda: Delivery.objects.count()), 2)
            walk.step(
                assistant,
                "Retrouver automatiquement les deux remises dans la demande",
                lambda: expect(assistant.locator("#request-status")).to_have_text(
                    "Livrée", timeout=10000
                ),
                lambda: expect(assistant.locator(".delivery-fact")).to_have_count(2),
            )
            walk.inspect(assistant, "ASSISTANT", "deux-remises")
            browser.close()
        walk.clean(self)

    def test_04_scenario_C_cancel_release_and_forbidden_late_cancellation(self):
        product = self.demandable_product()
        walk = Walk(self._testMethodName)
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            assistant = self.page(browser, walk, self.assistant)
            identifier = self.send_ui(walk, assistant, [product], [3])
            walk.step(
                assistant,
                "Ouvrir puis abandonner la confirmation d'annulation",
                lambda: (
                    assistant.locator("#cancel-open").click(),
                    assistant.locator("#cancel-back").click(),
                ),
                lambda: expect(assistant.locator("#cancel-request")).to_be_hidden(),
            )
            self.assertEqual(self.db(lambda: self.stock(product)[:3]), (10, 3, 7))
            walk.step(
                assistant,
                "Annuler après confirmation explicite",
                lambda: (
                    assistant.locator("#cancel-open").click(),
                    assistant.locator("#cancel-confirm").click(),
                ),
                lambda: expect(assistant.locator("#request-status")).to_have_text("Annulée"),
            )
            self.assertEqual(self.db(lambda: self.stock(product)[:3]), (10, 0, 10))
            walk.inspect(assistant, "ASSISTANT", "annulee")
            row = self.db(lambda: InternalRequest.objects.get(pk=identifier))
            self.assertEqual(self.db(lambda: self.cancel_request(row).status_code), 409)
            browser.close()
        walk.clean(self)

    def test_05_shortage_error_reporting_and_admin_readonly(self):
        request, products = self.prepare(quantities=(4,))
        walk = Walk(self._testMethodName)
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            operator = self.page(browser, walk, self.replenisher)
            admin = self.page(browser, walk, self.admin)
            operator.goto(self.live_server_url + f"/demandes/{request.pk}")
            self.deliver_ui(walk, operator, "SHORTAGE", [0])
            expect(operator.locator("#request-status")).to_have_text("Clôturée — manque")
            self.assertEqual(self.db(lambda: self.stock(products[0])[:3]), (6, 0, 6))
            walk.inspect(operator, "REPLENISHER", "manque-sans-remise")
            walk.step(
                operator,
                "Signaler une erreur sans modifier la remise",
                lambda: operator.get_by_role(
                    "link", name="Signaler une erreur", exact=True
                ).click(),
                lambda: expect(
                    operator.get_by_role("heading", name="Signaler une erreur", exact=True)
                ).to_be_visible(),
            )
            walk.step(
                operator,
                "Enregistrer le signalement d'erreur",
                lambda: (
                    operator.get_by_label("Précision facultative", exact=True).fill(
                        "Erreur de comptage fictive, à vérifier."
                    ),
                    operator.get_by_role(
                        "button", name="Confirmer le signalement", exact=True
                    ).click(),
                ),
                lambda: expect(
                    operator.get_by_text(
                        "Une erreur a déjà été signalée pour cette remise.", exact=True
                    )
                ).to_be_visible(),
            )
            admin.goto(self.live_server_url + "/livraisons/signalements")
            walk.inspect(admin, "ADMIN", "signalements-remise")
            self.assertEqual(self.db(lambda: DeliveryIssue.objects.count()), 2)
            self.assertEqual(self.db(lambda: self.stock(products[0])[:3]), (6, 0, 6))
            browser.close()
        walk.clean(self)

    def test_06_user_lifecycle_temporary_reset_revocation_and_roles(self):
        walk = Walk(self._testMethodName)
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            admin = self.page(browser, walk, self.admin)
            walk.step(
                admin,
                "Ouvrir l'administration des utilisateurs",
                lambda: admin.get_by_role("link", name="Utilisateurs", exact=True).click(),
                lambda: expect(
                    admin.get_by_role("heading", name="Utilisateurs", exact=True)
                ).to_be_visible(),
            )
            walk.step(
                admin,
                "Créer un compte individuel",
                lambda: admin.get_by_role("link", name="Créer un compte", exact=True).click(),
                lambda: expect(
                    admin.get_by_label("Identifiant de connexion", exact=True)
                ).to_be_visible(),
            )
            walk.step(
                admin,
                "Remplir le compte d'une demandeur — profil A fictive",
                lambda: (
                    admin.get_by_label("Nom affiché", exact=True).fill("Élodie d'Essai — recette"),
                    admin.get_by_label("Rôle", exact=True).select_option("ASSISTANT"),
                    admin.get_by_label("Unité d’affectation", exact=True).select_option(
                        str(self.floor.pk)
                    ),
                    admin.get_by_label("Identifiant de connexion", exact=True).fill(
                        "elodie-recette"
                    ),
                    admin.get_by_role("button", name="Créer le compte", exact=True).click(),
                ),
                lambda: expect(
                    admin.get_by_role("heading", name="Accès enregistré", exact=True)
                ).to_be_visible(),
            )
            temporary = admin.get_by_label("Mot de passe temporaire", exact=True).input_value()
            user = self.db(lambda: User.objects.get(username="elodie-recette"))
            assistant = walk.attach(
                browser.new_page(viewport={"width": 1280, "height": 800}), "ASSISTANT"
            )
            assistant.goto(self.live_server_url + "/connexion")
            walk.step(
                assistant,
                "Le temporaire oblige le remplacement initial",
                lambda: (
                    assistant.get_by_label("Identifiant", exact=True).fill(user.username),
                    assistant.get_by_label("Mot de passe", exact=True).fill(temporary),
                    assistant.get_by_label(
                        "Rester connecté sur cet appareil individuel", exact=True
                    ).check(),
                    assistant.get_by_role("button", name="Se connecter", exact=True).click(),
                ),
                lambda: expect(
                    assistant.get_by_label("Nouveau mot de passe", exact=True)
                ).to_be_visible(),
            )
            walk.step(
                assistant,
                "Choisir un mot de passe personnel fictif",
                lambda: (
                    assistant.get_by_label("Mot de passe actuel ou temporaire", exact=True).fill(
                        temporary
                    ),
                    assistant.get_by_label("Nouveau mot de passe", exact=True).fill(
                        SECOND_PASSWORD
                    ),
                    assistant.get_by_label("Confirmer le nouveau mot de passe", exact=True).fill(
                        SECOND_PASSWORD
                    ),
                    assistant.get_by_role(
                        "button", name="Enregistrer mon nouveau mot de passe", exact=True
                    ).click(),
                ),
                lambda: expect(
                    assistant.get_by_role("heading", name="Mon compte", exact=True)
                ).to_be_visible(),
            )
            state = assistant.context.storage_state()
            resume_browser = pw.chromium.launch()
            restarted = resume_browser.new_context(
                storage_state=state, viewport={"width": 1280, "height": 800}
            )
            resumed = walk.attach(restarted.new_page(), "ASSISTANT")
            resumed.goto(self.live_server_url + "/mon-compte")
            expect(resumed.get_by_role("heading", name="Mon compte", exact=True)).to_be_visible()
            admin.goto(self.live_server_url + f"/administration/utilisateurs/{user.pk}")
            # M9 lit le compteur dans chaque onglet : les refus de l'ancienne
            # session sont attendus dès la révocation, pas seulement au GET suivant.
            walk.expected_incident = True
            walk.step(
                admin,
                "Modifier l'unité et le nom",
                lambda: (
                    admin.get_by_label("Nom affiché", exact=True).fill("Élodie d'Essai — modifiée"),
                    admin.get_by_label("Unité d’affectation", exact=True).select_option(
                        str(self.other_floor.pk)
                    ),
                    admin.get_by_role("button", name="Enregistrer", exact=True).click(),
                ),
                lambda: expect(
                    admin.get_by_role("heading", name="Utilisateurs", exact=True)
                ).to_be_visible(),
            )
            walk.expected_incident = True
            walk.step(
                resumed,
                "L'ancienne session ne conserve pas les permissions",
                lambda: resumed.goto(self.live_server_url + "/mon-compte"),
                lambda: expect(
                    resumed.get_by_role("heading", name="Se connecter", exact=True)
                ).to_be_visible(),
            )
            walk.step(
                assistant,
                "L'autre onglet perd aussi l'ancienne session",
                lambda: assistant.goto(self.live_server_url + "/mon-compte"),
                lambda: expect(
                    assistant.get_by_role("heading", name="Se connecter", exact=True)
                ).to_be_visible(),
            )
            walk.expected_incident = False
            login(walk, assistant, self.live_server_url, user, SECOND_PASSWORD)
            admin.goto(self.live_server_url + f"/administration/utilisateurs/{user.pk}")
            walk.expected_incident = True
            walk.step(
                admin,
                "Révoquer l'accès individuel depuis les sessions",
                lambda: admin.get_by_role(
                    "button", name="Fermer cette session", exact=True
                ).first.click(),
                lambda: expect(
                    admin.get_by_text("Aucune session active.", exact=True)
                ).to_be_visible(),
            )
            walk.step(
                assistant,
                "La session fermée ne peut plus consulter le compte",
                lambda: assistant.goto(self.live_server_url + "/mon-compte"),
                lambda: expect(
                    assistant.get_by_role("heading", name="Se connecter", exact=True)
                ).to_be_visible(),
            )
            walk.expected_incident = False
            admin.goto(self.live_server_url + f"/administration/utilisateurs/{user.pk}")
            walk.step(
                admin,
                "Désactiver ce seul compte de recette",
                lambda: (
                    admin.get_by_label("Compte actif", exact=True).uncheck(),
                    admin.get_by_role("button", name="Enregistrer", exact=True).click(),
                ),
                lambda: expect(
                    admin.get_by_role("heading", name="Utilisateurs", exact=True)
                ).to_be_visible(),
            )
            admin.goto(self.live_server_url + f"/administration/utilisateurs/{user.pk}")
            walk.step(
                admin,
                "Réactiver sans restaurer les anciennes sessions",
                lambda: (
                    admin.get_by_label("Compte actif", exact=True).check(),
                    admin.get_by_role("button", name="Enregistrer", exact=True).click(),
                ),
                lambda: expect(
                    admin.get_by_role("heading", name="Utilisateurs", exact=True)
                ).to_be_visible(),
            )
            admin.goto(
                self.live_server_url + f"/administration/utilisateurs/{user.pk}/reinitialiser"
            )
            walk.step(
                admin,
                "Réinitialiser après vérification d'identité",
                lambda: (
                    admin.get_by_label(
                        "J'ai vérifié l'identité de cette personne", exact=True
                    ).check(),
                    admin.get_by_role(
                        "button", name="Confirmer la réinitialisation", exact=True
                    ).click(),
                ),
                lambda: expect(
                    admin.get_by_label("Mot de passe temporaire", exact=True)
                ).to_be_visible(),
            )
            new_temporary = admin.get_by_label("Mot de passe temporaire", exact=True).input_value()
            self.assertNotEqual(temporary, new_temporary)
            admin.goto(self.live_server_url + f"/administration/utilisateurs/{self.admin.pk}")
            expect(admin.get_by_label("Compte actif", exact=True)).to_be_disabled()
            walk.inspect(admin, "ADMIN", "dernier-acces-protege")
            resume_browser.close()
            walk.step(
                admin,
                "Se déconnecter volontairement",
                lambda: admin.get_by_role("button", name="Se déconnecter", exact=True).click(),
                lambda: expect(
                    admin.get_by_role("heading", name="Se connecter", exact=True)
                ).to_be_visible(),
            )
            browser.close()
        walk.clean(self)

    def test_07_catalogue_references_permissions_photos_initial_zero_activation_archive(self):
        walk = Walk(self._testMethodName)
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            admin = self.page(browser, walk, self.admin)
            for kind, values in [
                ("category", {"Nom": "Hygiène d'unité — démo"}),
                ("floor", {"Nom": "Unité de recette C"}),
                (
                    "supplier",
                    {
                        "Nom": "Fournisseur fictif facultatif",
                        "Site Web (facultatif)": "https://example.com",
                    },
                ),
            ]:
                admin.goto(self.live_server_url + f"/configuration/{kind}/creer")
                for label, value in values.items():
                    admin.get_by_label(label, exact=True).fill(value)
                walk.step(
                    admin,
                    f"Créer la référence {kind}",
                    lambda kind=kind: admin.get_by_role(
                        "button",
                        name=f"Enregistrer {dict(category='catégorie', floor='unité', supplier='fournisseur')[kind]}",
                        exact=True,
                    ).click(),
                    lambda values=values: expect(
                        admin.get_by_text(values["Nom"], exact=True)
                    ).to_be_visible(),
                )
                row = (
                    admin.locator(".reference-cards article").filter(has_text=values["Nom"])
                    if kind == "floor"
                    else admin.locator("tbody tr").filter(has_text=values["Nom"])
                )
                walk.step(
                    admin,
                    f"Ouvrir la modification {kind}",
                    lambda row=row: row.get_by_role("link", name="Modifier", exact=True).click(),
                    lambda values=values: expect(
                        admin.get_by_label("Nom", exact=True)
                    ).to_have_value(values["Nom"]),
                )
                walk.step(
                    admin,
                    f"Désactiver la référence {kind} sans suppression",
                    lambda kind=kind: (
                        admin.get_by_label("Actif", exact=True).uncheck(),
                        admin.get_by_role(
                            "button",
                            name=f"Enregistrer {dict(category='catégorie', floor='unité', supplier='fournisseur')[kind]}",
                            exact=True,
                        ).click(),
                    ),
                    lambda: expect(admin.get_by_text("Désactivé", exact=True)).to_be_visible(),
                )
                row = (
                    admin.locator(".reference-cards article").filter(has_text=values["Nom"])
                    if kind == "floor"
                    else admin.locator("tbody tr").filter(has_text=values["Nom"])
                )
                row.get_by_role("link", name="Modifier", exact=True).click()
                admin.get_by_label("Actif", exact=True).check()
                admin.get_by_role(
                    "button",
                    name=f"Enregistrer {dict(category='catégorie', floor='unité', supplier='fournisseur')[kind]}",
                    exact=True,
                ).click()
            admin.get_by_role("link", name="Catalogue", exact=True).click()
            admin.get_by_role("link", name="Créer un produit", exact=True).click()
            walk.step(
                admin,
                "Créer une fourniture sans fournisseur",
                lambda: (
                    admin.get_by_label("Nom du produit", exact=True).fill(
                        "Paquets d'essuie-mains — démonstration"
                    ),
                    admin.get_by_label("Catégorie (facultative)", exact=True).select_option(
                        label="Hygiène d'unité — démo"
                    ),
                    admin.get_by_label("Unité de comptage unique", exact=True).fill("paquet"),
                    admin.get_by_label("Stock bas à", exact=True).fill("2"),
                    admin.get_by_label("Quantité souhaitée", exact=True).fill("15"),
                    admin.get_by_role("button", name="Créer le produit", exact=True).click(),
                ),
                lambda: expect(
                    admin.get_by_text("Non inventorié — quantité inconnue", exact=True)
                ).to_be_visible(),
            )
            product = self.db(
                lambda: Product.objects.get(name="Paquets d'essuie-mains — démonstration")
            )
            photo = image_file(size=(300, 200), color="lightblue")
            admin.get_by_label("Photographie du produit", exact=True).set_input_files(
                {"name": "demo.png", "mimeType": "image/png", "buffer": photo.read()}
            )
            walk.step(
                admin,
                "Téléverser une vraie photo fictive",
                lambda: admin.get_by_role(
                    "button", name="Ajouter la photographie", exact=True
                ).click(),
                lambda: expect(admin.locator(".product-main-photo")).to_be_visible(),
            )
            photo = image_file(size=(280, 200), color="white")
            admin.get_by_label("Photographie du produit", exact=True).set_input_files(
                {"name": "nouvelle.png", "mimeType": "image/png", "buffer": photo.read()}
            )
            walk.step(
                admin,
                "Remplacer en conservant l'ancienne photo",
                lambda: admin.get_by_role(
                    "button", name="Remplacer la photographie", exact=True
                ).click(),
                lambda: expect(admin.locator(".photo-history img")).to_have_count(2),
            )
            admin.get_by_role("link", name="Configurer les droits et plafonds", exact=True).click()
            row = admin.locator("tbody tr").filter(has_text=self.floor.name)
            row.get_by_label("Demande autorisée", exact=True).check()
            row.get_by_label("Maximum par demande", exact=True).fill("4")
            walk.step(
                admin,
                "Enregistrer droits et plafond par unité",
                lambda: admin.get_by_role(
                    "button", name="Enregistrer les droits et plafonds", exact=True
                ).click(),
                lambda: expect(
                    admin.get_by_text("demande autorisée, maximum 4 paquet", exact=False)
                ).to_be_visible(),
            )
            operator = self.page(browser, walk, self.replenisher)
            operator.goto(self.live_server_url + "/stocks/inventaire-initial")
            walk.step(
                operator,
                "Inventorier explicitement à zéro",
                lambda: (
                    operator.get_by_label(f"Quantité physique — {product.name}", exact=True).fill(
                        "0"
                    ),
                    operator.get_by_role(
                        "button", name="Confirmer l'inventaire", exact=True
                    ).click(),
                ),
                lambda: expect(operator.locator("#stock-result")).to_contain_text(
                    "Lot enregistré une seule fois"
                ),
            )
            self.assertEqual(self.db(lambda: self.stock(product)[0]), 0)
            admin.reload()
            admin.goto(self.live_server_url + f"/catalogue/{product.pk}")
            admin.get_by_label("J'ai vérifié l'inventaire confirmé", exact=True).check()
            admin.get_by_label(
                "J'ai vérifié que la photo correspond à l'unité comptée", exact=True
            ).check()
            walk.step(
                admin,
                "Activer sans fournisseur après vérification",
                lambda: admin.get_by_role("button", name="Activer le produit", exact=True).click(),
                lambda: expect(
                    admin.get_by_text("Produit actif, vérifié par la gestionnaire.", exact=True)
                ).to_be_visible(),
            )
            expect(admin.get_by_text("Sans fournisseur configuré.", exact=True)).to_be_visible()
            admin.get_by_role("link", name="Modifier la fiche produit", exact=True).click()
            expect(admin.get_by_label("Unité de comptage unique", exact=True)).to_be_disabled()
            walk.step(
                admin,
                "Modifier seuil et cible avec les vrais steppers",
                lambda: (
                    admin.get_by_role("button", name="Augmenter — Stock bas à", exact=True).click(),
                    admin.get_by_role(
                        "button", name="Enregistrer les paramètres", exact=True
                    ).click(),
                ),
                lambda: expect(
                    admin.get_by_role("heading", name=product.name, exact=True)
                ).to_be_visible(),
            )
            admin.get_by_role("link", name="Configurer les fournisseurs", exact=True).click()
            group = admin.get_by_role("group", name="Fournisseur fictif facultatif", exact=True)
            group.get_by_label("Associer ce fournisseur", exact=True).check()
            group.get_by_label("Fournisseur habituel", exact=True).check()
            group.get_by_label(
                "Référence du produit chez le fournisseur (facultative)", exact=True
            ).fill("FICTIF-42")
            group.get_by_label("Prix indicatif", exact=True).fill("12.30")
            expect(group.locator('input[name$="-currency"]')).to_be_hidden()
            walk.step(
                admin,
                "Associer facultativement le fournisseur après activation",
                lambda: admin.get_by_role(
                    "button", name="Enregistrer les fournisseurs", exact=True
                ).click(),
                lambda: expect(
                    admin.get_by_text("Fournisseur fictif facultatif", exact=False)
                ).to_be_visible(),
            )
            admin.get_by_label(
                "Je confirme le retrait des nouvelles demandes, sans supprimer l'historique ni les engagements",
                exact=True,
            ).check()
            walk.step(
                admin,
                "Archiver en conservant les photographies et historiques",
                lambda: admin.get_by_role("button", name="Archiver le produit", exact=True).click(),
                lambda: expect(
                    admin.get_by_text(
                        "Produit archivé. Les historiques sont conservés.", exact=True
                    )
                ).to_be_visible(),
            )
            walk.step(
                admin,
                "Consulter l'historique des paramètres",
                lambda: admin.get_by_role(
                    "link", name="Historique des paramètres", exact=True
                ).click(),
                lambda: expect(admin.get_by_text(product.code, exact=False)).to_be_visible(),
            )
            walk.inspect(admin, "ADMIN", "historique-catalogue")
            admin.goto(self.live_server_url + f"/catalogue/{product.pk}")
            admin.get_by_label("J'ai vérifié l'inventaire confirmé", exact=True).check()
            admin.get_by_label(
                "J'ai vérifié que la photo correspond à l'unité comptée", exact=True
            ).check()
            walk.step(
                admin,
                "Revérifier et réactiver une référence archivée sans refaire l'inventaire",
                lambda: admin.get_by_role("button", name="Activer le produit", exact=True).click(),
                lambda: expect(
                    admin.get_by_text("Produit actif, vérifié par la gestionnaire.", exact=True)
                ).to_be_visible(),
            )
            self.assertEqual(self.db(lambda: self.stock(product)[:3]), (0, 0, 0))
            expect(admin.locator(".photo-history img")).to_have_count(2)

            browser.close()
        walk.clean(self)

    def test_08_stock_race_permissions_and_another_assistant_request(self):
        product = self.demandable_product(physical=4, maximum=10)
        independent = self.demandable_product(code="RECETTE-CONCURRENT")
        other = User.objects.create_user(
            "autre-demandeur-a-recette",
            FICTIVE_PASSWORD,
            display_name="Autre demandeur A recette",
            role="ASSISTANT",
            floor=self.floor,
            must_change_password=False,
        )
        walk = Walk(self._testMethodName)
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            assistant = self.page(browser, walk, self.assistant)
            operator = self.page(browser, walk, self.replenisher)
            second = self.page(browser, walk, self.replenisher)
            assistant.get_by_role("link", name="Faire une demande", exact=True).click()
            walk.step(
                assistant,
                "Préparer la dernière disponibilité affichée",
                lambda: (
                    assistant.get_by_label(f"Quantité demandée — {product.name}", exact=True).fill(
                        "4"
                    ),
                    assistant.locator("#cart-review").click(),
                ),
                lambda: expect(assistant.locator("#request-review")).to_be_visible(),
            )
            # Retarder volontairement les lectures avec une vraie réponse antérieure :
            # le nouveau rafraîchissement désactive sinon l'envoi avant la course.
            # On veut toujours éprouver le refus serveur d'une saisie obsolète.
            earlier_catalog = assistant.request.get(
                self.live_server_url + "/api/v1/request-catalog"
            ).json()
            assistant.route(
                "**/api/v1/request-catalog?*", lambda route: route.fulfill(json=earlier_catalog)
            )
            operator.goto(self.live_server_url + "/stocks")
            walk.step(
                operator,
                "Un autre compte corrige la quantité pendant le panier",
                lambda: (
                    operator.get_by_label(f"Quantité physique — {product.name}", exact=True).fill(
                        "2"
                    ),
                    operator.locator("#stock-confirm").click(),
                ),
                lambda: expect(operator.locator("#stock-result")).to_contain_text(
                    "Lot enregistré une seule fois"
                ),
            )
            walk.expected_incident = True
            walk.step(
                assistant,
                "Le serveur refuse le panier devenu indisponible",
                lambda: assistant.locator("#request-send").click(),
                lambda: expect(assistant.locator("#operation-result")).to_contain_text(
                    "Maximum possible: 2"
                ),
            )
            walk.expected_incident = False
            assistant.unroute("**/api/v1/request-catalog?*")
            expect(
                assistant.get_by_label(f"Quantité demandée — {product.name}", exact=True)
            ).to_have_value("4")
            self.assertEqual(self.db(lambda: InternalRequest.objects.count()), 0)
            walk.step(
                assistant,
                "Corriger la sélection conservée et envoyer",
                lambda: (
                    assistant.get_by_label(f"Quantité demandée — {product.name}", exact=True).fill(
                        "2"
                    ),
                    assistant.locator("#cart-review").click(),
                    assistant.locator("#request-send").click(),
                ),
                lambda: expect(
                    assistant.get_by_role("link", name="Suivre cette demande", exact=True)
                ).to_be_visible(),
            )
            identifier = self.db(lambda: str(InternalRequest.objects.get().pk))
            other_page = self.page(browser, walk, other)
            walk.expected_incident = True
            response = other_page.goto(self.live_server_url + f"/demandes/{identifier}")
            self.assertEqual(response.status, 404)
            for user_page in [assistant, operator]:
                response = user_page.request.get(
                    self.live_server_url + "/administration/utilisateurs"
                )
                self.assertEqual(response.status, 403)
            walk.expected_incident = False
            operator.goto(self.live_server_url + "/stocks")
            second.goto(self.live_server_url + "/stocks")
            walk.step(
                operator,
                "Saisir une quantité sur une première version",
                lambda: operator.get_by_label(
                    f"Quantité physique — {independent.name}", exact=True
                ).fill("8"),
                lambda: expect(operator.locator("#stock-confirm")).to_be_enabled(),
            )
            walk.step(
                second,
                "Une seconde session confirme une version plus récente",
                lambda: (
                    second.get_by_label(f"Quantité physique — {independent.name}", exact=True).fill(
                        "7"
                    ),
                    second.locator("#stock-confirm").click(),
                ),
                lambda: expect(second.locator("#stock-result")).to_contain_text(
                    "Lot enregistré une seule fois"
                ),
            )
            walk.expected_incident = True
            walk.step(
                operator,
                "La première session reçoit un conflit sans écriture",
                lambda: operator.locator("#stock-confirm").click(),
                lambda: expect(operator.locator("#stock-conflicts")).to_be_visible(),
            )
            walk.expected_incident = False
            self.assertEqual(self.db(lambda: self.stock(independent)[0]), 7)
            walk.step(
                operator,
                "Recontrôler explicitement la version actuelle",
                lambda: operator.locator("#review-values").click(),
                lambda: expect(operator.locator("#stock-confirm")).to_be_enabled(),
            )
            walk.step(
                operator,
                "Confirmer le lot recontrôlé",
                lambda: operator.locator("#stock-confirm").click(),
                lambda: expect(operator.locator("#stock-result")).to_contain_text(
                    "Lot enregistré une seule fois"
                ),
            )
            self.assertEqual(self.db(lambda: self.stock(independent)[0]), 8)
            walk.inspect(operator, "REPLENISHER", "conflit-resolu")
            browser.close()
        walk.clean(self)

    def test_09_lost_responses_double_click_and_late_cancel_server_refusal(self):
        product = self.demandable_product()
        walk = Walk(self._testMethodName)
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            assistant = self.page(browser, walk, self.assistant)
            operator = self.page(browser, walk, self.replenisher)
            assistant.get_by_role("link", name="Faire une demande", exact=True).click()
            assistant.get_by_label(f"Quantité demandée — {product.name}", exact=True).fill("2")
            assistant.locator("#cart-review").click()

            def lose(route):
                upstream = route.fetch()
                self.assertEqual(upstream.status, 201)
                route.abort("failed")

            assistant.route("**/api/v1/requests", lose, times=1)
            walk.expected_incident = True
            walk.step(
                assistant,
                "Double clic et réponse perdue après confirmation réelle",
                lambda: assistant.locator("#request-send").evaluate("(b)=>{b.click();b.click();}"),
                lambda: expect(assistant.locator("#operation-recovery")).to_be_visible(),
            )
            self.assertEqual(self.db(lambda: InternalRequest.objects.count()), 1)
            walk.step(
                assistant,
                "Vérifier le résultat plutôt que réenvoyer",
                lambda: assistant.locator("#operation-check").click(),
                lambda: expect(
                    assistant.get_by_role("link", name="Suivre cette demande", exact=True)
                ).to_be_visible(),
            )
            walk.expected_incident = False
            assistant.get_by_role("link", name="Suivre cette demande", exact=True).click()
            identifier = assistant.url.rsplit("/", 1)[-1]
            self.claim_ui(walk, operator, identifier)
            operator.locator("#delivery-review").click()
            operator.route(f"**/api/v1/requests/{identifier}/deliveries", lose, times=1)
            walk.expected_incident = True
            walk.step(
                operator,
                "Double confirmation de remise avec réponse perdue",
                lambda: operator.locator("#delivery-confirm").evaluate(
                    "(b)=>{b.click();b.click();}"
                ),
                lambda: expect(operator.locator("#operation-recovery")).to_be_visible(),
            )
            self.assertEqual(self.db(lambda: Delivery.objects.count()), 1)
            walk.step(
                operator,
                "Retrouver l'état confirmé de la même intention",
                lambda: operator.locator("#operation-check").click(),
                lambda: expect(operator.locator("#request-status")).to_have_text("Livrée"),
            )
            walk.expected_incident = False
            self.assertEqual(self.db(lambda: self.stock(product)[:3]), (8, 0, 8))
            assistant.reload()
            expect(assistant.locator("#cancel-open")).to_have_count(0)
            walk.expected_incident = True
            version = self.db(lambda: InternalRequest.objects.get(pk=identifier).version)
            result = browser_command(
                assistant,
                f"/api/v1/requests/{identifier}/cancel",
                "requests.cancel",
                {"expected_version": version},
                identifier,
            )
            self.assertEqual(result["status"], 409)
            walk.expected_incident = False
            self.assertEqual(self.db(lambda: Delivery.objects.count()), 1)
            self.assertEqual(self.db(lambda: self.stock(product)[:3]), (8, 0, 8))
            assistant.reload()
            walk.inspect(assistant, "ASSISTANT", "annulation-apres-remise-refusee")
            browser.close()
        walk.clean(self)

    def test_10_multi_adjustments_cancel_atomic_anomaly_and_hold(self):
        products = [self.demandable_product(code=f"LOT-RECETTE-{i}") for i in range(3)]
        self.assertEqual(self.send(products[:1], [4]).status_code, 201)
        walk = Walk(self._testMethodName)
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            operator = self.page(browser, walk, self.replenisher)
            admin = self.page(browser, walk, self.admin)
            operator.get_by_role("link", name="Stocks", exact=True).click()
            for product in products:
                operator.get_by_role(
                    "button", name=f"Augmenter — {product.name}", exact=True
                ).click()
            walk.step(
                operator,
                "Annuler toutes les modifications provisoires",
                lambda: operator.locator("#stock-cancel").click(),
                lambda: expect(operator.locator(".stock-dirty")).to_have_count(0),
            )
            self.assertEqual(
                self.db(lambda: StockEntry.objects.filter(kind="MANUAL_ADJUSTMENT").count()), 0
            )
            for product in products:
                operator.get_by_label(f"Quantité physique — {product.name}", exact=True).fill("12")
            walk.step(
                operator,
                "Une confirmation unique de trois produits",
                lambda: operator.locator("#stock-confirm").click(),
                lambda: expect(operator.locator("#stock-result")).to_contain_text(
                    "Lot enregistré une seule fois"
                ),
            )
            self.assertEqual(self.db(lambda: [self.stock(p)[0] for p in products]), [12, 12, 12])
            operator.get_by_label(f"Quantité physique — {products[0].name}", exact=True).fill("3")
            operator.get_by_label(f"Quantité physique — {products[1].name}", exact=True).fill("9")
            walk.expected_incident = True
            walk.step(
                operator,
                "Refuser atomiquement un lot inférieur aux réservations",
                lambda: operator.locator("#stock-confirm").click(),
                lambda: expect(operator.locator("#stock-result")).to_contain_text(
                    "Signalement transmis"
                ),
            )
            walk.expected_incident = False
            self.assertEqual(self.db(lambda: [self.stock(p)[0] for p in products]), [12, 12, 12])
            self.assertEqual(self.db(lambda: StockCase.objects.count()), 1)
            walk.step(
                operator,
                "Ouvrir le dossier de stock sans régularisation M6",
                lambda: operator.get_by_role(
                    "link", name="Ouvrir le signalement", exact=True
                ).click(),
                lambda: expect(
                    operator.get_by_role("heading", name="Ce qui a été signalé", exact=True)
                ).to_be_visible(),
            )
            walk.inspect(operator, "REPLENISHER", "anomalie-stock")
            admin.get_by_role("link", name="Signalements", exact=True).click()
            case_id = self.db(lambda: StockCase.objects.get().pk)
            admin.locator(f'a[href^="/dossiers/{case_id}"]').click()
            expect(
                admin.get_by_role("heading", name="Ce qui a été signalé", exact=True)
            ).to_be_visible()
            walk.inspect(admin, "ADMIN", "supervision-dossier")
            operator.goto(self.live_server_url + "/stocks")
            expect(
                operator.get_by_role("button", name=f"Diminuer — {products[0].name}", exact=True)
            ).to_be_disabled()
            browser.close()
        walk.clean(self)

    def test_11_viewport_matrix_keyboard_and_browser_zoom(self):
        request, products = self.prepare(quantities=(2, 3))
        walk = Walk(self._testMethodName)
        issues = []
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            for width, height in [(1280, 800), (390, 844)]:
                for user, paths in self.surfaces(request, products[0]):
                    page = self.page(browser, walk, user, width, height)
                    for index, path in enumerate(paths):
                        page.goto(self.live_server_url + path)
                        a11y = walk.inspect(page, user.role, f"{width}-{index}")
                        self.assertLessEqual(
                            page.evaluate("document.documentElement.scrollWidth"), width, path
                        )
                        issues += [
                            (width, user.role, path, v["id"])
                            for v in a11y["violations"]
                            if v["impact"] in {"serious", "critical"}
                        ]
                    if user.role == "ADMIN" and width == 390:
                        page.goto(self.live_server_url + f"/catalogue/{products[0].pk}/historique")
                        region = page.locator(".table-scroll").first
                        needs_horizontal_scroll = region.evaluate("n=>n.scrollWidth>n.clientWidth")
                        if needs_horizontal_scroll:
                            page.locator("body").click(position={"x": 1, "y": 1})
                            for _ in range(120):
                                page.keyboard.press("Tab")
                                if region.evaluate("n=>document.activeElement===n"):
                                    break
                            expect(region).to_be_focused()
                            walk.step(
                                page,
                                "Défiler le tableau historique avec les flèches du clavier",
                                lambda page=page: page.keyboard.press("ArrowRight"),
                                lambda page=page: page.wait_for_function(
                                    "()=>document.querySelector('.table-scroll').scrollLeft>0"
                                ),
                            )
                        else:
                            # Le retour à la ligne peut désormais rendre le tableau entier lisible.
                            # Le composant retire alors son arrêt Tab devenu inutile.
                            expect(region).to_have_attribute("tabindex", "-1")
                            self.assertLessEqual(
                                region.locator("table").evaluate(
                                    "n=>n.getBoundingClientRect().width"
                                ),
                                region.evaluate("n=>n.clientWidth") + 1,
                            )
                    if user.role == "ADMIN" and width == 390:
                        walk.step(
                            page,
                            "Ouvrir le menu mobile au clavier",
                            lambda page=page: (
                                page.get_by_role(
                                    "button", name="Ouvrir la navigation", exact=True
                                ).focus(),
                                page.keyboard.press("Enter"),
                            ),
                            lambda page=page: expect(
                                page.get_by_role("link", name="Catalogue", exact=True)
                            ).to_be_visible(),
                        )
                        walk.inspect(page, "ADMIN", "menu-mobile-ouvert")
                        walk.step(
                            page,
                            "Fermer avec Échap et restaurer le focus",
                            lambda page=page: page.keyboard.press("Escape"),
                            lambda page=page: expect(
                                page.get_by_role("button", name="Ouvrir la navigation", exact=True)
                            ).to_be_focused(),
                        )
                    page.context.close()
            # Seuils intermédiaires : navigation, panneaux repliables et panier.
            for width, height in [(375, 812), (768, 1024), (1024, 768), (1920, 1080)]:
                for user, path in [
                    (self.admin, "/catalogue"),
                    (self.assistant, "/demandes/nouvelle"),
                    (self.replenisher, "/stocks"),
                ]:
                    page = self.page(browser, walk, user, width, height)
                    page.goto(self.live_server_url + path)
                    for summary in page.locator("summary:visible").all():
                        summary.click()
                    walk.inspect(page, user.role, f"panels-{width}")
                    self.assertLessEqual(
                        page.evaluate("document.documentElement.scrollWidth"), width
                    )
                    page.context.close()
            browser.close()
            extension = Path("tests/assets/zoom-extension").resolve()
            for user, path in [
                (self.admin, "/catalogue"),
                (self.assistant, "/demandes/nouvelle"),
                (self.replenisher, f"/demandes/{request.pk}"),
            ]:
                with TemporaryDirectory() as profile:
                    context = pw.chromium.launch_persistent_context(
                        profile,
                        channel="chromium",
                        headless=True,
                        viewport={"width": 1440, "height": 900},
                        args=[
                            f"--disable-extensions-except={extension}",
                            f"--load-extension={extension}",
                        ],
                    )
                    page = walk.attach(context.new_page(), user.role)
                    login(walk, page, self.live_server_url, user, FICTIVE_PASSWORD)
                    page.goto(self.live_server_url + path)
                    worker = (
                        context.service_workers[0]
                        if context.service_workers
                        else context.wait_for_event("serviceworker")
                    )
                    for factor in [1.25, 2]:
                        self.assertEqual(worker.evaluate("(f)=>setRecetteZoom(f)", factor), factor)
                        page.wait_for_timeout(300)
                        walk.inspect(page, user.role, f"zoom-{factor}")
                        self.assertLessEqual(
                            page.evaluate("document.documentElement.scrollWidth"),
                            page.evaluate("innerWidth"),
                        )
                    context.close()
        walk.clean(self)
        self.assertEqual(issues, [])

    def test_12_heavy_data_pagination_empty_search_and_real_upload_errors(self):
        for index in range(1001):
            self.create_product(code=f"VOLUME-{index:04}", name=f"Fourniture de recette {index:04}")
        walk = Walk(self._testMethodName)
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            admin = self.page(browser, walk, self.admin)
            admin.get_by_role("link", name="Catalogue", exact=True).click()
            expect(admin.locator("tbody tr")).to_have_count(50)
            walk.step(
                admin,
                "Parcourir réellement la page suivante du catalogue volumineux",
                lambda: admin.get_by_role("link", name="Produits suivants", exact=True).click(),
                lambda: expect(admin.get_by_text("VOLUME-0050", exact=True)).to_be_visible(),
            )
            walk.step(
                admin,
                "Rechercher la dernière référence parmi1001 produits",
                lambda: (
                    admin.get_by_label("Rechercher un produit", exact=True).fill("VOLUME-1000"),
                    admin.get_by_role("button", name="Rechercher", exact=True).click(),
                ),
                lambda: expect(admin.locator("tbody tr")).to_have_count(1),
            )
            admin.get_by_role("link", name="Ouvrir Fourniture de recette 1000", exact=True).click()
            walk.expected_incident = True
            admin.get_by_label("Photographie du produit", exact=True).set_input_files(
                {
                    "name": "fausse-image.jpg",
                    "mimeType": "image/jpeg",
                    "buffer": b"<script>window.recipeXSS=true</script>",
                }
            )
            walk.step(
                admin,
                "Rejeter un faux fichier image sans succès trompeur",
                lambda: admin.get_by_role(
                    "button", name="Ajouter la photographie", exact=True
                ).click(),
                lambda: expect(admin.locator(".errorlist")).to_be_visible(),
            )
            walk.expected_incident = False
            self.assertIsNone(admin.evaluate("window.recipeXSS"))
            walk.inspect(admin, "ADMIN", "upload-refuse")
            assistant = self.page(browser, walk, self.assistant)
            assistant.get_by_role("link", name="Faire une demande", exact=True).click()
            expect(assistant.locator("#request-empty")).to_be_visible()
            walk.inspect(assistant, "ASSISTANT", "catalogue-autorise-vide")
            operator = self.page(browser, walk, self.replenisher)
            operator.goto(self.live_server_url + "/stocks/inventaire-initial")
            expect(operator.locator(".stock-row")).to_have_count(50)
            walk.step(
                operator,
                "Charger les produits suivants sans oublier les premiers",
                lambda: operator.locator("#stock-more").click(),
                lambda: expect(operator.locator(".stock-row")).to_have_count(100),
            )
            operator.locator("#stock-search").fill("VOLUME-1000")
            walk.step(
                operator,
                "Rechercher dans l'inventaire volumineux",
                lambda: operator.get_by_role("button", name="Rechercher", exact=True).click(),
                lambda: expect(operator.locator(".stock-row")).to_have_count(1),
            )
            walk.inspect(operator, "REPLENISHER", "volume-inventaire")
            browser.close()
        walk.clean(self)

    def test_13_keyboard_real_flavour_inputs_offline_and_performance(self):
        product = self.demandable_product(code="CLAVIER-PERFORMANCE")
        name = (
            "Essai d'Élodie <img src=x onerror=window.recetteXSS=true> — fourniture de démonstration pour un service pilote fictif à nom composé "
            + "complément de recette " * 3
        )[:150]
        display_name = (
            "Demandeur A de recette au nom composé pour le contrôle des textes longs " * 2
        )[:150]
        self.assertEqual(
            self.update(self.assistant, {"display_name": display_name}).status_code, 200
        )
        self.assertEqual(self.modify(product, name=name).status_code, 200)
        product.refresh_from_db()
        category = Category.objects.create(name="Hygiène d'unité — recette")
        self.assertEqual(self.modify(product, category_id=str(category.pk)).status_code, 200)
        walk = Walk(self._testMethodName)
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = self.page(browser, walk, self.assistant)
            page.add_init_script("""window.recetteVitals={lcp:0,cls:0,events:[]};
            new PerformanceObserver(l=>{for(const e of l.getEntries())recetteVitals.lcp=e.startTime}).observe({type:'largest-contentful-paint',buffered:true});
            new PerformanceObserver(l=>{for(const e of l.getEntries())if(!e.hadRecentInput)recetteVitals.cls+=e.value}).observe({type:'layout-shift',buffered:true});
            new PerformanceObserver(l=>{for(const e of l.getEntries())if(e.interactionId)recetteVitals.events.push(e.duration)}).observe({type:'event',buffered:true,durationThreshold:16});""")
            cdp = page.context.new_cdp_session(page)
            cdp.send("Emulation.setCPUThrottlingRate", {"rate": 4})
            cdp.send("Network.enable")
            cdp.send(
                "Network.emulateNetworkConditions",
                {
                    "offline": False,
                    "latency": 50,
                    "downloadThroughput": 200000,
                    "uploadThroughput": 100000,
                },
            )
            page.get_by_role("link", name="Faire une demande", exact=True).click()
            page.wait_for_load_state("networkidle")
            self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 1440)
            walk.step(
                page,
                "Filtrer par catégorie avec les vraies données",
                lambda: page.locator("#request-category").select_option(str(category.pk)),
                lambda: expect(page.locator(".request-product")).to_have_count(1),
            )
            self.assertIsNone(page.evaluate("window.recetteXSS"))
            walk.step(
                page,
                "Ajouter au clavier sans exécution du libellé contenant des chevrons",
                lambda: (
                    page.get_by_role("button", name=f"Augmenter — {name}", exact=True).focus(),
                    page.keyboard.press("Space"),
                ),
                lambda: expect(
                    page.get_by_label(f"Quantité demandée — {name}", exact=True)
                ).to_have_value("1"),
            )
            walk.step(
                page,
                "Diminuer au clavier",
                lambda: (
                    page.get_by_role("button", name=f"Diminuer — {name}", exact=True).focus(),
                    page.keyboard.press("Enter"),
                ),
                lambda: expect(
                    page.get_by_label(f"Quantité demandée — {name}", exact=True)
                ).to_have_value("0"),
            )
            page.get_by_role("button", name=f"Augmenter — {name}", exact=True).click()
            walk.step(
                page,
                "Vérifier au clavier et revenir à la sélection",
                lambda: (
                    page.locator("#cart-review").focus(),
                    page.keyboard.press("Enter"),
                    page.locator("#request-edit").focus(),
                    page.keyboard.press("Enter"),
                ),
                lambda: expect(page.locator("#request-review")).to_be_hidden(),
            )
            page.locator("#cart-review").click()
            page.context.set_offline(True)
            expect(page.locator("#request-send")).to_be_disabled()
            walk.inspect(page, "ASSISTANT", "hors-connexion-recapitulatif")
            page.context.set_offline(False)
            expect(page.locator("#request-send")).to_be_enabled()
            page.locator("#request-edit").click()
            walk.step(
                page,
                "Retirer le produit au moyen de la vraie corbeille",
                lambda: page.get_by_role("button", name=f"Retirer — {name}", exact=True).click(),
                lambda: expect(page.locator("#cart-count")).to_have_text(
                    "Aucun produit sélectionné"
                ),
            )
            page.wait_for_timeout(1500)
            metrics = page.evaluate("recetteVitals")
            (walk.folder / "performance.json").write_text(
                json.dumps(
                    {
                        "route": "/demandes/nouvelle",
                        "CPU_slowdown": 4,
                        "network_latency_ms": 50,
                        "download_bytes_per_second": 200000,
                        "metrics": metrics,
                        "interaction_duration_max_is_INP_proxy": True,
                    },
                    indent=2,
                )
                + "\n"
            )
            self.assertTrue(
                metrics["events"], "Aucune interaction mesurée, pas de proxy INP validable"
            )
            self.assertGreater(metrics["lcp"], 0)
            self.assertLess(metrics["lcp"], 4000)
            self.assertLess(metrics["cls"], 0.25)
            self.assertLess(max(metrics["events"], default=0), 500)
            self.assertEqual(self.db(lambda: InternalRequest.objects.count()), 0)
            page.emulate_media(reduced_motion="reduce")
            walk.inspect(page, "ASSISTANT", "reduced-motion")
            browser.close()
        walk.clean(self)

    def test_14_remaining_visible_controls_filters_and_navigation(self):
        products = [self.demandable_product(code=f"CONTROLES-{i}") for i in range(2)]
        walk = Walk(self._testMethodName)
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel="chromium")
            admin = self.page(browser, walk, self.admin)
            # Tous les liens du menu Gestionnaire sont réellement suivis, pas seulement résolus.
            targets = [
                "Catalogue",
                "Utilisateurs",
                "Unités",
                "Autorisations & plafonds",
                "Paramètres de stock",
                "Fournisseurs",
                "Catégories",
                "Stocks",
                "À activer",
                "Demandes",
                "Mouvements",
                "Signalements",
            ]
            for target in targets:
                walk.step(
                    admin,
                    f"Navigation Gestionnaire — {target}",
                    lambda target=target: (
                        admin.locator("#admin-navigation")
                        .get_by_role("link", name=target, exact=True)
                        .click()
                    ),
                    lambda: expect(admin.locator("main h1")).to_be_visible(),
                )
            admin.locator("#admin-navigation").get_by_role(
                "link", name="Utilisateurs", exact=True
            ).click()
            walk.step(
                admin,
                "Rechercher un compte puis restaurer la liste",
                lambda: (
                    admin.get_by_label("Rechercher un compte", exact=True).fill("demandeur"),
                    admin.get_by_role("button", name="Rechercher", exact=True).click(),
                ),
                lambda: expect(admin.locator("tbody tr")).to_have_count(1),
            )
            admin.goto(self.live_server_url + f"/catalogue/{products[0].pk}/droits")
            rows = admin.locator("tbody tr")
            row = rows.filter(has_text=self.other_floor.name)
            row.get_by_label("Demande autorisée", exact=True).check()
            row.get_by_role(
                "button", name=f"Augmenter le plafond de {self.other_floor.name}", exact=True
            ).click()
            row.get_by_role(
                "button", name=f"Augmenter le plafond de {self.other_floor.name}", exact=True
            ).click()
            row.get_by_role(
                "button", name=f"Diminuer le plafond de {self.other_floor.name}", exact=True
            ).click()
            walk.step(
                admin,
                "Les steppers du plafond enregistrent une vraie autorisation",
                lambda: admin.get_by_role(
                    "button", name="Enregistrer les droits et plafonds", exact=True
                ).click(),
                lambda: expect(
                    admin.get_by_text("demande autorisée, maximum 1 carton", exact=False)
                ).to_be_visible(),
            )
            admin.get_by_role("link", name="Modifier la fiche produit", exact=True).click()
            for field in ["Stock bas à", "Quantité souhaitée"]:
                admin.get_by_role("button", name=f"Augmenter — {field}", exact=True).click()
                admin.get_by_role("button", name=f"Diminuer — {field}", exact=True).click()
            admin.get_by_role("button", name="Enregistrer les paramètres", exact=True).click()
            walk.step(
                admin,
                "Afficher une photographie privée historique",
                lambda: admin.locator(".photo-history a").first.click(),
                lambda: expect(admin.locator("img")).to_be_visible(),
            )
            admin.wait_for_function("()=>document.querySelector('img').naturalWidth>0")
            response = admin.request.get(admin.url)
            self.assertEqual(response.status, 200)
            self.assertTrue(response.headers["content-type"].startswith("image/jpeg"))
            self.assertIn(
                "style-src 'self' 'unsafe-inline'", response.headers["content-security-policy"]
            )
            self.assertIn("default-src 'none'", response.headers["content-security-policy"])
            self.assertEqual(response.headers["x-content-type-options"], "nosniff")
            admin.go_back()
            response = admin.request.get(admin.url)
            self.assertNotIn("unsafe-inline", response.headers["content-security-policy"])
            # Les filtres et retours du panier manipulent les vrais produits.
            assistant = self.page(browser, walk, self.assistant)
            assistant.get_by_role("link", name="Faire une demande", exact=True).click()
            for product in products:
                assistant.get_by_role(
                    "button", name=f"Augmenter — {product.name}", exact=True
                ).click()
            walk.step(
                assistant,
                "Enlever une référence sélectionnée",
                lambda: assistant.get_by_role(
                    "button", name=f"Retirer — {products[0].name}", exact=True
                ).click(),
                lambda: expect(assistant.locator("#cart-count")).to_contain_text("1 référence"),
            )
            walk.step(
                assistant,
                "Vider la sélection entière",
                lambda: assistant.locator("#cart-clear").click(),
                lambda: expect(assistant.locator("#cart-count")).to_have_text(
                    "Aucun produit sélectionné"
                ),
            )
            assistant.get_by_role(
                "button", name=f"Augmenter — {products[0].name}", exact=True
            ).click()
            assistant.locator("#cart-review").click()
            walk.step(
                assistant,
                "Revenir du récapitulatif sans envoyer",
                lambda: assistant.locator("#request-edit").click(),
                lambda: expect(assistant.locator("#request-review")).to_be_hidden(),
            )
            assistant.locator("#cart-review").click()
            assistant.locator("#request-send").click()
            assistant.get_by_role("link", name="Suivre cette demande", exact=True).click()
            identifier = assistant.url.rsplit("/", 1)[-1]
            assistant.locator("main").get_by_role("link", name="Mes demandes", exact=True).click()
            reference = self.db(lambda: InternalRequest.objects.get(pk=identifier).reference)
            walk.step(
                assistant,
                "Filtrer le suivi par référence et statut",
                lambda: (
                    assistant.get_by_label("Rechercher une demande", exact=True).fill(reference),
                    assistant.get_by_label("Statut", exact=True).select_option("NEW"),
                    assistant.get_by_role("button", name="Rechercher", exact=True).click(),
                ),
                lambda: expect(assistant.locator("tbody tr")).to_have_count(1),
            )
            assistant.get_by_role("link", name="Mon historique", exact=True).click()
            today = timezone.localdate().isoformat()
            walk.step(
                assistant,
                "Filtrer l'historique par dates et produit",
                lambda: (
                    assistant.get_by_label("Du", exact=True).fill(today),
                    assistant.get_by_label("Au", exact=True).fill(today),
                    assistant.get_by_label("Produit", exact=True).select_option(
                        str(products[0].pk)
                    ),
                    assistant.get_by_role("button", name="Filtrer", exact=True).click(),
                ),
                lambda: expect(
                    assistant.get_by_role("heading", name=reference, exact=False)
                ).to_be_visible(),
            )
            operator = self.page(browser, walk, self.replenisher)
            self.claim_ui(walk, operator, identifier)
            operator.locator("#delivery-review").click()
            walk.step(
                operator,
                "Modifier le récapitulatif de remise sans mouvement",
                lambda: operator.locator("#delivery-back").click(),
                lambda: expect(operator.locator("#delivery-editor")).to_be_visible(),
            )
            self.assertEqual(self.db(lambda: Delivery.objects.count()), 0)
            operator.locator(".bottom-navigation").get_by_role(
                "link", name="Demandes", exact=True
            ).click()
            operator.locator(".queue-filters summary").click()
            walk.step(
                operator,
                "Filtrer les préparations avec un libellé français",
                lambda: (
                    operator.get_by_label("Statut", exact=False).select_option("PREPARING"),
                    operator.get_by_role("button", name="Filtrer", exact=True).click(),
                ),
                lambda: expect(
                    operator.get_by_text("Filtre actif : En préparation", exact=False)
                ).to_be_visible(),
            )
            walk.step(
                operator,
                "Restaurer les demandes accessibles",
                lambda: operator.get_by_role("link", name="Tout afficher", exact=True).click(),
                lambda: expect(
                    operator.get_by_role("link", name=reference, exact=True)
                ).to_be_visible(),
            )
            operator.get_by_role("link", name="Stocks", exact=True).click()
            operator.get_by_role(
                "button", name=f"Augmenter — {products[1].name}", exact=True
            ).click()
            walk.step(
                operator,
                "Filtrer les lignes modifiées",
                lambda: operator.locator("#stock-show-modified").click(),
                lambda: expect(operator.locator(".stock-row:visible")).to_have_count(1),
            )
            walk.step(
                operator,
                "Restaurer toutes les lignes",
                lambda: operator.locator("#stock-show-all").click(),
                lambda: expect(operator.locator(".stock-row:visible")).to_have_count(2),
            )
            operator.locator("#stock-cancel").click()
            operator.get_by_role("link", name="Mon activité", exact=True).click()
            for label in ["Ajustements manuels", "Baisses de stock", "Tous"]:
                walk.step(
                    operator,
                    f"Filtrer les mouvements — {label}",
                    lambda label=label: operator.get_by_role(
                        "button", name=label, exact=True
                    ).click(),
                    lambda label=label: expect(
                        operator.get_by_role("button", name=label, exact=True)
                    ).to_have_attribute("aria-pressed", "true"),
                )
            operator.get_by_role("link", name="Mon compte", exact=True).click()
            operator.get_by_role("link", name="Changer mon mot de passe", exact=True).click()
            toggle = operator.get_by_role(
                "button", name="Afficher : Nouveau mot de passe", exact=True
            )
            walk.step(
                operator,
                "Afficher puis masquer un champ de mot de passe vide",
                lambda: (
                    toggle.click(),
                    operator.get_by_role(
                        "button", name="Masquer : Nouveau mot de passe", exact=True
                    ).click(),
                ),
                lambda: expect(
                    operator.get_by_label("Nouveau mot de passe", exact=True)
                ).to_have_attribute("type", "password"),
            )
            operator.get_by_role("link", name="Mon compte", exact=True).click()
            walk.step(
                operator,
                "Fermer sa session actuelle",
                lambda: operator.get_by_role(
                    "button", name="Fermer cette session", exact=True
                ).first.click(),
                lambda: expect(
                    operator.get_by_role("heading", name="Se connecter", exact=True)
                ).to_be_visible(),
            )
            browser.close()
        walk.clean(self)

    def test_15_registration_never_arrived_resolve_then_new_send(self):
        product = self.demandable_product()
        walk = Walk(self._testMethodName)
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = self.page(browser, walk, self.assistant)
            page.get_by_role("link", name="Faire une demande", exact=True).click()
            page.get_by_label(f"Quantité demandée — {product.name}", exact=True).fill("1")
            page.locator("#cart-review").click()
            page.route("**/api/v1/operation-intents", lambda route: route.abort("failed"), times=1)
            walk.expected_incident = True
            walk.step(
                page,
                "Couper l'envoi avant l'inscription de l'intention",
                lambda: page.locator("#request-send").click(),
                lambda: expect(page.locator("#operation-result")).to_contain_text(
                    "Connexion indisponible pendant l'envoi"
                ),
            )
            self.assertEqual(self.db(lambda: InternalRequest.objects.count()), 0)
            walk.step(
                page,
                "Recharger sans réenvoyer une demande incertaine",
                lambda: page.reload(),
                lambda: expect(page.locator("#operation-result")).to_contain_text(
                    "Intention encore introuvable"
                ),
            )
            walk.step(
                page,
                "Résoudre la même intention non parvenue",
                lambda: page.locator("#operation-resolve").click(),
                lambda: expect(page.locator("#operation-recovery")).to_be_hidden(),
            )
            walk.expected_incident = False
            self.assertEqual(self.db(lambda: InternalRequest.objects.count()), 0)
            page.get_by_label(f"Quantité demandée — {product.name}", exact=True).fill("1")
            page.locator("#cart-review").click()
            walk.step(
                page,
                "Confirmer une nouvelle intention après résolution définitive",
                lambda: page.locator("#request-send").click(),
                lambda: expect(
                    page.get_by_role("link", name="Suivre cette demande", exact=True)
                ).to_be_visible(),
            )
            self.assertEqual(self.db(lambda: InternalRequest.objects.count()), 1)
            walk.inspect(page, "ASSISTANT", "intention-resolue-et-nouvelle-demande")
            browser.close()
        walk.clean(self)

    def test_16_reference_search_reauthentication_and_invalid_login(self):
        category = Category.objects.create(name="Catégorie fictive de recette")
        self.assertEqual(
            self.post_json(
                "/api/v1/suppliers",
                {
                    "code": "FOURNISSEUR-RECETTE",
                    "name": "Fournisseur de recette",
                    "active": True,
                    "website": "https://example.com",
                },
            ).status_code,
            201,
        )
        walk = Walk(self._testMethodName)
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            admin = self.page(browser, walk, self.admin)
            for kind, query, label in [
                ("category", "Catégorie fictive", category.name),
                ("floor", self.floor.name, self.floor.name),
                ("supplier", "Fournisseur de recette", "Fournisseur de recette"),
            ]:
                admin.goto(self.live_server_url + f"/configuration/{kind}")
                walk.step(
                    admin,
                    f"Rechercher la référence {kind}",
                    lambda query=query: (
                        admin.get_by_label("Rechercher", exact=True).fill(query),
                        admin.get_by_role("button", name="Rechercher", exact=True).click(),
                    ),
                    lambda label=label: expect(
                        admin.get_by_text(label, exact=True)
                    ).to_be_visible(),
                )
                walk.step(
                    admin,
                    f"Vérifier la recherche vide {kind}",
                    lambda: (
                        admin.get_by_label("Rechercher", exact=True).fill(
                            "reference-absente-de-recette"
                        ),
                        admin.get_by_role("button", name="Rechercher", exact=True).click(),
                    ),
                    lambda: expect(
                        admin.get_by_text("Aucun résultat pour cette recherche.", exact=True)
                    ).to_be_visible(),
                )
            admin.goto(self.live_server_url + "/configuration/supplier")
            admin.context.route(
                "https://example.com/**", lambda route: route.abort("blockedbyclient")
            )
            with (
                admin.expect_popup() as popup,
                admin.context.expect_event(
                    "request",
                    predicate=lambda request: request.url.startswith("https://example.com/"),
                ) as outbound,
            ):
                admin.get_by_role("link", name="Ouvrir le site", exact=True).click()
            self.assertTrue(outbound.value.is_navigation_request())
            self.assertEqual(outbound.value.url, "https://example.com/")
            (walk.folder / "external-link.json").write_text(
                json.dumps(
                    {
                        "url": outbound.value.url,
                        "new_tab_created": True,
                        "navigation_request": True,
                        "external_content_tested": False,
                        "interception": "requête externe volontairement bloquée ; aucun appel Django simulé",
                    },
                    indent=2,
                    ensure_ascii=False,
                )
                + "\n"
            )
            popup.value.close()
            # Uniquement une horloge de fraîcheur de session fictive, pas un historique métier.
            self.db(
                lambda: UserSession.objects.filter(user=self.admin, revoked_at=None).update(
                    authenticated_at=timezone.now() - timedelta(minutes=20)
                )
            )
            admin.goto(self.live_server_url + f"/administration/utilisateurs/{self.assistant.pk}")
            admin.get_by_label("Nom affiché", exact=True).fill(
                "Demandeur A recette après réauthentification"
            )
            walk.expected_incident = True
            walk.step(
                admin,
                "Exiger une nouvelle preuve du mot de passe Gestionnaire",
                lambda: admin.get_by_role("button", name="Enregistrer", exact=True).click(),
                lambda: expect(admin.locator(".errorlist")).to_contain_text("mot de passe"),
            )
            walk.expected_incident = False
            walk.step(
                admin,
                "Se réauthentifier avant l'opération sensible",
                lambda: (
                    admin.locator(".narrow form")
                    .get_by_label(
                        "Votre mot de passe Gestionnaire (si connexion ancienne)",
                        exact=True,
                    )
                    .fill(FICTIVE_PASSWORD),
                    admin.get_by_role("button", name="Enregistrer", exact=True).click(),
                ),
                lambda: expect(
                    admin.get_by_role("heading", name="Utilisateurs", exact=True)
                ).to_be_visible(),
            )
            public = walk.attach(
                browser.new_page(viewport={"width": 1280, "height": 800}), "PUBLIC"
            )
            public.goto(self.live_server_url + "/connexion")
            walk.expected_incident = True
            walk.step(
                public,
                "Refuser une connexion avec mot de passe incorrect",
                lambda: (
                    public.get_by_label("Identifiant", exact=True).fill(self.assistant.username),
                    public.get_by_label("Mot de passe", exact=True).fill(
                        "faux mot de passe de recette"
                    ),
                    public.get_by_role("button", name="Se connecter", exact=True).click(),
                ),
                lambda: expect(public.locator(".errorlist")).to_be_visible(),
            )
            walk.expected_incident = False
            walk.inspect(public, "PUBLIC", "connexion-refusee")
            browser.close()
        walk.clean(self)
