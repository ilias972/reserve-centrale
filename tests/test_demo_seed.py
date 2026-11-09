"""Safety and repeatability checks for the fictional demonstration seed."""

import io
import os
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import TransactionTestCase, override_settings

from apps.accounts.models import AccountGuard, User
from apps.catalog.models import Category, Floor, Product, Supplier
from apps.inventory.models import StockBalance, StockEntry
from apps.purchasing.models import OrderLine, PurchaseOrder
from apps.receiving.models import Receipt
from apps.requests.models import Delivery, InternalRequest, RequestLine


@override_settings(
    DEBUG=True,
    NOTIFICATION_EXTERNAL_ALLOWED=False,
    NOTIFICATION_PUSH_ENABLED=False,
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
)
class DemoSeedTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.owner = User.objects.create_user(
            "local-manager",
            "fictional-local-password-2026-long",
            display_name="Gestionnaire local fictif",
            role=User.Role.ADMIN,
            must_change_password=False,
        )
        AccountGuard.objects.update_or_create(pk=1, defaults={"initialized": True})
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_database(), current_user")
            self.database_name, self.database_user = cursor.fetchone()
        self.environment = {
            "DJANGO_SETTINGS_MODULE": "config.settings.test",
            "DATABASE_USER": self.database_user,
            "RUNTIME_DATABASE_USER": "demo_runtime_role_not_used",
            "ALLOW_FICTITIOUS_DEMO_SEED": "1",
        }

    def _seed(self, output):
        call_command(
            "seed_demo",
            confirm_demo_database=self.database_name,
            stdout=output,
            verbosity=0,
        )

    def test_seed_creates_audited_fictional_flow_and_repeat_is_noop(self):
        first_output = io.StringIO()
        with patch.dict(os.environ, self.environment):
            self._seed(first_output)
        self.assertEqual(Product.objects.filter(code__startswith="DEMO-ITEM-").count(), 3)
        self.assertEqual(Floor.objects.filter(code__startswith="DEMO-UNIT-").count(), 3)
        self.assertEqual(Category.objects.count(), 2)
        self.assertEqual(Supplier.objects.filter(code="DEMO-SUP-001").count(), 1)
        self.assertEqual(User.objects.count(), 4)
        self.assertEqual(StockEntry.objects.count(), 6)
        self.assertEqual(InternalRequest.objects.count(), 1)
        self.assertEqual(Delivery.objects.count(), 1)
        self.assertEqual(PurchaseOrder.objects.count(), 1)
        self.assertEqual(Receipt.objects.count(), 1)

        for balance in StockBalance.objects.select_related("product"):
            self.assertIsNotNone(balance.physical)
            self.assertGreaterEqual(balance.reserved, 0)
            self.assertLessEqual(balance.reserved, balance.physical)
            self.assertEqual(balance.available, balance.physical - balance.reserved)
        request_line = RequestLine.objects.get(product__code="DEMO-ITEM-001")
        self.assertEqual(
            request_line.quantity,
            request_line.delivered + request_line.closed + request_line.reservation.remaining,
        )
        order_line = OrderLine.objects.get(product__code="DEMO-ITEM-001")
        self.assertEqual(
            order_line.quantity,
            order_line.received + order_line.cancelled + order_line.waiting,
        )
        self.assertEqual(
            (order_line.quantity, order_line.received, order_line.cancelled, order_line.waiting),
            (8, 4, 0, 4),
        )
        balance = StockBalance.objects.get(product__code="DEMO-ITEM-001")
        self.assertEqual((balance.physical, balance.reserved, balance.available), (22, 1, 21))
        self.assertEqual(first_output.getvalue().count(": Demo-"), 3)

        counts_before = (
            User.objects.count(),
            Product.objects.count(),
            StockEntry.objects.count(),
            InternalRequest.objects.count(),
            PurchaseOrder.objects.count(),
            Receipt.objects.count(),
        )
        second_output = io.StringIO()
        with patch.dict(os.environ, self.environment):
            self._seed(second_output)
        counts_after = (
            User.objects.count(),
            Product.objects.count(),
            StockEntry.objects.count(),
            InternalRequest.objects.count(),
            PurchaseOrder.objects.count(),
            Receipt.objects.count(),
        )
        self.assertEqual(counts_after, counts_before)
        self.assertIn("aucune donnée modifiée", second_output.getvalue())
        self.assertNotIn("Demo-", second_output.getvalue())

    def test_seed_refuses_without_explicit_opt_in_and_writes_nothing(self):
        env = {**self.environment, "ALLOW_FICTITIOUS_DEMO_SEED": "0"}
        with patch.dict(os.environ, env):
            with self.assertRaises(CommandError):
                self._seed(io.StringIO())
        self.assertEqual(Floor.objects.count(), 0)
        self.assertEqual(Product.objects.count(), 0)
        self.assertEqual(User.objects.count(), 1)

    def test_partial_demo_is_refused_without_cleanup(self):
        Product.objects.create(
            code="DEMO-ITEM-001",
            name="Entrée partielle fictive",
            created_by=self.owner,
            unit="pièce",
            minimum=0,
            target=0,
        )
        with patch.dict(os.environ, self.environment):
            with self.assertRaises(CommandError):
                self._seed(io.StringIO())
        self.assertEqual(Product.objects.count(), 1)
        self.assertEqual(User.objects.count(), 1)
        self.assertEqual(StockEntry.objects.count(), 0)
