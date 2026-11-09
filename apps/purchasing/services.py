"""Commandes M7 dans le noyau M0 ; P et R ne sont jamais modifiés."""

import re
from datetime import date
from decimal import Decimal, InvalidOperation

from django.db import connection
from django.utils import timezone
from django.utils.translation import gettext

from apps.accounts.models import User
from apps.catalog.models import Product, ProductSupplier, Supplier
from apps.catalog.services import exact, new_scope, quantity
from apps.core import commands
from apps.core.commands import Command, Effect, Event
from apps.core.errors import BusinessError
from apps.core.payloads import invalid_input, parse_uuid
from apps.core.policies import check_version
from apps.inventory.primitives import lock_stocks

from . import duplicates, recommendations
from .currencies import CURRENCY_CODES
from .models import (
    DuplicateAcknowledgement,
    OrderLine,
    PurchaseOrder,
    Recommendation,
    RecommendationEvent,
    RemainderEvent,
    RemainderLine,
)
from .selectors import order_data

ADMIN = frozenset({User.Role.ADMIN})


def scope(model):
    def authorize(actor, identifier):
        if identifier is None or not model.objects.filter(pk=identifier).exists():
            raise BusinessError("NOT_FOUND", gettext("Référence introuvable."), status=404)

    return authorize


def parse_lines(payload):
    rows = payload.get("lines")
    if not isinstance(rows, list) or not 1 <= len(rows) <= 100:
        raise invalid_input()
    result = []
    seen = set()
    for row in rows:
        exact(row, {"product_id", "quantity", "expected_product_version"})
        p = parse_uuid(row.get("product_id"))
        if p in seen:
            raise BusinessError(
                "DUPLICATE_PRODUCT", gettext("Un produit apparaît plusieurs fois."), status=400
            )
        seen.add(p)
        expected = row.get("expected_product_version")
        if type(expected) is not int or expected < 0:
            raise invalid_input()
        result.append(
            {
                "product_id": p,
                "quantity": quantity(row.get("quantity"), zero=False),
                "expected_product_version": expected,
            }
        )
    return result


def supplier_lock(actor, identifier, payload):
    supplier_id = parse_uuid(payload.get("supplier_id"))
    row = Supplier.objects.select_for_update().filter(pk=supplier_id).first()
    if row is None:
        raise BusinessError(
            "SUPPLIER_REQUIRED", gettext("Choisissez un fournisseur existant."), status=400
        )


def create_resources(actor, identifier, payload):
    rows = parse_lines(payload)
    return rows, lock_stocks({r["product_id"] for r in rows})


