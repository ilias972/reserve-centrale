"""M2 réel sur PostgreSQL. Les fixtures d'ancrage ne développent pas M3."""

import hashlib
import io
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.files.uploadhandler import TemporaryFileUploadHandler
from django.db import DatabaseError, IntegrityError, close_old_connections, connection, transaction
from django.test import Client, TransactionTestCase, override_settings
from django.utils import timezone
from PIL import Image

from apps.accounts.models import User
from apps.audit.models import AuditEvent
from apps.catalog.models import (
    Category,
    FloorProduct,
    PhotoAsset,
    Product,
    ProductPhoto,
    ProductSupplier,
    Supplier,
)
from apps.catalog.storage import photo_storage
from apps.core.management.commands.grant_runtime import grant_runtime
from apps.core.models import Operation, OperationIntent
from tests.test_accounts import AccountFixtures


def image_file(*, kind="PNG", size=(80, 60), color="green", name=None):
    stream = io.BytesIO()
    image = Image.new("RGB", size, color)
    if kind == "JPEG":
        exif = image.getexif()
        exif[270] = "Métadonnée fictive à retirer"
        image.save(stream, kind, exif=exif)
    else:
        image.save(stream, kind)
    return SimpleUploadedFile(
        name or {"PNG": "test.png", "JPEG": "test.jpg", "WEBP": "test.webp"}[kind],
        stream.getvalue(),
        content_type={"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}[kind],
    )


class CatalogFixtures(AccountFixtures):
    def setUp(self):
        super().setUp()
        self.photo_dir = TemporaryDirectory()
        stores = deepcopy(settings.STORAGES)
        stores["product_photos"]["OPTIONS"]["location"] = self.photo_dir.name
        self.storage_override = override_settings(STORAGES=stores)
        self.storage_override.enable()

    def tearDown(self):
        self.storage_override.disable()
        self.photo_dir.cleanup()
        super().tearDown()

    def create_product(self, **values):
        response = self.post_json(
            "/api/v1/products",
            {
                "code": "FICTIF-001",
                "name": "Cartons fictifs",
                "unit": "carton",
                "minimum": 2,
                "target": 10,
                **values,
            },
        )
        self.assertEqual(response.status_code, 201, response.content)
        return Product.objects.get(pk=response.json()["resource"]["id"])

    def modify(self, product, **values):
        return self.post_json(
            f"/api/v1/products/{product.pk}",
            {"expected_version": product.version, **values},
            method="patch",
        )

    def rights(self, product, rows, **kwargs):
        return self.post_json(
            f"/api/v1/products/{product.pk}/floor-permissions",
            {"expected_version": product.version, "permissions": rows},
            method="put",
            **kwargs,
        )

    def upload(self, product, upload=None, *, identifier=None, key=None, version=None, client=None):
        return (client or self.admin_client).post(
            f"/api/v1/products/{product.pk}/photos",
            {
                "expected_version": product.version if version is None else version,
                "photo": upload or image_file(),
            },
            headers={
                "Operation-Intent": str(identifier or uuid.uuid4()),
                "Idempotency-Key": key or uuid.uuid4().hex,
            },
        )

    def install_inventory_contract_fixture(self, product, quantity=0):
        """Depuis M3, le contrat est exercé par un véritable inventaire audité."""
        response = self.post_json(
            "/api/v1/inventories/initial",
            {
                "lines": [
                    {
                        "product_id": str(product.pk),
                        "expected_stock_version": product.stock.version,
                        "expected_product_version": product.version,
                        "counted_quantity": quantity,
                    }
                ]
            },
            self.logged(self.replenisher),
        )
        self.assertEqual(response.status_code, 201, response.content)
        product.refresh_from_db()
        return Operation.objects.get(pk=response.json()["operation_id"])


class CatalogTests(CatalogFixtures, TransactionTestCase):
    def test_create_edit_unique_product_and_audit(self):
        category = Category.objects.create(name="Fournitures fictives")
        product = self.create_product(category_id=str(category.pk))
        response = self.modify(product, name="Carton fictif renommé", minimum=3, target=12)
        self.assertEqual(response.status_code, 200, response.content)
        product.refresh_from_db()
        self.assertEqual(
            (product.name, product.minimum, product.target, product.version),
            ("Carton fictif renommé", 3, 12, 1),
        )
        history = self.admin_client.get(f"/api/v1/products/{product.pk}/history").json()["history"]
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["before"]["name"], "Cartons fictifs")
        self.assertEqual(history[0]["after"]["name"], product.name)
        duplicate = self.post_json(
            "/api/v1/products",
            {"code": "fictif-001", "name": "Doublon", "unit": "boîte", "minimum": 0, "target": 0},
        )
        self.assertEqual(duplicate.status_code, 409)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Product.objects.create(
                code="fictif-001", name="Doublon SQL", unit="boîte", minimum=0, target=0
            )

    def test_not_inventoried_has_unknown_quantities_and_no_stock_edit_route(self):
        product = self.create_product()
        result = self.admin_client.get(f"/api/v1/products/{product.pk}").json()["product"]
        self.assertEqual(result["status"], "NOT_COUNTED")
        self.assertIsNone(result["physical_quantity"])
        self.assertIsNone(result["available_quantity"])
        self.assertTrue(result["stock_module_available"])
        self.assertFalse(result["demandable"])
        self.assertContains(
            self.admin_client.get(f"/catalogue/{product.pk}"), "Non inventorié — quantité inconnue"
        )
        for field in [
            "physical_quantity",
            "stock",
            "counted_at",
            "initial_inventory_operation_id",
            "status",
        ]:
            self.assertEqual(self.modify(product, **{field: 0}).status_code, 400)
        for route in ["/api/v1/inventories/initial", "/api/v1/stocks/adjustments"]:
            self.assertEqual(self.post_json(route, {}).status_code, 403)
        self.assertEqual(self.post_json("/api/v1/requests", {}).status_code, 403)

    def test_single_unit_edit_before_inventory_and_sql_lock_with_zero_anchor_fixture(self):
        product = self.create_product()
        self.assertEqual(self.modify(product, unit="boîte").status_code, 200)
        product.refresh_from_db()
        operation = self.install_inventory_contract_fixture(product, quantity=0)
        self.assertEqual(
            AuditEvent.objects.get(operation=operation).details[0]["counted_quantity"], 0
        )
        self.assertEqual(self.modify(product, unit="pièce").status_code, 409)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Product.objects.filter(pk=product.pk).update(unit="pièce")
        with self.assertRaises(IntegrityError), transaction.atomic():
            Product.objects.filter(pk=product.pk).update(
                counted_at=None, initial_inventory_operation=None, status="NOT_COUNTED"
            )
        product.refresh_from_db()
        self.assertEqual(product.unit, "boîte")
        self.assertEqual(self.modify(product, unit="boîtes / pièces").status_code, 400)

    def test_activation_requires_inventory_photo_and_explicit_admin_verification(self):
        product = self.create_product()
        path = f"/api/v1/products/{product.pk}/activate"
        body = {
            "expected_version": product.version,
            "inventory_verified": True,
            "photo_verified": True,
        }
        response = self.post_json(path, body)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "INVENTORY_REQUIRED")
        self.install_inventory_contract_fixture(product)
        body["expected_version"] = product.version
        self.assertEqual(self.post_json(path, body).status_code, 400)
        self.assertEqual(self.upload(product).status_code, 200)
        product.refresh_from_db()
        body["expected_version"] = product.version
        self.assertEqual(
            self.post_json(path, {**body, "inventory_verified": False}).status_code, 400
        )
        self.assertEqual(self.post_json(path, {**body, "photo_verified": False}).status_code, 400)
        for user in [self.assistant, self.replenisher]:
            self.assertEqual(self.post_json(path, body, self.logged(user)).status_code, 403)

    def test_activation_without_any_supplier_against_inventory_contract_fixture(self):
        product = self.create_product()
        self.assertEqual(self.upload(product).status_code, 200)
        product.refresh_from_db()
        self.install_inventory_contract_fixture(product)
        self.assertFalse(ProductSupplier.objects.exists())
        response = self.post_json(
            f"/api/v1/products/{product.pk}/activate",
            {
                "expected_version": product.version,
                "inventory_verified": True,
                "photo_verified": True,
            },
        )
        self.assertEqual(response.status_code, 200, response.content)
        product.refresh_from_db()
        self.assertEqual(product.status, "ACTIVE")
        self.assertFalse(response.json()["resource"]["supplier_configured"])
        self.assertFalse(ProductSupplier.objects.exists())

    def test_sql_rejects_active_product_without_confirmed_inventory_or_activation(self):
        product = self.create_product()
        with self.assertRaises(IntegrityError), transaction.atomic():
            Product.objects.filter(pk=product.pk).update(status="ACTIVE")
        wrong = Operation.objects.get(command_type="catalog.product.create")
        with self.assertRaises(IntegrityError), transaction.atomic():
            Product.objects.filter(pk=product.pk).update(
                status="COUNTED_PENDING_ACTIVATION",
                counted_at=timezone.now(),
                initial_inventory_operation=wrong,
            )

    def test_archive_preserves_identity_rights_photos_and_parameter_history(self):
        product = self.create_product()
        self.upload(product)
        product.refresh_from_db()
        self.rights(
            product,
            [{"floor_id": str(self.floor.pk), "visible": True, "demandable": True, "maximum": 5}],
        )
        product.refresh_from_db()
        old_ids = list(product.floor_permissions.values_list("pk", flat=True))
        old_photos = list(product.photos.values_list("pk", flat=True))
        old_audits = list(AuditEvent.objects.values_list("pk", flat=True))
        response = self.post_json(
            f"/api/v1/products/{product.pk}/archive",
            {"expected_version": product.version, "confirmed": True},
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(Product.objects.filter(pk=product.pk, status="ARCHIVED").exists())
        self.assertEqual(list(product.floor_permissions.values_list("pk", flat=True)), old_ids)
        self.assertEqual(list(product.photos.values_list("pk", flat=True)), old_photos)
        self.assertEqual(AuditEvent.objects.filter(pk__in=old_audits).count(), len(old_audits))
        self.assertEqual(self.user_client.get(f"/api/v1/catalog/{product.pk}").status_code, 404)
        self.assertEqual(
            self.logged(self.replenisher).get(f"/api/v1/catalog/{product.pk}").status_code, 404
        )
        self.assertEqual(self.admin_client.get(f"/api/v1/products/{product.pk}").status_code, 200)

    def test_floor_rights_differ_and_missing_invalid_ceiling_is_never_unlimited(self):
        product = self.create_product()
        rows = [
            {"floor_id": str(self.floor.pk), "visible": True, "demandable": True, "maximum": 4},
            {
                "floor_id": str(self.other_floor.pk),
                "visible": False,
                "demandable": False,
                "maximum": None,
            },
        ]
        response = self.rights(product, rows)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self.user_client.get(f"/api/v1/catalog/{product.pk}").status_code, 200)
        other = User.objects.create_user(
            "autre-demandeur-a-fictif",
            role="ASSISTANT",
            floor=self.other_floor,
            must_change_password=False,
        )
        self.assertEqual(self.logged(other).get(f"/api/v1/catalog/{product.pk}").status_code, 404)
        product.refresh_from_db()
        for ceiling in [None, 0, -1, True, 1.5, "10"]:
            response = self.rights(
                product,
                [
                    {
                        "floor_id": str(self.floor.pk),
                        "visible": True,
                        "demandable": True,
                        "maximum": ceiling,
                    }
                ],
            )
            self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(
            self.rights(
                product,
                [
                    {
                        "floor_id": str(self.floor.pk),
                        "visible": True,
                        "demandable": False,
                        "maximum": None,
                    }
                ],
            ).status_code,
            200,
        )
        data = self.user_client.get(f"/api/v1/catalog/{product.pk}").json()["product"]
        self.assertFalse(data["permission_allows_request"])
        self.assertFalse(data["demandable"])
        self.assertIsNone(data["maximum"])
        with self.assertRaises(IntegrityError), transaction.atomic():
            FloorProduct.objects.filter(product=product).update(demandable=True, maximum=None)

    def test_assistant_catalog_has_no_supplier_price_threshold_or_unauthorized_photo(self):
        product = self.create_product()
        self.upload(product)
        product.refresh_from_db()
        self.assertEqual(
            self.user_client.get(f"/api/v1/products/{product.pk}/photo").status_code, 404
        )
        self.rights(
            product,
            [
                {
                    "floor_id": str(self.floor.pk),
                    "visible": True,
                    "demandable": False,
                    "maximum": None,
                }
            ],
        )
        response = self.user_client.get(f"/api/v1/catalog/{product.pk}")
        self.assertEqual(response.status_code, 200)
        for key in ["minimum", "target", "supplier_configured", "suppliers", "version"]:
            self.assertNotIn(key, response.json()["product"])
        photo = self.user_client.get(f"/api/v1/products/{product.pk}/photo")
        self.assertEqual(photo.status_code, 200)
        self.assertEqual(photo["Cache-Control"], "no-store")
        photo.close()

    def test_rights_changes_preserve_link_identity_and_immutable_previous_audit(self):
        product = self.create_product()
        self.rights(
            product,
            [{"floor_id": str(self.floor.pk), "visible": True, "demandable": True, "maximum": 3}],
        )
        old = FloorProduct.objects.get(product=product, floor=self.floor)
        audit = AuditEvent.objects.get(operation__command_type="catalog.product.permissions")
        snapshot = deepcopy(audit.after)
        product.refresh_from_db()
        self.assertEqual(self.rights(product, []).status_code, 200)
        old.refresh_from_db()
        self.assertFalse(old.visible)
        self.assertFalse(old.demandable)
        audit.refresh_from_db()
        self.assertEqual(audit.after, snapshot)
        with self.assertRaises(IntegrityError), transaction.atomic():
            AuditEvent.objects.filter(pk=audit.pk).update(after={})

    def test_admin_manages_floors_categories_suppliers_without_deleting_references(self):
        for resource, payload in [
            ("floors", {"code": "NOUVEAU-FICTIF", "name": "Unité fictif neuf"}),
            ("categories", {"name": "Catégorie fictive"}),
            (
                "suppliers",
                {
                    "code": "FOUR-FICTIF",
                    "name": "Fournisseur fictif",
                    "website": "https://example.com",
                },
            ),
        ]:
            created = self.post_json(f"/api/v1/{resource}", payload)
            self.assertEqual(created.status_code, 201, created.content)
            identifier = created.json()["resource"]["id"]
            modified = self.post_json(
                f"/api/v1/{resource}/{identifier}",
                {"expected_version": 0, "name": "Nom fictif modifié", "active": False},
                method="patch",
            )
            self.assertEqual(modified.status_code, 200, modified.content)
            self.assertFalse(modified.json()["resource"]["active"])
            self.assertEqual(
                self.admin_client.delete(f"/api/v1/{resource}/{identifier}").status_code, 405
            )

    def test_floor_deactivation_preserves_assignment_and_denies_new_assignment_and_catalog(self):
        product = self.create_product()
        self.rights(
            product,
            [{"floor_id": str(self.floor.pk), "visible": True, "demandable": True, "maximum": 5}],
        )
        response = self.post_json(
            f"/api/v1/floors/{self.floor.pk}",
            {"expected_version": 0, "active": False},
            method="patch",
        )
        self.assertEqual(response.status_code, 200)
        self.assistant.refresh_from_db()
        self.assertEqual(self.assistant.floor_id, self.floor.pk)
        self.assertEqual(self.user_client.get("/api/v1/catalog").json()["products"], [])
        self.assertEqual(self.user_client.get(f"/api/v1/catalog/{product.pk}").status_code, 404)
        self.assertEqual(
            self.post_json(
                "/api/v1/users",
                {
                    "username": "nouvelle-fictive",
                    "display_name": "Nouvelle fictive",
                    "role": "ASSISTANT",
                    "floor_id": str(self.floor.pk),
                },
            ).status_code,
            400,
        )
        self.assertTrue(FloorProduct.objects.filter(floor=self.floor, product=product).exists())

    def test_supplier_multiple_links_optional_preferred_price_reference_and_replacement(self):
        product = self.create_product()
        first = Supplier.objects.create(code="FOUR-A", name="Fournisseur fictif A")
        second = Supplier.objects.create(code="FOUR-B", name="Fournisseur fictif B")
        body = {
            "expected_version": 0,
            "suppliers": [
                {
                    "supplier_id": str(first.pk),
                    "preferred": True,
                    "reference": "REF-FICTIVE",
                    "website": "https://example.com/produit",
                    "indicative_price": "12.30",
                    "currency": "EUR",
                },
                {"supplier_id": str(second.pk), "preferred": False},
            ],
        }
        response = self.post_json(
            f"/api/v1/products/{product.pk}/supplier-links", body, method="put"
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(ProductSupplier.objects.filter(product=product, active=True).count(), 2)
        self.assertEqual(
            ProductSupplier.objects.get(product=product, preferred=True).supplier_id, first.pk
        )
        product.refresh_from_db()
        body["expected_version"] = product.version
        body["suppliers"][1]["preferred"] = True
        self.assertEqual(
            self.post_json(
                f"/api/v1/products/{product.pk}/supplier-links", body, method="put"
            ).status_code,
            400,
        )
        result = self.post_json(
            f"/api/v1/products/{product.pk}/supplier-links",
            {"expected_version": product.version, "suppliers": []},
            method="put",
        )
        self.assertEqual(result.status_code, 200)
        self.assertEqual(ProductSupplier.objects.filter(product=product).count(), 2)
        self.assertFalse(ProductSupplier.objects.filter(product=product, active=True).exists())
        self.assertFalse(Operation.objects.filter(command_type__contains="purchase").exists())

    def test_unsafe_supplier_url_price_and_new_inactive_link_rejected(self):
        product = self.create_product()
        supplier = Supplier.objects.create(code="FOUR-FICTIF", name="Fournisseur fictif")
        for value in [
            "javascript:alert(1)",
            "data:text/html,test",
            "https://name:password@example.com",
        ]:
            self.assertEqual(
                self.post_json(
                    "/api/v1/suppliers", {"code": "DANGER", "name": "Fictif", "website": value}
                ).status_code,
                400,
            )
        for price, currency in [
            ("NaN", "EUR"),
            ("-1", "EUR"),
            ("0.001", "EUR"),
            ("1", ""),
            (1.2, "EUR"),
        ]:
            response = self.post_json(
                f"/api/v1/products/{product.pk}/supplier-links",
                {
                    "expected_version": 0,
                    "suppliers": [
                        {
                            "supplier_id": str(supplier.pk),
                            "indicative_price": price,
                            "currency": currency,
                        }
                    ],
                },
                method="put",
            )
            self.assertEqual(response.status_code, 400, response.content)
        supplier.active = False
        supplier.save()
        self.assertEqual(
            self.post_json(
                f"/api/v1/products/{product.pk}/supplier-links",
                {"expected_version": 0, "suppliers": [{"supplier_id": str(supplier.pk)}]},
                method="put",
            ).status_code,
            400,
        )

    def test_no_ordinary_role_can_configure_any_m2_route(self):
        product = self.create_product()
        for user in [self.assistant, self.replenisher]:
            client = self.logged(user)
            for path in [
                "/api/v1/products",
                "/api/v1/categories",
                "/api/v1/floors",
                "/api/v1/suppliers",
                "/configuration/floor",
                "/catalogue/creer",
                f"/catalogue/{product.pk}/modifier",
                f"/catalogue/{product.pk}/droits",
                f"/catalogue/{product.pk}/fournisseurs",
                f"/api/v1/products/{product.pk}/history",
            ]:
                self.assertEqual(
                    client.get(path).status_code, 403, (path, client.get(path).content)
                )
            for suffix, method, body in [
                ("", "patch", {"expected_version": 0, "name": "Interdit"}),
                ("/archive", "post", {"expected_version": 0, "confirmed": True}),
                ("/floor-permissions", "put", {"expected_version": 0, "permissions": []}),
                ("/supplier-links", "put", {"expected_version": 0, "suppliers": []}),
            ]:
                self.assertEqual(
                    self.post_json(
                        f"/api/v1/products/{product.pk}" + suffix, body, client, method=method
                    ).status_code,
                    403,
                )
            self.assertEqual(self.upload(product, client=client).status_code, 403)
        self.assertFalse(PhotoAsset.objects.exists())

    def test_concurrent_edits_use_versions_and_one_confirmed_operation(self):
        from tests.catalog_evidence import observations

        product = self.create_product()
        barrier = threading.Barrier(2)
        cookie = self.admin_client.cookies[settings.SESSION_COOKIE_NAME].value

        def worker(name):
            close_old_connections()
            try:
                client = Client(raise_request_exception=False)
                client.cookies[settings.SESSION_COOKIE_NAME] = cookie
                barrier.wait(timeout=5)
                response = self.post_json(
                    f"/api/v1/products/{product.pk}",
                    {"expected_version": 0, "name": name},
                    client,
                    method="patch",
                )
                return response.status_code
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = [
                future.result(timeout=15)
                for future in [
                    pool.submit(worker, name) for name in ["Premier fictif", "Second fictif"]
                ]
            ]
        self.assertEqual(sorted(results), [200, 409])
        self.assertEqual(Operation.objects.filter(command_type="catalog.product.update").count(), 1)
        observations["concurrent_product_edits"] = {
            "statuses": sorted(results),
            "confirmed_updates": 1,
        }

    def test_different_idempotency_payload_conflicts_and_lost_response_replays(self):
        identifier, key = uuid.uuid4(), uuid.uuid4().hex
        payload = {
            "code": "PERDU-FICTIF",
            "name": "Réponse perdue fictive",
            "unit": "pièce",
            "minimum": 0,
            "target": 0,
        }
        first = self.post_json("/api/v1/products", payload, identifier=identifier, key=key)
        replay = self.post_json("/api/v1/products", payload, identifier=identifier, key=key)
        self.assertEqual(first.status_code, 201)
        self.assertEqual(replay.json()["operation_id"], first.json()["operation_id"])
        self.assertEqual(Product.objects.count(), 1)
        self.assertEqual(
            self.post_json(
                "/api/v1/products", {**payload, "unit": "carton"}, identifier=identifier, key=key
            ).status_code,
            409,
        )

    def test_outbox_failure_rolls_back_product_and_rights_changes(self):
        with patch(
            "apps.core.idempotency.Outbox.objects.create", side_effect=RuntimeError("panne fictive")
        ):
            response = self.post_json(
                "/api/v1/products",
                {"code": "ECHEC", "name": "Fictif", "unit": "carton", "minimum": 0, "target": 0},
            )
        self.assertEqual(response.status_code, 500)
        self.assertFalse(Product.objects.exists())
        self.assertFalse(Operation.objects.exists())
        self.assertEqual(OperationIntent.objects.get().state, "READY")

    def test_runtime_can_manage_catalog_but_not_write_inventory_anchor_or_delete_history(self):
        product = self.create_product()
        grant_runtime(os.environ["RUNTIME_DATABASE_USER"])
        original = connection.settings_dict.copy()
        connection.close()
        connection.settings_dict.update(
            USER=os.environ["RUNTIME_DATABASE_USER"],
            PASSWORD=os.environ["RUNTIME_DATABASE_PASSWORD"],
        )
        try:
            self.assertEqual(self.modify(product, name="Fictif runtime").status_code, 200)
            product.refresh_from_db()
            self.assertEqual(self.upload(product).status_code, 200)
            for statement in [
                "UPDATE catalog_product SET counted_at=now()",
                "DELETE FROM catalog_product",
                "DELETE FROM catalog_productphoto",
                "UPDATE catalog_photoasset SET object_key='forged'",
            ]:
                with (
                    self.assertRaises(DatabaseError),
                    transaction.atomic(),
                    connection.cursor() as cursor,
                ):
                    cursor.execute(statement)
        finally:
            connection.close()
            connection.settings_dict.clear()
            connection.settings_dict.update(original)


class PhotographTests(CatalogFixtures, TransactionTestCase):
    def test_valid_formats_are_reencoded_without_exif_or_original_name(self):
        product = self.create_product()
        for kind in ["JPEG", "PNG", "WEBP"]:
            upload = image_file(kind=kind)
            response = self.upload(product, upload)
            self.assertEqual(response.status_code, 200, response.content)
            product.refresh_from_db()
            asset = product.current_photo.asset
            self.assertNotIn("test", asset.object_key)
            with photo_storage().open(asset.object_key, "rb") as stream:
                encoded = stream.read()
            self.assertEqual(hashlib.sha256(encoded).hexdigest(), asset.digest)
            with Image.open(io.BytesIO(encoded)) as image:
                self.assertEqual(image.format, "JPEG")
                self.assertEqual(len(image.getexif()), 0)
                self.assertNotIn("icc_profile", image.info)
            self.assertNotIn("Métadonnée fictive".encode(), encoded)
            with (
                photo_storage().open(asset.thumbnail_key, "rb") as stream,
                Image.open(stream) as thumb,
            ):
                self.assertLessEqual(max(thumb.size), 240)
        self.assertEqual(product.photos.count(), 3)
        self.assertEqual(PhotoAsset.objects.count(), 3)

    def test_invalid_or_malicious_files_mismatched_mime_and_animation_rejected(self):
        product = self.create_product()
        cases = [
            SimpleUploadedFile(
                "malveillant.svg", b'<svg onload="alert(1)"></svg>', content_type="image/svg+xml"
            ),
            SimpleUploadedFile(
                "html.jpg", b"<html><script>alert(1)</script></html>", content_type="image/jpeg"
            ),
            SimpleUploadedFile("zip.png", b"PK\x03\x04fake", content_type="image/png"),
            SimpleUploadedFile("truncated.jpg", b"\xff\xd8\xff\xe0test", content_type="image/jpeg"),
        ]
        mismatch = image_file()
        mismatch.content_type = "image/jpeg"
        cases.append(mismatch)
        animated = io.BytesIO()
        Image.new("RGB", (20, 20), "red").save(
            animated,
            "WEBP",
            save_all=True,
            append_images=[Image.new("RGB", (20, 20), "blue")],
            duration=100,
        )
        cases.append(
            SimpleUploadedFile("anime.webp", animated.getvalue(), content_type="image/webp")
        )
        for upload in cases:
            response = self.upload(product, upload)
            self.assertEqual(response.status_code, 400, response.content)
        self.assertFalse(PhotoAsset.objects.exists())
        self.assertFalse(ProductPhoto.objects.exists())

    def test_size_pixels_and_side_limits_are_enforced_before_association(self):
        product = self.create_product()
        for override, expected_status in [
            ({"PHOTO_MAX_BYTES": 10}, 413),
            ({"PHOTO_MAX_PIXELS": 100}, 400),
            ({"PHOTO_MAX_DIMENSION": 32}, 400),
        ]:
            with override_settings(**override):
                self.assertEqual(self.upload(product, image_file()).status_code, expected_status)
        self.assertFalse(PhotoAsset.objects.exists())

    def test_decodable_trailing_payload_is_discarded_not_served(self):
        product = self.create_product()
        upload = image_file(kind="JPEG")
        marker = b"<script>fictive-trailing-payload</script>"
        upload = SimpleUploadedFile("photo.jpg", upload.read() + marker, content_type="image/jpeg")
        self.assertEqual(self.upload(product, upload).status_code, 200)
        response = self.admin_client.get(f"/api/v1/products/{product.pk}/photo")
        data = b"".join(response.streaming_content)
        response.close()
        self.assertNotIn(marker, data)
        self.assertEqual(response["Content-Type"], "image/jpeg")
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")

    def test_private_current_and_historical_photo_permissions_are_rechecked(self):
        product = self.create_product()
        self.upload(product)
        product.refresh_from_db()
        first = product.current_photo_id
        self.rights(
            product,
            [
                {
                    "floor_id": str(self.floor.pk),
                    "visible": True,
                    "demandable": False,
                    "maximum": None,
                }
            ],
        )
        product.refresh_from_db()
        self.upload(product, image_file(color="blue"))
        product.refresh_from_db()
        path = f"/api/v1/products/{product.pk}/photo"
        anonymous = Client()
        self.assertEqual(anonymous.get(path).status_code, 401)
        self.assertEqual(self.user_client.get(path + f"?version={first}").status_code, 404)
        current = self.user_client.get(path)
        self.assertEqual(current.status_code, 200)
        current.close()
        old = self.admin_client.get(path + f"?version={first}")
        self.assertEqual(old.status_code, 200)
        old.close()
        asset = product.current_photo.asset
        for prefix in ["/media/", "/.local/private/photos/", "/static/"]:
            self.assertEqual(anonymous.get(prefix + asset.object_key).status_code, 404)
        with self.assertRaises(ValueError):
            photo_storage().url(asset.object_key)
        self.rights(product, [])
        self.assertEqual(self.user_client.get(path).status_code, 404)
        self.assertEqual(ProductPhoto.objects.get(pk=product.current_photo_id).replaces_id, first)

    def test_cross_product_photo_and_history_references_are_refused(self):
        product = self.create_product()
        other = self.create_product(code="FICTIF-002", name="Autre fictif")
        self.upload(product)
        product.refresh_from_db()
        self.assertEqual(
            self.admin_client.get(
                f"/api/v1/products/{other.pk}/photo?version={product.current_photo_id}"
            ).status_code,
            404,
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            Product.objects.filter(pk=other.pk).update(current_photo_id=product.current_photo_id)
        with self.assertRaises(IntegrityError), transaction.atomic():
            ProductPhoto.objects.filter(pk=product.current_photo_id).update(product=other)

    def test_double_upload_same_intention_keeps_one_association_and_file_pair(self):
        product = self.create_product()
        identifier, key = uuid.uuid4(), uuid.uuid4().hex
        first = self.upload(product, identifier=identifier, key=key)
        self.assertEqual(first.status_code, 200, first.content)
        repeat = self.upload(product, identifier=identifier, key=key)
        self.assertEqual(repeat.status_code, 200, repeat.content)
        self.assertEqual(first.json()["operation_id"], repeat.json()["operation_id"])
        self.assertEqual(PhotoAsset.objects.count(), 1)
        self.assertEqual(ProductPhoto.objects.count(), 1)
        self.assertEqual(
            len(list(__import__("pathlib").Path(self.photo_dir.name).rglob("*.jpg"))), 2
        )
        conflict = self.upload(product, image_file(color="blue"), identifier=identifier, key=key)
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(PhotoAsset.objects.count(), 1)

    def test_database_failure_does_not_publish_staged_file_and_retry_is_safe(self):
        product = self.create_product()
        identifier, key = uuid.uuid4(), uuid.uuid4().hex
        with patch(
            "apps.core.idempotency.Outbox.objects.create", side_effect=RuntimeError("panne fictive")
        ):
            response = self.upload(product, identifier=identifier, key=key)
        self.assertEqual(response.status_code, 500)
        product.refresh_from_db()
        self.assertIsNone(product.current_photo_id)
        self.assertFalse(ProductPhoto.objects.exists())
        self.assertEqual(PhotoAsset.objects.count(), 1)
        self.assertEqual(
            self.admin_client.get(f"/api/v1/products/{product.pk}/photo").status_code, 404
        )
        retried = self.upload(product, identifier=identifier, key=key)
        self.assertEqual(retried.status_code, 200, retried.content)
        self.assertEqual(PhotoAsset.objects.count(), 1)
        self.assertEqual(ProductPhoto.objects.count(), 1)

    def test_storage_failure_compensates_only_new_files(self):
        product = self.create_product()
        storage = photo_storage()
        original_save = storage.save
        call_count = 0

        def failing_save(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise OSError("panne fictive stockage")
            return original_save(*args, **kwargs)

        with patch.object(storage, "save", side_effect=failing_save):
            response = self.upload(product)
        self.assertEqual(response.status_code, 500)
        self.assertFalse(PhotoAsset.objects.exists())
        self.assertEqual(list(__import__("pathlib").Path(self.photo_dir.name).rglob("*.jpg")), [])

    def test_upload_csrf_and_password_change_restrictions_apply_before_decoding(self):
        product = self.create_product()
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.admin)
        self.assertEqual(self.upload(product, client=client).status_code, 403)
        User.objects.filter(pk=self.admin.pk).update(must_change_password=True)
        self.assertEqual(self.upload(product).status_code, 403)
        self.assertFalse(PhotoAsset.objects.exists())

    def test_anonymous_oversize_uploads_are_rejected_before_csrf_parsing(self):
        product = self.create_product()
        client = Client(enforce_csrf_checks=True)
        client.get("/connexion")
        csrf_token = client.cookies["csrftoken"].value
        oversized = b"x" * (settings.PHOTO_MAX_REQUEST_BYTES + 1)
        paths = [
            f"/api/v1/products/{product.pk}/photos",
            f"/catalogue/{product.pk}/photographie",
        ]
        with patch.object(
            TemporaryFileUploadHandler, "receive_data_chunk", autospec=True
        ) as receive_chunk:
            for path in paths:
                response = client.post(
                    path,
                    {
                        "expected_version": "1",
                        "photo": SimpleUploadedFile(
                            "large.jpg", oversized, content_type="image/jpeg"
                        ),
                    },
                    HTTP_X_CSRFTOKEN=csrf_token,
                )
                self.assertEqual(response.status_code, 413)
            receive_chunk.assert_not_called()
        self.assertFalse(PhotoAsset.objects.exists())

    def test_authenticated_file_over_photo_limit_is_stopped_during_streaming(self):
        product = self.create_product()
        client = Client()
        client.force_login(self.admin)
        response = client.post(
            f"/api/v1/products/{product.pk}/photos",
            {
                "expected_version": str(product.version),
                "photo": SimpleUploadedFile(
                    "large.jpg",
                    b"x" * (settings.PHOTO_MAX_BYTES + 1),
                    content_type="image/jpeg",
                ),
            },
        )
        self.assertEqual(response.status_code, 413, response.content)
        product.refresh_from_db()
        self.assertIsNone(product.current_photo_id)
        self.assertFalse(PhotoAsset.objects.exists())


class CatalogAdditionalTests(CatalogFixtures, TransactionTestCase):
    def test_inventory_anchor_cannot_be_reused_for_another_product(self):
        first = self.create_product()
        other = self.create_product(code="AUTRE-FICTIF")
        operation = self.install_inventory_contract_fixture(first)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Product.objects.filter(pk=other.pk).update(
                counted_at=timezone.now(),
                initial_inventory_operation=operation,
                status="COUNTED_PENDING_ACTIVATION",
            )

    def test_runtime_cannot_insert_a_precounted_product(self):
        product = self.create_product()
        operation = self.install_inventory_contract_fixture(product)
        grant_runtime(os.environ["RUNTIME_DATABASE_USER"])
        original = connection.settings_dict.copy()
        connection.close()
        connection.settings_dict.update(
            USER=os.environ["RUNTIME_DATABASE_USER"],
            PASSWORD=os.environ["RUNTIME_DATABASE_PASSWORD"],
        )
        try:
            with (
                self.assertRaisesMessage(
                    IntegrityError, "Runtime M2 cannot insert an inventory anchor"
                ),
                transaction.atomic(),
            ):
                Product.objects.create(
                    code="INTERDIT-FICTIF",
                    name="Fictif",
                    unit="carton",
                    minimum=0,
                    target=0,
                    counted_at=timezone.now(),
                    initial_inventory_operation=operation,
                    status="COUNTED_PENDING_ACTIVATION",
                )
        finally:
            connection.close()
            connection.settings_dict.clear()
            connection.settings_dict.update(original)

    def test_floor_supplier_snapshots_do_not_change_when_references_are_renamed(self):
        product = self.create_product()
        self.rights(
            product,
            [{"floor_id": str(self.floor.pk), "visible": True, "demandable": True, "maximum": 2}],
        )
        product.refresh_from_db()
        supplier = Supplier.objects.create(
            code="FOUR-FICTIF", name="Nom fournisseur initial fictif"
        )
        self.post_json(
            f"/api/v1/products/{product.pk}/supplier-links",
            {"expected_version": product.version, "suppliers": [{"supplier_id": str(supplier.pk)}]},
            method="put",
        )
        self.post_json(
            f"/api/v1/floors/{self.floor.pk}",
            {"expected_version": 0, "name": "Nom unité modifié fictif"},
            method="patch",
        )
        self.post_json(
            f"/api/v1/suppliers/{supplier.pk}",
            {"expected_version": 0, "name": "Nom fournisseur modifié fictif"},
            method="patch",
        )
        right_audit = AuditEvent.objects.get(operation__command_type="catalog.product.permissions")
        supplier_audit = AuditEvent.objects.get(operation__command_type="catalog.product.suppliers")
        self.assertEqual(right_audit.after["permissions"][0]["floor_name"], "Unité fictif A")
        self.assertEqual(
            supplier_audit.after["suppliers"][0]["supplier_name"], "Nom fournisseur initial fictif"
        )
        page = self.admin_client.get(f"/catalogue/{product.pk}/historique")
        self.assertContains(page, "Nom fournisseur initial fictif")
        self.assertNotContains(page, "Nom fournisseur modifié fictif")
        self.assertNotContains(page, "&#x27;floor_id&#x27;")

    def test_concurrent_photo_upload_same_intention_confirms_once(self):
        from tests.catalog_evidence import observations

        product = self.create_product()
        identifier, key = uuid.uuid4(), uuid.uuid4().hex
        cookie = self.admin_client.cookies[settings.SESSION_COOKIE_NAME].value
        barrier = threading.Barrier(2)

        def worker():
            close_old_connections()
            try:
                client = Client(raise_request_exception=False)
                client.cookies[settings.SESSION_COOKIE_NAME] = cookie
                barrier.wait(timeout=5)
                response = self.upload(product, identifier=identifier, key=key, client=client)
                return response.status_code, response.json()
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = [f.result(timeout=15) for f in [pool.submit(worker), pool.submit(worker)]]
        self.assertEqual([status for status, _ in results], [200, 200])
        self.assertEqual(len({body["operation_id"] for _, body in results}), 1)
        self.assertEqual(PhotoAsset.objects.count(), 1)
        self.assertEqual(ProductPhoto.objects.count(), 1)
        observations["concurrent_same_photo_intention"] = {
            "requests": 2,
            "associations": 1,
            "asset_pairs": 1,
        }

    def test_gc_dry_run_then_explicit_cleanup_preserves_confirmed_and_ready_assets(self):
        from datetime import timedelta

        from django.core.files.base import ContentFile
        from django.core.management import call_command

        from apps.core.idempotency import resolve_intent
        from apps.core.policies import ActorContext

        confirmed = self.create_product()
        self.upload(confirmed)
        confirmed.refresh_from_db()
        confirmed_key = confirmed.current_photo.asset.object_key
        abandoned = self.create_product(code="ABANDON-FICTIF")
        pending = self.create_product(code="PENDING-FICTIF")
        for product in [abandoned, pending]:
            with patch(
                "apps.core.idempotency.Outbox.objects.create",
                side_effect=RuntimeError("panne fictive"),
            ):
                self.assertEqual(self.upload(product).status_code, 500)
        asset = PhotoAsset.objects.get(product=abandoned)
        resolve_intent(ActorContext(self.admin.pk, self.admin.auth_version), asset.intent_id)
        orphan = photo_storage().save(
            f"objects/{uuid.uuid4().hex}.jpg", ContentFile(b"orphelin technique fictif")
        )
        future = timezone.now() + timedelta(days=2)
        output = io.StringIO()
        with patch(
            "apps.catalog.management.commands.cleanup_photo_uploads.timezone.now",
            return_value=future,
        ):
            call_command("cleanup_photo_uploads", stdout=output)
            self.assertTrue(PhotoAsset.objects.filter(pk=asset.pk).exists())
            self.assertTrue(photo_storage().exists(orphan))
            call_command(
                "cleanup_photo_uploads",
                execute=True,
                confirm_local_test=True,
                stdout=io.StringIO(),
            )
        self.assertIn("Simulation", output.getvalue())
        self.assertFalse(PhotoAsset.objects.filter(pk=asset.pk).exists())
        self.assertFalse(photo_storage().exists(orphan))
        self.assertTrue(photo_storage().exists(confirmed_key))
        self.assertTrue(PhotoAsset.objects.filter(product=pending).exists())
        self.assertTrue(ProductPhoto.objects.filter(product=confirmed).exists())
        self.assertTrue(
            OperationIntent.objects.filter(pk=asset.intent_id, state="EXPIRED").exists()
        )

    def test_unknown_configuration_and_malformed_form_identifiers_never_return_500(self):
        self.assertEqual(self.admin_client.get("/configuration/inconnu").status_code, 404)
        product = self.create_product()
        formdata = {
            "operation_id": str(uuid.uuid4()),
            "operation_key": uuid.uuid4().hex,
            "expected_version": 0,
            "rights-TOTAL_FORMS": 1,
            "rights-INITIAL_FORMS": 1,
            "rights-MIN_NUM_FORMS": 0,
            "rights-MAX_NUM_FORMS": 1000,
            "rights-0-floor_id": "not-a-uuid",
            "rights-0-visible": "on",
        }
        response = self.admin_client.post(f"/catalogue/{product.pk}/droits", formdata)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertFalse(FloorProduct.objects.exists())
        product.refresh_from_db()
        self.assertEqual(product.version, 0)

    def test_product_list_is_paginated_without_query_per_row(self):
        for i in range(54):
            Product.objects.create(
                code=f"LISTE-{i}", name=f"Produit fictif {i:02d}", unit="pièce", minimum=0, target=0
            )
        from django.test.utils import CaptureQueriesContext

        with CaptureQueriesContext(connection) as queries:
            response = self.admin_client.get("/api/v1/products")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["products"]), 50)
        self.assertEqual(response.json()["next_offset"], 50)
        self.assertLess(len(queries), 15)
        self.assertEqual(
            len(self.admin_client.get("/api/v1/products?offset=50").json()["products"]), 4
        )

    def test_missing_staged_file_cannot_be_published_as_a_success(self):
        product = self.create_product()
        identifier, key = uuid.uuid4(), uuid.uuid4().hex
        with patch(
            "apps.core.idempotency.Outbox.objects.create", side_effect=RuntimeError("panne fictive")
        ):
            self.assertEqual(self.upload(product, identifier=identifier, key=key).status_code, 500)
        asset = PhotoAsset.objects.get(product=product)
        photo_storage().delete(asset.thumbnail_key)
        response = self.upload(product, identifier=identifier, key=key)
        self.assertEqual(response.status_code, 503, response.content)
        self.assertFalse(ProductPhoto.objects.exists())
        product.refresh_from_db()
        self.assertIsNone(product.current_photo_id)

    def test_private_file_permissions_and_gc_requires_local_explicit_confirmation(self):
        from pathlib import Path

        from django.core.management import call_command
        from django.core.management.base import CommandError

        product = self.create_product()
        self.upload(product)
        asset = PhotoAsset.objects.get(product=product)
        self.assertEqual(
            (Path(self.photo_dir.name) / asset.object_key).stat().st_mode & 0o777, 0o600
        )
        self.assertEqual(Path(self.photo_dir.name).stat().st_mode & 0o777, 0o700)
        with self.assertRaises(CommandError):
            call_command("cleanup_photo_uploads", execute=True, stdout=io.StringIO())
        with self.assertRaises(CommandError):
            call_command("cleanup_photo_uploads", age_hours=1, stdout=io.StringIO())
        self.assertTrue(photo_storage().exists(asset.object_key))

    def test_floor_deactivation_serializes_with_new_assistant_assignment(self):
        import time
        from dataclasses import replace

        from apps.catalog.services import ensure_registered
        from apps.core import commands
        from tests.catalog_evidence import observations

        ensure_registered()
        original = commands._registry["catalog.floor.update"]
        entered, release = threading.Event(), threading.Event()

        def pause(execution, resource, payload):
            entered.set()
            if not release.wait(timeout=10):
                raise RuntimeError("barrière fictive expirée")
            return original.apply(execution, resource, payload)

        commands._registry[original.name] = replace(original, apply=pause)
        cookie = self.admin_client.cookies[settings.SESSION_COOKIE_NAME].value

        def worker(path, body, method="post"):
            close_old_connections()
            try:
                client = Client(raise_request_exception=False)
                client.cookies[settings.SESSION_COOKIE_NAME] = cookie
                return self.post_json(path, body, client, method=method).status_code
            finally:
                connection.close()

        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                floor_future = pool.submit(
                    worker,
                    f"/api/v1/floors/{self.floor.pk}",
                    {"expected_version": 0, "active": False},
                    "patch",
                )
                try:
                    self.assertTrue(entered.wait(timeout=5))
                    account_future = pool.submit(
                        worker,
                        "/api/v1/users",
                        {
                            "username": "affectation-concurrente-fictive",
                            "display_name": "Concurrente fictive",
                            "role": "ASSISTANT",
                            "floor_id": str(self.floor.pk),
                        },
                    )
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline:
                        with connection.cursor() as cursor:
                            cursor.execute(
                                "SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND pid<>pg_backend_pid() AND wait_event_type='Lock' AND query LIKE '%%accounts_user%%'"
                            )
                            if cursor.fetchone()[0]:
                                break
                        time.sleep(0.02)
                    else:
                        self.fail("Attente SQL d'affectation non observée")
                finally:
                    release.set()
                self.assertEqual(floor_future.result(timeout=15), 200)
                self.assertEqual(account_future.result(timeout=15), 400)
        finally:
            commands._registry[original.name] = original
            release.set()
        self.assertFalse(User.objects.filter(username="affectation-concurrente-fictive").exists())
        self.assistant.refresh_from_db()
        self.assertEqual(self.assistant.floor_id, self.floor.pk)
        observations["floor_deactivation_assignment_race"] = {
            "real_sql_lock_observed": True,
            "new_assignment_refused": True,
            "old_assignment_preserved": True,
        }
