"""Mutations M2 : intentions/audit M0, jamais d'écriture de stock."""

import re
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from django.db import IntegrityError
from django.utils import timezone
from django.utils.translation import gettext

from apps.accounts.models import User
from apps.core import commands
from apps.core.commands import Command, Effect
from apps.core.errors import BusinessError
from apps.core.payloads import invalid_input, parse_uuid
from apps.core.policies import check_version

from .models import (
    Category,
    Floor,
    FloorProduct,
    PhotoAsset,
    Product,
    ProductPhoto,
    ProductSupplier,
    Supplier,
)
from .selectors import permission_snapshot, product_data, supplier_link_snapshot

ADMIN = frozenset({User.Role.ADMIN})


def exact(payload, allowed):
    if not isinstance(payload, dict) or set(payload) - set(allowed):
        raise invalid_input()


def text(value, maximum, *, empty=False):
    if not isinstance(value, str):
        raise invalid_input()
    value = " ".join(value.split())
    if (not value and not empty) or len(value) > maximum or any(ord(c) < 32 for c in value):
        raise invalid_input()
    return value


def code(value, maximum):
    value = text(value, maximum).upper()
    if not re.fullmatch(r"[A-Z0-9][A-Z0-9_.-]*", value):
        raise invalid_input()
    return value


def quantity(value, *, zero=True):
    if type(value) is not int or not (0 if zero else 1) <= value <= 1000000000:
        raise BusinessError(
            "INVALID_QUANTITY", gettext("Saisissez une quantité entière valide."), status=400
        )
    return value


def boolean(value):
    if type(value) is not bool:
        raise invalid_input()
    return value


def website(value):
    value = text(value, 500, empty=True)
    if value:
        from urllib.parse import urlsplit

        try:
            URLValidator(schemes=["https", "http"])(value)
            parsed = urlsplit(value)
            if parsed.username is not None or parsed.password is not None:
                raise ValueError
        except (ValidationError, ValueError) as exc:
            raise BusinessError(
                "INVALID_URL",
                gettext("Utilisez une adresse http(s) sans identifiant ni mot de passe."),
                status=400,
            ) from exc
    return value


def scope_for(model):
    def scope(actor, identifier):
        if identifier is None or not model.objects.filter(pk=identifier).exists():
            raise BusinessError("NOT_FOUND", gettext("Référence introuvable."), status=404)

    return scope


def new_scope(actor, identifier):
    if identifier is not None:
        raise invalid_input()


def no_resources(actor, identifier):
    return None


def lock_for(model):
    def lock(actor, identifier):
        return model.objects.select_for_update().get(pk=identifier)

    return lock


def lock_reference(model):
    def lock(actor, identifier):
        if identifier:
            model.objects.select_for_update().get(pk=identifier)

    return lock


def references_for(action):
    def lock(actor, identifier, payload):
        if action in {"create", "update"}:
            identifiers = set()
            if identifier:
                current = (
                    Product.objects.filter(pk=identifier)
                    .values_list("category_id", flat=True)
                    .first()
                )
                if current:
                    identifiers.add(current)
            if payload.get("category_id") is not None:
                identifiers.add(parse_uuid(payload["category_id"]))
            list(Category.objects.select_for_update().filter(pk__in=identifiers).order_by("pk"))
        if action in {"permissions", "suppliers"}:
            field = "permissions" if action == "permissions" else "suppliers"
            reference_field = "floor_id" if action == "permissions" else "supplier_id"
            relation = FloorProduct if action == "permissions" else ProductSupplier
            model = Floor if action == "permissions" else Supplier
            rows = payload.get(field)
            if (
                not isinstance(rows, list)
                or len(rows) > 1000
                or any(not isinstance(row, dict) for row in rows)
            ):
                raise invalid_input()
            identifiers = set(
                relation.objects.filter(product_id=identifier).values_list(
                    reference_field, flat=True
                )
            )
            identifiers.update(parse_uuid(row.get(reference_field)) for row in rows)
            list(model.objects.select_for_update().filter(pk__in=identifiers).order_by("pk"))

    return lock


def stamped_save(instance, execution, fields):
    instance.version += 1
    instance.updated_at = timezone.now()
    instance.last_operation_id = execution.operation_id
    instance.save(update_fields=[*fields, "version", "updated_at", "last_operation"])


def reference_data(instance):
    result = {
        "id": str(instance.pk),
        "name": instance.name,
        "active": instance.active,
        "version": instance.version,
    }
    if hasattr(instance, "code"):
        result["code"] = instance.code
    if isinstance(instance, Supplier):
        result["website"] = instance.website
    return result