def purchase_values(payload):
    exact(
        payload,
        {
            "supplier_id",
            "purchased_on",
            "external_reference",
            "amount",
            "currency",
            "lines",
            "purchase_confirmed",
            "distinct_purchase_ack",
        },
    )
    if payload.get("purchase_confirmed") is not True:
        raise BusinessError(
            "PURCHASE_NOT_CONFIRMED",
            gettext("Confirmez que cet achat a réellement été passé."),
            status=400,
        )
    day = payload.get("purchased_on")
    try:
        if not isinstance(day, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
            raise ValueError
        day = date.fromisoformat(day)
        if day > timezone.localdate():
            raise ValueError
    except ValueError as exc:
        raise BusinessError(
            "INVALID_PURCHASE_DATE",
            gettext("Indiquez la date réelle d’un achat déjà passé."),
            status=400,
        ) from exc
    raw, key = duplicates.reference(payload.get("external_reference", ""))
    amount = payload.get("amount")
    currency = payload.get("currency", "")
    if amount is not None:
        try:
            if not isinstance(amount, str) or not re.fullmatch(r"\d{1,12}(?:\.\d{1,2})?", amount):
                raise ValueError
            amount = Decimal(amount).quantize(Decimal("0.01"))
            if currency not in CURRENCY_CODES:
                raise ValueError
        except (InvalidOperation, ValueError, TypeError) as exc:
            raise BusinessError(
                "INVALID_AMOUNT",
                gettext(
                    "Saisissez un montant positif ou nul avec au plus deux décimales et une devise ISO valide."
                ),
                status=400,
            ) from exc
    elif currency != "":
        raise invalid_input()
    return day, raw, key, amount, currency


def check_duplicates(payload, supplier, day, key, line_key):
    found, digest = duplicates.candidates(supplier, day, key, line_key)
    ack = payload.get("distinct_purchase_ack")
    if ack is not None:
        exact(ack, {"candidate_ids", "candidate_fingerprint", "verified_distinct"})
        ids = ack.get("candidate_ids")
        if not isinstance(ids, list) or any(not isinstance(i, str) for i in ids):
            raise invalid_input()
        valid = (
            ack.get("verified_distinct") is True
            and ack.get("candidate_fingerprint") == digest
            and sorted(ids) == [c["id"] for c in found]
            and bool(found)
        )
    else:
        valid = False
    if found and not valid:
        raise BusinessError(
            "DUPLICATE_PURCHASE" if key else "DISTINCT_PURCHASE_CONFIRMATION_REQUIRED",
            gettext(
                "Un achat ressemblant existe déjà. Vérifiez les commandes avant de confirmer un achat réellement distinct."
            ),
            details={"candidates": found, "candidate_fingerprint": digest},
        )
    if ack is not None and not found:
        raise BusinessError(
            "CANDIDATES_CHANGED", gettext("Les candidats ont changé. Vérifiez à nouveau.")
        )
    return found, digest


def create(execution, resources, payload):
    rows, stocks = resources
    day, raw, key, amount, currency = purchase_values(payload)
    for row in rows:
        product = stocks[row["product_id"]].product
        if row["expected_product_version"] != product.version:
            raise BusinessError(
                "STALE_PRODUCT_VERSION",
                f"Le produit {product.code} a changé (unité actuelle : {product.unit}). Retirez cette ligne et choisissez à nouveau la référence après vérification. Les autres lignes sont conservées.",
                details={
                    "product_id": str(product.pk),
                    "current_unit": product.unit,
                    "current_product_version": product.version,
                },
            )
    supplier = Supplier.objects.get(pk=parse_uuid(payload.get("supplier_id")))
    line_key = duplicates.lines_key(rows)
    found, digest = check_duplicates(payload, supplier, day, key, line_key)
    with connection.cursor() as c:
        c.execute("SELECT nextval('public.purchase_reference_seq')")
        reference = f"ACH-{c.fetchone()[0]:08d}"
    order = PurchaseOrder.objects.create(
        reference=reference,
        supplier=supplier,
        author_name=execution.actor.display_name,
        supplier_code=supplier.code,
        supplier_name=supplier.name,
        supplier_website=supplier.website,
        purchased_on=day,
        creation_operation_id=execution.operation_id,
        external_reference=raw,
        reference_key=key,
        amount=amount,
        currency=currency,
        lines_key=line_key,
    )
    links = {
        item.product_id: item
        for item in ProductSupplier.objects.filter(
            product_id__in=stocks, supplier=supplier, active=True
        )
    }
    for row in rows:
        p = stocks[row["product_id"]].product
        link = links.get(p.pk)
        OrderLine.objects.create(
            order=order,
            product=p,
            product_code=p.code,
            product_name=p.name,
            unit=p.unit,
            photo_id=p.current_photo_id,
            supplier_reference=link.reference if link else "",
            quantity=row["quantity"],
            product_version=p.version,
        )
    if found:
        DuplicateAcknowledgement.objects.create(
            order=order,
            candidate_ids=[c["id"] for c in found],
            candidate_fingerprint=digest,
            verified_distinct=True,
        )
    after = order_data(order)
    return Effect(
        resource={
            "type": "purchase_order",
            "id": str(order.pk),
            "reference": order.reference,
            "version": 0,
            "stock_changed": False,
        },
        after=after,
        details=[
            {
                "purchase_confirmed": True,
                "distinct_purchase_verified": bool(found),
                "candidate_ids": [c["id"] for c in found],
                "candidate_fingerprint": digest,
            }
        ],
        events=(
            Event(
                "purchase.recorded",
                order.pk,
                1,
                {
                    "order": after,
                    "recipient_roles": ["ADMIN", "ACCOUNTANT_EXTERNAL"],
                    "external_delivery": "DEFERRED_M9",
                },
            ),
        ),
        http_status=201,
    )


def cancel_supplier_lock(actor, identifier):
    supplier_id = PurchaseOrder.objects.values_list("supplier_id", flat=True).get(pk=identifier)
    Supplier.objects.select_for_update().get(pk=supplier_id)


def cancel_resources(actor, identifier):
    identifiers = set(
        OrderLine.objects.filter(order_id=identifier).values_list("product_id", flat=True)
    )
    lock_stocks(identifiers)
    return PurchaseOrder.objects.select_for_update().get(pk=identifier)


def cancel(execution, order, payload):
    exact(payload, {"expected_version", "external_verification", "lines"})
    check_version(order.version, payload.get("expected_version"))
    if payload.get("external_verification") is not True:
        raise BusinessError(
            "VERIFICATION_REQUIRED",
            gettext("Vérifiez d’abord le reliquat avec le fournisseur hors de l’application."),
            status=400,
        )
    rows = payload.get("lines")
    if not isinstance(rows, list) or not 1 <= len(rows) <= 100:
        raise invalid_input()
    selected = {}
    existing = {item.pk: item for item in order.lines.all()}
    for row in rows:
        exact(row, {"order_line_id", "quantity"})
        identifier = parse_uuid(row.get("order_line_id"))
        q = quantity(row.get("quantity"), zero=False)
        if identifier in selected or identifier not in existing:
            raise invalid_input()
        if q > existing[identifier].waiting:
            raise BusinessError(
                "REMAINDER_EXCEEDED",
                gettext("Vous ne pouvez annuler que les quantités encore attendues."),
            )
        selected[identifier] = q
    before = order_data(order)
    event = RemainderEvent.objects.create(
        order=order,
        operation_id=execution.operation_id,
        revision=order.version + 1,
        external_verification=True,
    )
    for identifier, q in selected.items():
        line = existing[identifier]
        RemainderLine.objects.create(
            event=event,
            line=line,
            quantity=q,
            cancelled_before=line.cancelled,
            cancelled_after=line.cancelled + q,
        )
        line.cancelled += q
        line.save(update_fields=["cancelled"])
    order.version += 1
    project_order(order)
    after = order_data(order)
    return Effect(
        resource={
            "type": "purchase_order",
            "id": str(order.pk),
            "reference": order.reference,
            "version": order.version,
            "stock_changed": False,
        },
        before=before,
        after=after,
        events=(
            Event(
                "purchase.remainder_cancelled",
                order.pk,
                order.version,
                {"order": after, "recipient_roles": ["ADMIN"]},
            ),
        ),
    )


def order_status(rows):
    received = any(row.received for row in rows)
    if any(row.waiting for row in rows):
        return "PARTIAL" if received else "WAITING"
    elif any(row.cancelled for row in rows):
        return "CLOSED_REMAINDER_CANCELLED" if received else "CANCELLED_NO_RECEIPT"
    return "RECEIVED"


def project_order(order):
    order.status = order_status(list(order.lines.all()))
    order.save(update_fields=["version", "status"])


def rec_resources(actor, identifier):
    lock_stocks({identifier})
    return recommendations.query().get(pk=identifier)


def rec_action(action):
    def apply(execution, product, payload):
        exact(payload, {"expected_version", "state_fingerprint"})
        current = recommendations.data(product)
        check_version(current["version"], payload.get("expected_version"))
        if payload.get("state_fingerprint") != current["state_fingerprint"]:
            raise BusinessError(
                "STALE_VERSION", gettext("Le besoin a changé. Actualisez-le avant de confirmer.")
            )
        if action == "DEFER" and current["state"] != "ACTIVE":
            raise BusinessError("INVALID_STATE", gettext("Seul un besoin actif peut être reporté."))
        if action == "REACTIVATE" and current["state"] != "DEFERRED":
            raise BusinessError(
                "INVALID_STATE", gettext("Ce besoin n’est pas reporté ou n’est plus nécessaire.")
            )
        projection, _ = Recommendation.objects.get_or_create(product=product)
        before = {
            k: str(v) if k == "state_label" else v
            for k, v in current.items()
            if k != "state_fingerprint"
        }
        after = {
            **before,
            "state": "DEFERRED" if action == "DEFER" else "ACTIVE",
            "state_label": str(
                recommendations.LABELS["DEFERRED" if action == "DEFER" else "ACTIVE"]
            ),
            "version": projection.version + 1,
            "reactivated_by_stock": False,
            "deferred": action == "DEFER",
        }
        event = RecommendationEvent.objects.create(
            recommendation=projection,
            operation_id=execution.operation_id,
            action=action,
            revision=projection.version + 1,
            available=current["available"],
            stock_version=product.stock.version,
            product_version=product.version,
            before=before,
            after=after,
        )
        projection.version += 1
        projection.latest_event = event
        projection.save(update_fields=["version", "latest_event"])
        return Effect(
            resource={
                "type": "recommendation",
                "id": str(product.pk),
                "version": projection.version,
                "stock_changed": False,
            },
            before=before,
            after=after,
            events=(
                Event(
                    "recommendation.deferred"
                    if action == "DEFER"
                    else "recommendation.reactivated",
                    product.pk,
                    projection.version,
                    {
                        "product_id": str(product.pk),
                        "recipient_roles": ["ADMIN"],
                        "policy": recommendations.POLICY,
                    },
                ),
            ),
        )

    return apply


COMMANDS = [
    Command(
        "orders.create",
        ADMIN,
        new_scope,
        lambda a, i: None,
        create,
        lock_payload_references=supplier_lock,
        lock_payload_resources=create_resources,
    ),
    Command(
        "orders.cancel_remainder",
        ADMIN,
        scope(PurchaseOrder),
        cancel_resources,
        cancel,
        lock_references=cancel_supplier_lock,
    ),
    Command("recommendations.defer", ADMIN, scope(Product), rec_resources, rec_action("DEFER")),
    Command(
        "recommendations.reactivate", ADMIN, scope(Product), rec_resources, rec_action("REACTIVATE")
    ),
]


def ensure_registered():
    for command in COMMANDS:
        if command.name not in commands._registry:
            commands.register_command(command)
