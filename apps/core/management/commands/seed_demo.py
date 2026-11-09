"""Create a small, auditable and entirely fictional local demonstration dataset."""

import io
import os
import secrets
import uuid
from types import SimpleNamespace

from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from django.utils import timezone
from PIL import Image

from apps.accounts.models import AccountGuard, User
from apps.catalog.images import upload_photo
from apps.catalog.models import Category, Floor, Product, Supplier
from apps.catalog.services import ensure_registered as register_catalog
from apps.core.idempotency import execute_intent, register_intent
from apps.core.payloads import payload_hash
from apps.core.policies import ActorContext
from apps.inventory.models import StockBalance, StockEntry
from apps.inventory.services import ensure_registered as register_inventory
from apps.purchasing.models import OrderLine, PurchaseOrder
from apps.purchasing.services import ensure_registered as register_purchasing
from apps.receiving.models import Receipt
from apps.receiving.services import ensure_registered as register_receiving
from apps.requests.models import Delivery, InternalRequest, RequestLine
from apps.requests.services import ensure_registered as register_requests

SEED_CODE = "DEMO-ITEM-001"
DEMO_USERS = [
    ("demo_requester_a", User.Role.ASSISTANT, "Demandeur A — démonstration", "DEMO-UNIT-A"),
    ("demo_requester_b", User.Role.DOCTOR, "Demandeur B — démonstration", "DEMO-UNIT-B"),
    ("demo_preparer", User.Role.REPLENISHER, "Préparateur — démonstration", None),
]
FLOORS = [
    ("DEMO-UNIT-A", "Département A"),
    ("DEMO-UNIT-B", "Département B"),
    ("DEMO-UNIT-C", "Département C"),
]
PRODUCTS = [
    ("DEMO-ITEM-001", "Boîte de rangement", "Fournitures courantes", "boîte", 20, 3, 12),
    ("DEMO-ITEM-002", "Rouleau d’étiquettes", "Organisation", "rouleau", 9, 2, 8),
    ("DEMO-ITEM-003", "Marqueur effaçable", "Organisation", "pièce", 4, 1, 6),
]