def reference_handler(model, creating):
    def apply(execution, instance, payload):
        allowed = {"name", "active"}
        if model != Category:
            allowed.add("code")
        if model == Supplier:
            allowed.add("website")
        if not creating:
            allowed.add("expected_version")
        exact(payload, allowed)
        if creating:
            instance = model(created_by=execution.actor, last_operation_id=execution.operation_id)
            before = {}
        else:
            check_version(instance.version, payload.get("expected_version"))
            before = reference_data(instance)
        instance.name = text(payload.get("name", instance.name), 150 if model == Supplier else 120)
        instance.active = boolean(payload.get("active", True if creating else instance.active))
        if model != Category:
            new_code = code(payload.get("code", instance.code), 32)
            if not creating and new_code != instance.code:
                raise BusinessError(
                    "REFERENCE_IMMUTABLE",
                    gettext("Le code de référence est permanent. Modifiez son nom si nécessaire."),
                )
            instance.code = new_code
        if model == Supplier:
            instance.website = website(payload.get("website", instance.website))
        try:
            if creating:
                instance.save(force_insert=True)
            else:
                stamped_save(
                    instance,
                    execution,
                    [field for field in ["name", "active", "website"] if hasattr(instance, field)],
                )
        except IntegrityError as exc:
            raise BusinessError(
                "REFERENCE_CONFLICT", gettext("Cette référence existe déjà.")
            ) from exc
        return Effect(
            reference_data(instance),
            before=before,
            after=reference_data(instance),
            http_status=201 if creating else 200,
        )

    return apply


def product_create(execution, unused, payload):
    exact(payload, {"code", "name", "category_id", "unit", "minimum", "target"})
    product = Product(
        code=code(payload.get("code"), 64),
        created_by=execution.actor,
        last_operation_id=execution.operation_id,
    )
    set_product_fields(product, payload)
    try:
        product.save(force_insert=True)
    except IntegrityError as exc:
        raise BusinessError(
            "REFERENCE_CONFLICT", gettext("Cette référence produit existe déjà.")
        ) from exc
    return Effect(product_data(product), after=product_data(product), http_status=201)


def set_product_fields(product, payload):
    product.name = text(payload.get("name", product.name), 150)
    unit = text(payload.get("unit", product.unit), 40)
    if any(char in unit for char in "/;=|+"):
        raise BusinessError(
            "INVALID_UNIT",
            gettext("Renseignez une seule unité de comptage, sans conversion."),
            status=400,
        )
    if product.counted_at and unit != product.unit:
        raise BusinessError(
            "UNIT_LOCKED",
            gettext(
                "L'unité est définitivement verrouillée depuis le premier inventaire, même nul."
            ),
        )
    product.unit = unit
    product.minimum = quantity(payload.get("minimum", product.minimum))
    product.target = quantity(payload.get("target", product.target))
    if product.target < product.minimum:
        raise BusinessError(
            "INVALID_TARGET",
            gettext("La quantité souhaitée doit être au moins égale à la limite de stock bas."),
            status=400,
        )
    category_id = payload.get(
        "category_id", str(product.category_id) if product.category_id else None
    )
    if category_id is not None:
        category = Category.objects.filter(pk=parse_uuid(category_id)).first()
        if category is None or (not category.active and product.category_id != category.pk):
            raise BusinessError(
                "CATEGORY_UNAVAILABLE", gettext("Choisissez une catégorie active."), status=400
            )
        product.category = category
    else:
        product.category = None


def product_update(execution, product, payload):
    exact(payload, {"expected_version", "name", "category_id", "unit", "minimum", "target"})
    check_version(product.version, payload.get("expected_version"))
    before = product_data(product)
    set_product_fields(product, payload)
    stamped_save(product, execution, ["name", "category", "unit", "minimum", "target"])
    return Effect(product_data(product), before=before, after=product_data(product))


def product_archive(execution, product, payload):
    exact(payload, {"expected_version", "confirmed"})
    check_version(product.version, payload.get("expected_version"))
    if payload.get("confirmed") is not True:
        raise BusinessError(
            "CONFIRMATION_REQUIRED",
            gettext("Confirmez l'archivage de cette référence."),
            status=400,
        )
    before = product_data(product)
    product.status = Product.Status.ARCHIVED
    stamped_save(product, execution, ["status"])
    return Effect(
        product_data(product),
        before=before,
        after=product_data(product),
        details=[{"engagements_preserved": True, "engagement_module_available": True}],
    )


