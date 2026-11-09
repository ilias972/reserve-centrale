"""Compteur de demandes : portée du rôle et changements d'état réels M4/M5."""

from django.test import Client, TransactionTestCase

from apps.accounts.models import User
from apps.core.models import Operation
from apps.requests.models import InternalRequest
from tests.test_accounts import FICTIVE_PASSWORD
from tests.test_deliveries import DeliveryFixtures


class NavigationCountTests(DeliveryFixtures, TransactionTestCase):
    def count(self, client=None):
        response = (client or self.operator).get("/api/v1/requests/pending-count")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response["Cache-Control"], "no-store")
        return response.json()["pending_count"]

    def test_count_follows_new_preparing_partial_and_delivered(self):
        self.assertEqual(self.count(), 0)
        request, products = self.requested()
        self.assertEqual(self.count(), 1)
        self.claim(request)
        request.refresh_from_db()
        self.assertEqual(self.count(), 1)
        self.assertEqual(self.deliver(request, [1], "LATER").status_code, 201)
        self.assertEqual(self.count(), 1)
        request.refresh_from_db()
        self.assertEqual(self.deliver(request, [3]).status_code, 201)
        self.assertEqual(self.count(), 0)
        self.assertEqual(self.stock(products[0])[:3], (6, 0, 6))

    def test_terminal_requests_cancelled_refused_and_shortage_are_not_counted(self):
        product = self.demandable_product(code="BADGE-TERMINAL", physical=30)
        for mode in ["cancel", "refuse", "shortage"]:
            response = self.send([product], [2])
            request = InternalRequest.objects.get(pk=response.json()["resource"]["id"])
            self.assertEqual(self.count(), 1)
            if mode == "cancel":
                result = self.cancel_request(request)
            else:
                self.claim(request)
                request.refresh_from_db()
                if mode == "refuse":
                    result = self.post_json(
                        f"/api/v1/requests/{request.pk}/refuse",
                        {"expected_version": request.version, "message": ""},
                        self.operator,
                    )
                else:
                    result = self.deliver(request, [0], "SHORTAGE")
            self.assertIn(result.status_code, {200, 201}, result.content)
            self.assertEqual(self.count(), 0)

    def test_other_assigned_operator_work_not_counted_but_new_shared_requests_are(self):
        product = self.demandable_product(code="BADGE-SCOPE", physical=20)
        other = User.objects.create_user(
            "autre-reap-badge", FICTIVE_PASSWORD, role="REPLENISHER", must_change_password=False
        )
        other_client = self.logged(other)
        requests = []
        for _ in range(3):
            response = self.send([product], [2])
            requests.append(InternalRequest.objects.get(pk=response.json()["resource"]["id"]))
        self.assertEqual(self.count(), 3)
        self.assertEqual(self.claim(requests[0]).status_code, 200)
        self.assertEqual(self.claim(requests[1], client=other_client).status_code, 200)
        self.assertEqual(self.count(), 2)
        self.assertEqual(self.count(other_client), 2)
        self.assertContains(self.operator.get("/mon-compte"), 'id="pending-request-count"')

    def test_counter_requires_replenisher_and_does_not_create_operations(self):
        self.requested()
        before = Operation.objects.count()
        for client in [self.admin_client, self.user_client]:
            self.assertEqual(client.get("/api/v1/requests/pending-count").status_code, 403)
            self.assertNotContains(client.get("/mon-compte"), 'id="pending-request-count"')
        self.assertEqual(Client().get("/api/v1/requests/pending-count").status_code, 401)
        for _ in range(3):
            self.assertEqual(self.count(), 1)
        self.assertEqual(Operation.objects.count(), before)

    def test_full_count_is_not_limited_by_page_size_or_list_search(self):
        product = self.demandable_product(code="BADGE-ALL", physical=60)
        for _ in range(51):
            self.assertEqual(self.send([product], [1]).status_code, 201)
        self.assertEqual(self.count(), 51)
        page = self.operator.get("/demandes?q=aucun-resultat&status=NEW")
        self.assertContains(page, ">51</span>")
        self.assertContains(page, "Aucune demande dans cette sélection.")
        self.assertEqual(self.stock(product)[:3], (60, 51, 9))
