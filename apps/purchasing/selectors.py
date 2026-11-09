from django.db.models import F, Q
from django.utils.translation import gettext

from apps.core.errors import BusinessError
from apps.core.payloads import invalid_input, parse_uuid
from apps.core.policies import check_role
from apps.core.search import matching
from apps.inventory.selectors import page_offset

from . import recommendations
from .models import PurchaseOrder


def needs(actor, params):
    check_role(actor, {"ADMIN"})
    rows = matching(recommendations.query(), params.get("q", ""), ("name", "code")).filter(
        Q(_D__lte=F("minimum")) | Q(_deferred=True), status="ACTIVE", counted_at__isnull=False
    )
    if params.get("state"):
        if params["state"] not in recommendations.LABELS:
            raise invalid_input()
        rows = (
            rows.filter(_deferred=True)
            if params["state"] == "DEFERRED"
            else rows.filter(_rec_state=params["state"])
        )
    if params.get("supplier"):
        rows = rows.filter(
            supplier_links__active=True,
            supplier_links__supplier__active=True,
            supplier_links__supplier_id=parse_uuid(params["supplier"]),
        )
    offset = page_offset(params)
    found = list(rows.order_by("name", "pk")[offset : offset + 51])
    result = [recommendations.data(p) for p in found[:50]]
    if params.get("supplier"):
        for row in result:
            row["group_supplier"] = next(
                s for s in row["suppliers"] if s["id"] == str(parse_uuid(params["supplier"]))
            )
    return result, offset + 50 if len(found) > 50 else None


def order_query(actor, params):
    check_role(actor, {"ADMIN"})
    rows = matching(
        PurchaseOrder.objects.all(),
        params.get("q", ""),
        (
            "reference",
            "external_reference",
            "supplier_name",
            "lines__product_name",
            "lines__product_code",
        ),
    )
    rows = (
        PurchaseOrder.objects.filter(pk__in=rows.values("pk"))
        .select_related("creation_operation__actor")
        .prefetch_related("lines")
    )
    if params.get("status"):
        if params["status"] not in PurchaseOrder.Status.values:
            raise invalid_input()
        rows = rows.filter(status=params["status"])
    if params.get("supplier"):
        rows = rows.filter(supplier_id=parse_uuid(params["supplier"]))
    return rows.order_by("-creation_operation__confirmed_at", "-pk")


def get_order(actor, identifier):
    row = order_query(actor, {}).filter(pk=identifier).first()
    if row is None:
        raise BusinessError("NOT_FOUND", gettext("Achat introuvable."), status=404)
    return row


def order_data(order):
    return {
        "id": str(order.pk),
        "reference": order.reference,
        "supplier_id": str(order.supplier_id),
        "supplier_code": order.supplier_code,
        "supplier_name": order.supplier_name,
        "purchased_on": order.purchased_on.isoformat(),
        "external_reference": order.external_reference,
        "amount": str(order.amount) if order.amount is not None else None,
        "currency": order.currency,
        "status": order.status,
        "status_label": order.get_status_display(),
        "version": order.version,
        "author": order.author_name,
        "registered_at": order.creation_operation.confirmed_at.isoformat(),
        "lines": [
            {
                "id": str(item.pk),
                "product_id": str(item.product_id),
                "code": item.product_code,
                "name": item.product_name,
                "unit": item.unit,
                "quantity": item.quantity,
                "product_version": item.product_version,
                "received": item.received,
                "cancelled": item.cancelled,
                "waiting": item.waiting,
                "supplier_reference": item.supplier_reference,
            }
            for item in order.lines.all()
        ],
    }


def purchase_product_data(product):
    """Valeurs de présentation au moment de l'ajout, sans enregistrer un achat."""
    physical = product.stock.physical
    return {
        "id": str(product.pk),
        "code": product.code,
        "name": product.name,
        "unit": product.unit,
        "version": product.version,
        "photo_url": f"/api/v1/products/{product.pk}/photo?thumbnail=1"
        if product.current_photo_id
        else None,
        "physical": physical,
        "target": product.target,
        "suggested_actual_quantity": max(0, product.target - physical)
        if physical is not None
        else None,
    }
