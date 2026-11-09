"""Ordre M7/M8 : fournisseur → intention M0 → stocks → commande → objets → dossiers."""

from dataclasses import dataclass

from django.utils.translation import gettext

from apps.catalog.models import Supplier
from apps.core.errors import BusinessError
from apps.inventory.primitives import lock_stocks
from apps.purchasing.models import PurchaseOrder

from .models import QuarantineItem, Receipt
from .state import item_state


@dataclass
class OrderScope:
    order: object
    lines: dict
    stocks: dict


def order_supplier(actor, identifier):
    supplier_id = (
        PurchaseOrder.objects.filter(pk=identifier).values_list("supplier_id", flat=True).first()
    )
    if supplier_id is None:
        raise BusinessError("NOT_FOUND", gettext("Commande introuvable."), status=404)
    Supplier.objects.select_for_update().get(pk=supplier_id)


def order_scope(actor, identifier):
    row = PurchaseOrder.objects.get(pk=identifier)
    lines = list(row.lines.select_related("product", "photo").order_by("product_id"))
    stocks = lock_stocks({line.product_id for line in lines})
    row = PurchaseOrder.objects.select_for_update().get(pk=identifier)
    return OrderScope(
        row,
        {
            line.pk: line
            for line in row.lines.select_related("product", "photo").order_by("product_id")
        },
        stocks,
    )


def order_exists(actor, identifier):
    if not PurchaseOrder.objects.filter(pk=identifier).exists():
        raise BusinessError("NOT_FOUND", gettext("Commande introuvable."), status=404)


def item_supplier(actor, identifier):
    value = item_state(identifier)
    if value is None:
        raise BusinessError("NOT_FOUND", gettext("Marchandises isolées introuvables."), status=404)
    order_supplier(actor, value["order_id"])


def item_scope(actor, identifier):
    value = item_state(identifier)
    scope = order_scope(actor, value["order_id"])
    item = (
        QuarantineItem.objects.select_for_update(of=("self",))
        .select_related("case__origin_operation")
        .get(pk=identifier)
    )
    from apps.cases.models import Case

    case = (
        Case.objects.select_for_update(of=("self",))
        .select_related("origin_operation")
        .get(pk=item.case_id)
    )
    return scope, item, case


def receipt_supplier(actor, identifier, payload=None):
    order_id = (
        Receipt.objects.filter(pk=identifier).values_list("event__order_id", flat=True).first()
    )
    if order_id is None:
        raise BusinessError("NOT_FOUND", gettext("Réception introuvable."), status=404)
    order_supplier(actor, order_id)


def report_order(report):
    return report.order_id or getattr(getattr(report, "order_link", None), "order_id", None)