def product_activate(execution, product, payload):
    exact(payload, {"expected_version", "inventory_verified", "photo_verified"})
    check_version(product.version, payload.get("expected_version"))
    if not product.counted_at or not product.initial_inventory_operation_id:
        raise BusinessError(
            "INVENTORY_REQUIRED",
            gettext(
                "Un inventaire confirmé est nécessaire. Le comptage n’est pas encore disponible dans cette version."
            ),
        )
    if product.status not in [Product.Status.COUNTED_PENDING_ACTIVATION, Product.Status.ARCHIVED]:
        raise BusinessError(
            "INVALID_STATE", gettext("Ce produit n'est pas en attente d'activation.")
        )
    if (
        payload.get("inventory_verified") is not True
        or payload.get("photo_verified") is not True
        or not product.current_photo_id
    ):
        raise BusinessError(
            "ACTIVATION_VERIFICATION_REQUIRED",
            gettext("Vérifiez l'inventaire et la photographie avant d'activer."),
            status=400,
        )
    before = product_data(product)
    product.status = Product.Status.ACTIVE
    product.activation_operation_id = execution.operation_id
    stamped_save(product, execution, ["status", "activation_operation"])
    # Aucun fournisseur, lien ou prix n'entre dans les conditions d'activation.
    return Effect(
        product_data(product),
        before=before,
        after=product_data(product),
        details=[{"inventory_verified": True, "photo_verified": True}],
    )


def floor_permissions(execution, product, payload):
    exact(payload, {"expected_version", "permissions"})
    check_version(product.version, payload.get("expected_version"))
    rows = payload.get("permissions")
    if not isinstance(rows, list) or len(rows) > 1000:
        raise invalid_input()
    planned, seen = [], set()
    for row in rows:
        exact(row, {"floor_id", "visible", "demandable", "maximum"})
        floor_id = parse_uuid(row.get("floor_id"))
        if floor_id in seen:
            raise invalid_input()
        seen.add(floor_id)
        if not Floor.objects.filter(pk=floor_id).exists():
            raise BusinessError("NOT_FOUND", gettext("Unité introuvable."), status=404)
        visible, demandable = (
            boolean(row.get("visible", False)),
            boolean(row.get("demandable", False)),
        )
        maximum = quantity(row["maximum"], zero=False) if row.get("maximum") is not None else None
        if demandable and (not visible or maximum is None):
            raise BusinessError(
                "CEILING_REQUIRED",
                gettext(
                    "Un produit demandable doit être visible et posséder un plafond entier positif."
                ),
                status=400,
            )
        planned.append((floor_id, visible, demandable, maximum))
    before = {
        **product_data(product),
        "permissions": [
            permission_snapshot(row)
            for row in product.floor_permissions.select_related("floor").order_by("floor_id")
        ],
    }
    # Absence dans le nouvel ensemble = retrait explicite, sans DELETE de ligne.
    product.floor_permissions.exclude(floor_id__in=seen).update(
        visible=False, demandable=False, maximum=None
    )
    for floor_id, visible, demandable, maximum in planned:
        FloorProduct.objects.update_or_create(
            product=product,
            floor_id=floor_id,
            defaults={"visible": visible, "demandable": demandable, "maximum": maximum},
        )
    stamped_save(product, execution, [])
    after = {
        **product_data(product),
        "permissions": [
            permission_snapshot(row)
            for row in product.floor_permissions.select_related("floor").order_by("floor_id")
        ],
    }
    return Effect(product_data(product), before=before, after=after)