class Command(BaseCommand):
    help = "Create fictitious demo data in a confirmed, isolated local PostgreSQL database."

    def add_arguments(self, parser):
        parser.add_argument(
            "--confirm-demo-database",
            required=True,
            help="Exact database name shown by SELECT current_database().",
        )

    def handle(self, *args, **options):
        database_name = self._current_database()
        self._check_target(database_name, options["confirm_demo_database"])
        if Product.objects.filter(code=SEED_CODE).exists():
            self._assert_complete_seed()
            self.stdout.write("Démonstration déjà complète ; aucune donnée modifiée.")
            return
        manager = self._assert_empty_install()
        manager_context = ActorContext(manager.pk, manager.auth_version)
        register_catalog()
        register_inventory()
        register_requests()
        register_purchasing()
        register_receiving()

        created_passwords = {}
        floors = {
            code: self._run(
                manager_context, "catalog.floor.create", {"code": code, "name": name}, 201
            )["resource"]["id"]
            for code, name in FLOORS
        }
        for username, role, display_name, floor_code in DEMO_USERS:
            password = "Demo-" + secrets.token_urlsafe(32) + "9!"
            User.objects.create_user(
                username,
                password,
                display_name=display_name,
                role=role,
                floor_id=floors[floor_code] if floor_code else None,
                must_change_password=False,
            )
            created_passwords[username] = password
        demo_users = {username: User.objects.get(username=username) for username, *_ in DEMO_USERS}
        contexts = {
            username: ActorContext(user.pk, user.auth_version)
            for username, user in demo_users.items()
        }

        category_ids = {}
        for name in sorted({row[2] for row in PRODUCTS}):
            result = self._run(manager_context, "catalog.category.create", {"name": name}, 201)
            category_ids[name] = result["resource"]["id"]
        supplier = self._run(
            manager_context,
            "catalog.supplier.create",
            {
                "code": "DEMO-SUP-001",
                "name": "Fournisseur Exemple",
                "website": "https://example.invalid",
            },
            201,
        )["resource"]

        products = []
        for code, name, category, unit, _initial, minimum, target in PRODUCTS:
            result = self._run(
                manager_context,
                "catalog.product.create",
                {
                    "code": code,
                    "name": name,
                    "category_id": category_ids[category],
                    "unit": unit,
                    "minimum": minimum,
                    "target": target,
                },
                201,
            )
            products.append(Product.objects.get(pk=result["resource"]["id"]))

        initial_lines = []
        for product, row in zip(products, PRODUCTS, strict=True):
            balance = StockBalance.objects.get(product=product)
            initial_lines.append(
                {
                    "product_id": str(product.pk),
                    "expected_stock_version": balance.version,
                    "expected_product_version": product.version,
                    "counted_quantity": row[4],
                }
            )
        self._run(
            contexts["demo_preparer"],
            "inventory.initial",
            {"lines": initial_lines},
            201,
        )

        image_bytes = self._demo_image()
        upload_request = SimpleNamespace(
            user=manager, user_session_id=None, correlation_id=uuid.uuid4()
        )
        for product in products:
            product.refresh_from_db()
            upload = SimpleUploadedFile(
                "fictitious-item.png", image_bytes, content_type="image/png"
            )
            response = upload_photo(
                upload_request,
                product.pk,
                upload,
                expected_version=product.version,
                identifier=str(uuid.uuid4()),
                key=uuid.uuid4().hex,
            )
            self._expect(response.status, 200, "photo")
            product.refresh_from_db()
            self._run(
                manager_context,
                "catalog.product.activate",
                {
                    "expected_version": product.version,
                    "inventory_verified": True,
                    "photo_verified": True,
                },
                200,
                resource_id=product.pk,
            )

        floor_permissions = {
            "DEMO-ITEM-001": ["DEMO-UNIT-A", "DEMO-UNIT-B"],
            "DEMO-ITEM-002": ["DEMO-UNIT-B"],
            "DEMO-ITEM-003": ["DEMO-UNIT-C"],
        }
        for product in products:
            product.refresh_from_db()
            permissions = [
                {
                    "floor_id": floors[floor_code],
                    "visible": True,
                    "demandable": True,
                    "maximum": 8,
                }
                for floor_code in floor_permissions[product.code]
            ]
            self._run(
                manager_context,
                "catalog.product.permissions",
                {"expected_version": product.version, "permissions": permissions},
                200,
                resource_id=product.pk,
            )

        product = products[0]
        product.refresh_from_db()
        self._run(
            manager_context,
            "catalog.product.suppliers",
            {
                "expected_version": product.version,
                "suppliers": [
                    {
                        "supplier_id": supplier["id"],
                        "preferred": True,
                        "reference": "EXAMPLE-ITEM-01",
                        "website": "https://example.invalid/item",
                    }
                ],
            },
            200,
            resource_id=product.pk,
        )
        product.refresh_from_db()
        purchase = self._run(
            manager_context,
            "orders.create",
            {
                "supplier_id": supplier["id"],
                "purchased_on": timezone.localdate().isoformat(),
                "lines": [
                    {
                        "product_id": str(product.pk),
                        "quantity": 8,
                        "expected_product_version": product.version,
                    }
                ],
                "purchase_confirmed": True,
            },
            201,
        )
        order = PurchaseOrder.objects.get(pk=purchase["resource"]["id"])
        order_line = OrderLine.objects.get(order=order)
        balance = StockBalance.objects.get(product=product)
        self._run(
            contexts["demo_preparer"],
            "receipts.create",
            {
                "expected_version": order.version,
                "lines": [
                    {
                        "order_line_id": str(order_line.pk),
                        "conforming": 4,
                        "nonconforming": 0,
                        "nonconformity": "",
                        "expected_stock_version": balance.version,
                    }
                ],
            },
            201,
            resource_id=order.pk,
        )

        request_result = self._run(
            contexts["demo_requester_a"],
            "requests.create",
            {"lines": [{"product_id": str(product.pk), "quantity": 3}]},
            201,
        )
        request = InternalRequest.objects.get(pk=request_result["resource"]["id"])
        self._run(
            contexts["demo_preparer"],
            "requests.claim",
            {"expected_version": request.version},
            200,
            resource_id=request.pk,
        )
        request.refresh_from_db()
        request_line = RequestLine.objects.get(request=request)
        self._run(
            contexts["demo_preparer"],
            "requests.deliver",
            {
                "expected_version": request.version,
                "mode": "LATER",
                "lines": [
                    {
                        "request_line_id": str(request_line.pk),
                        "delivered_quantity": 2,
                        "missing_quantity": 0,
                    }
                ],
            },
            201,
            resource_id=request.pk,
        )
        self._assert_complete_seed()

        self.stdout.write(self.style.SUCCESS("Données fictives créées par les services métier."))
        self.stdout.write(
            "Comptes de démonstration — mots de passe aléatoires affichés une seule fois :"
        )
        for username, password in created_passwords.items():
            self.stdout.write(f"{username}: {password}")
        self.stdout.write("Ne copiez pas ces mots de passe dans un journal ni dans Git.")

    def _run(self, context, command_type, payload, expected_status, resource_id=None):
        intent_id, key = uuid.uuid4(), uuid.uuid4().hex
        register_intent(
            context,
            intent_id=intent_id,
            key=key,
            fingerprint=payload_hash(payload),
            command_type=command_type,
            resource_id=resource_id,
        )
        outcome = execute_intent(context, intent_id=intent_id, key=key, payload=payload)
        self._expect(outcome.status, expected_status, command_type)
        return outcome.body

    @staticmethod
    def _current_database():
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_database(), current_user")
            database_name, database_user = cursor.fetchone()
        if not database_name or not database_user:
            raise CommandError("Impossible d'identifier la base PostgreSQL actuelle.")
        return database_name

    def _check_target(self, database_name, confirmed):
        module = os.environ.get("DJANGO_SETTINGS_MODULE", "")
        test_mode = module == "config.settings.test" and settings.ROOT_URLCONF == "tests.urls"
        prefix = "test_stock_app_" if test_mode else "demo_stock_app_"
        expected_module = "config.settings.test" if test_mode else "config.settings.development"
        host = connection.settings_dict.get("HOST")
        owner = os.environ.get("DATABASE_USER")
        runtime = os.environ.get("RUNTIME_DATABASE_USER")
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_user")
            current_user = cursor.fetchone()[0]
        if (
            module != expected_module
            or not settings.DEBUG
            or connection.vendor != "postgresql"
            or host not in {"127.0.0.1", "localhost", "::1"}
            or not database_name.startswith(prefix)
            or confirmed != database_name
            or os.environ.get("ALLOW_FICTITIOUS_DEMO_SEED") != "1"
            or not owner
            or current_user != owner
            or current_user == runtime
            or settings.NOTIFICATION_EXTERNAL_ALLOWED
            or settings.NOTIFICATION_PUSH_ENABLED
            or settings.EMAIL_BACKEND != "django.core.mail.backends.locmem.EmailBackend"
        ):
            raise CommandError(
                "Génération refusée : confirmez une base PostgreSQL fictive locale dédiée, "
                "son nom exact, l'environnement de développement et les transports externes désactivés."
            )

    def _assert_empty_install(self):
        active_managers = User.objects.filter(role=User.Role.ADMIN, is_active=True)
        if active_managers.count() != 1:
            raise CommandError("Créez d'abord un unique compte Gestionnaire local.")
        manager = active_managers.get()
        guard = AccountGuard.objects.filter(pk=1, initialized=True).exists()
        business_rows = [
            Floor.objects.exists(),
            Category.objects.exists(),
            Supplier.objects.exists(),
            Product.objects.exists(),
            StockEntry.objects.exists(),
            InternalRequest.objects.exists(),
            PurchaseOrder.objects.exists(),
            Receipt.objects.exists(),
            User.objects.exclude(pk=manager.pk).exists(),
        ]
        if not guard or manager.must_change_password or any(business_rows):
            raise CommandError(
                "La base n'est pas vierge ou le compte Gestionnaire doit encore changer son mot de passe. "
                "Aucune donnée n'a été modifiée ; utilisez une nouvelle base de démonstration dédiée."
            )
        return manager

    @staticmethod
    def _expect(actual, expected, stage):
        if actual != expected:
            raise CommandError(
                f"Génération interrompue à l'étape {stage} (HTTP {actual}). "
                "Aucune reprise destructive n'est effectuée ; utilisez une nouvelle base dédiée."
            )

    @staticmethod
    def _demo_image():
        buffer = io.BytesIO()
        Image.new("RGB", (160, 120), color=(92, 148, 118)).save(buffer, format="PNG")
        return buffer.getvalue()

    def _assert_complete_seed(self):
        from apps.receiving.models import Receipt as ReceivingReceipt

        codes = {row[0] for row in PRODUCTS}
        products = list(Product.objects.filter(code__in=codes).select_related("stock"))
        expected_users = {row[0] for row in DEMO_USERS}
        actual_users = set(
            User.objects.filter(username__in=expected_users).values_list("username", flat=True)
        )
        request = (
            InternalRequest.objects.filter(
                assistant__username="demo_requester_a", lines__product__code=SEED_CODE
            )
            .distinct()
            .first()
        )
        order = PurchaseOrder.objects.filter(lines__product__code=SEED_CODE).distinct().first()
        receipt_exists = (
            ReceivingReceipt.objects.filter(event__order=order).exists() if order else False
        )
        if (
            len(products) != len(PRODUCTS)
            or actual_users != expected_users
            or Floor.objects.filter(code__in=[row[0] for row in FLOORS]).count() != len(FLOORS)
            or request is None
            or not Delivery.objects.filter(request=request).exists()
            or order is None
            or not receipt_exists
        ):
            raise CommandError(
                "La démonstration est partielle. Aucun effacement n'est tenté ; créez une nouvelle base dédiée."
            )
        for product in products:
            balance = product.stock
            if balance.physical is None or not 0 <= balance.reserved <= balance.physical:
                raise CommandError("Le contrôle de stock de la démonstration a échoué.")
        request_line = RequestLine.objects.filter(request=request, product__code=SEED_CODE).get()
        if (
            request_line.quantity
            != request_line.delivered + request_line.closed + request_line.reservation.remaining
            or (
                request_line.quantity,
                request_line.delivered,
                request_line.closed,
                request_line.reservation.remaining,
            )
            != (3, 2, 0, 1)
        ):
            raise CommandError("Le contrôle des quantités de demande a échoué.")
        order_line = OrderLine.objects.filter(order=order, product__code=SEED_CODE).get()
        if (
            order_line.quantity != order_line.received + order_line.cancelled + order_line.waiting
            or (order_line.quantity, order_line.received, order_line.cancelled, order_line.waiting)
            != (8, 4, 0, 4)
        ):
            raise CommandError("Le contrôle des quantités de commande a échoué.")
        demo_stock = StockBalance.objects.get(product__code=SEED_CODE)
        if (demo_stock.physical, demo_stock.reserved, demo_stock.available) != (22, 1, 21):
            raise CommandError("Le contrôle du solde P/R/D a échoué.")
