"""Présentation réelle des réceptions ; invariants et services inchangés."""

from django.test import TransactionTestCase

from apps.core.models import Operation
from apps.receiving.models import Receipt
from tests.test_notifications import NotificationFixtures


class GlobalPresentationTests(NotificationFixtures, TransactionTestCase):
    def receipt(self):
        product = self.demandable_product(code="UI-RECU", physical=0)
        order = self.buy([product], [20])
        self.assertEqual(self.receive(order, [23], [1]).status_code, 201)
        return product, order, Receipt.objects.get()

    def test_list_displays_products_quantities_and_isolation_without_stock_mutation(self):
        product, order, receipt = self.receipt()
        before = self.stock(product)
        operations = Operation.objects.count()
        response = self.admin_client.get("/receptions/historique")
        self.assertEqual(response.status_code, 200)
        for text in [
            product.name,
            "20 carton",
            "3 carton de surplus déclaré",
            "1 carton non conformes",
            "Voir le reçu",
            receipt.reference,
        ]:
            self.assertContains(response, text)
        self.assertEqual(self.stock(product), before)
        self.assertEqual(Operation.objects.count(), operations)

    def test_detail_groups_original_current_followup_and_keeps_real_links(self):
        product, order, receipt = self.receipt()
        response = self.admin_client.get(f"/receptions/recus/{receipt.pk}")
        for text in [
            "Déclaration initiale",
            "23 conformes",
            "Reçus sur commande",
            "Surplus déclaré",
            "Non conformes",
            "Suivi du reçu",
            "Corrections",
            "Dossiers associés",
            "Retour aux reçus",
        ]:
            self.assertContains(response, text)
        self.assertContains(response, f"/receptions/commandes/{order.pk}")
        self.assertContains(response, f"/receptions/recus/{receipt.pk}/open_receipt_case")
        self.assertEqual(self.stock(product)[:3], (20, 0, 20))

    def test_four_roles_keep_receipt_permissions(self):
        from apps.accounts.models import User
        from tests.test_accounts import FICTIVE_PASSWORD

        product, _, receipt = self.receipt()
        doctor = User.objects.create_user(
            "medecin-ui",
            FICTIVE_PASSWORD,
            display_name="Demandeur B",
            role="DOCTOR",
            floor=self.floor,
            must_change_password=False,
        )
        for user in [self.assistant, doctor]:
            client = self.logged(user)
            for path in ["/receptions/historique", f"/receptions/recus/{receipt.pk}"]:
                self.assertEqual(client.get(path).status_code, 403)
        response = self.operator.get(f"/receptions/recus/{receipt.pk}")
        self.assertContains(response, "Signaler une erreur")
        self.assertNotContains(response, "Vérifier ce reçu")
        self.assertEqual(self.stock(product)[:3], (20, 0, 20))

    def test_purchase_help_no_longer_denies_existing_receiving_functions(self):
        _, order, _ = self.receipt()
        response = self.admin_client.get(f"/achats/{order.pk}")
        self.assertContains(response, "réceptions et les rétablissements vérifiés")
        self.assertNotContains(response, "ne sont pas disponibles dans ce prototype")
        self.assertNotContains(response, "L’achat est enregistré, pas réceptionné.")

    def test_review_label_matches_existing_case_state_without_stock_movement(self):
        product, _, receipt = self.receipt()
        case = self.receipt_case(receipt)
        before = self.stock(product)
        self.assertContains(
            self.admin_client.get(f"/receptions/dossiers/{case.pk}"), "Commencer la vérification"
        )
        result = self.post_json(
            f"/api/v1/cases/{case.pk}/review",
            {
                "expected_version": case.version,
                "verification": True,
                "message": "Vérification fictive commencée.",
            },
        )
        self.assertEqual(result.status_code, 200, result.content)
        page = self.admin_client.get(f"/receptions/dossiers/{case.pk}")
        self.assertContains(page, "Ajouter une vérification")
        self.assertNotContains(page, ">Commencer la vérification</a>")
        self.assertContains(
            self.admin_client.get(f"/receptions/dossiers/{case.pk}/review"),
            "<h1>Ajouter une vérification</h1>",
            html=True,
        )
        self.assertEqual(self.stock(product), before)