def supplier_links(execution, product, payload):
    exact(payload, {"expected_version", "suppliers"})
    check_version(product.version, payload.get("expected_version"))
    rows = payload.get("suppliers")
    if not isinstance(rows, list) or len(rows) > 1000:
        raise invalid_input()
    planned, seen, preferred_count = [], set(), 0
    for row in rows:
        exact(
            row,
            {"supplier_id", "preferred", "reference", "website", "indicative_price", "currency"},
        )
        supplier_id = parse_uuid(row.get("supplier_id"))
        if supplier_id in seen:
            raise invalid_input()
        seen.add(supplier_id)
        supplier = Supplier.objects.filter(pk=supplier_id).first()
        if not supplier or (
            not supplier.active
            and not ProductSupplier.objects.filter(
                product=product, supplier=supplier, active=True
            ).exists()
        ):
            raise BusinessError(
                "SUPPLIER_UNAVAILABLE", gettext("Choisissez un fournisseur actif."), status=400
            )
        preferred = boolean(row.get("preferred", False))
        preferred_count += int(preferred)
        price, currency = None, text(row.get("currency", ""), 3, empty=True).upper()
        if row.get("indicative_price") is not None:
            try:
                if not isinstance(row["indicative_price"], str):
                    raise ValueError
                price = Decimal(row["indicative_price"])
                if (
                    not price.is_finite()
                    or price < 0
                    or price > Decimal("999999999999.99")
                    or price.as_tuple().exponent < -2
                    or not re.fullmatch(r"[A-Z]{3}", currency)
                ):
                    raise ValueError
            except (InvalidOperation, ValueError) as exc:
                raise BusinessError(
                    "INVALID_PRICE",
                    gettext(
                        "Prix positif ou nul à deux décimales maximum et devise de trois lettres requis."
                    ),
                    status=400,
                ) from exc
        elif currency:
            raise BusinessError(
                "INVALID_PRICE", gettext("La devise accompagne un prix indicatif."), status=400
            )
        planned.append(
            (
                supplier_id,
                {
                    "active": True,
                    "preferred": preferred,
                    "reference": text(row.get("reference", ""), 150, empty=True),
                    "website": website(row.get("website", "")),
                    "indicative_price": price,
                    "currency": currency,
                },
            )
        )
    if preferred_count > 1:
        raise BusinessError(
            "MULTIPLE_PREFERRED", gettext("Choisissez au plus un fournisseur habituel."), status=400
        )
    before = {
        **product_data(product),
        "suppliers": [
            supplier_link_snapshot(row)
            for row in product.supplier_links.select_related("supplier").order_by("supplier_id")
        ],
    }
    product.supplier_links.update(preferred=False)
    product.supplier_links.exclude(supplier_id__in=seen).update(active=False)
    for supplier_id, defaults in planned:
        ProductSupplier.objects.update_or_create(
            product=product, supplier_id=supplier_id, defaults=defaults
        )
    stamped_save(product, execution, [])
    after = {
        **product_data(product),
        "suppliers": [
            supplier_link_snapshot(row)
            for row in product.supplier_links.select_related("supplier").order_by("supplier_id")
        ],
    }
    return Effect(product_data(product), before=before, after=after)


def photo_attach(execution, product, payload):
    exact(payload, {"expected_version", "asset_id", "source_digest"})
    check_version(product.version, payload.get("expected_version"))
    asset = (
        PhotoAsset.objects.select_for_update()
        .filter(
            pk=parse_uuid(payload.get("asset_id")),
            product=product,
            uploader=execution.actor,
            intent=execution.intent,
            source_digest=payload.get("source_digest"),
        )
        .first()
    )
    if asset is None:
        raise BusinessError(
            "PHOTO_NOT_READY", gettext("La photographie doit être à nouveau téléversée.")
        )
    before = product_data(product)
    photo = ProductPhoto.objects.create(
        product=product,
        asset=asset,
        replaces_id=product.current_photo_id,
        operation_id=execution.operation_id,
    )
    product.current_photo = photo
    stamped_save(product, execution, ["current_photo"])
    return Effect(
        product_data(product),
        before=before,
        after=product_data(product),
        details=[
            {
                "photo_id": str(photo.pk),
                "replaces_id": str(photo.replaces_id) if photo.replaces_id else None,
                "sha256": asset.digest,
            }
        ],
    )


COMMANDS = []
for name, model in [("floor", Floor), ("category", Category), ("supplier", Supplier)]:
    COMMANDS.extend(
        [
            Command(
                f"catalog.{name}.create",
                ADMIN,
                new_scope,
                no_resources,
                reference_handler(model, True),
            ),
            Command(
                f"catalog.{name}.update",
                ADMIN,
                scope_for(model),
                lock_for(model),
                reference_handler(model, False),
                lock_references=lock_reference(model),
            ),
        ]
    )
for action, handler in [
    ("create", product_create),
    ("update", product_update),
    ("activate", product_activate),
    ("archive", product_archive),
    ("permissions", floor_permissions),
    ("suppliers", supplier_links),
    ("photo", photo_attach),
]:
    COMMANDS.append(
        Command(
            f"catalog.product.{action}",
            ADMIN,
            new_scope if action == "create" else scope_for(Product),
            no_resources if action == "create" else lock_for(Product),
            handler,
            lock_payload_references=references_for(action),
        )
    )


def ensure_registered():
    for command in COMMANDS:
        if command.name not in commands._registry:
            commands.register_command(command)
