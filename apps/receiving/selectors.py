"""Lectures M8 : contexte des tâches R, supervision N, jamais de montant exposé à R."""

from django.db.models import Prefetch
from django.utils.translation import gettext

from apps.core.errors import BusinessError
from apps.core.policies import check_role
from apps.core.search import matching
from apps.purchasing.models import PurchaseOrder

from .models import (
    ArrivalReport,
    ManualReminder,
    QuarantineItem,
    Receipt,
    ReceiptCorrection,
    ReceiptCorrectionLine,
    ReceiptLine,
)
from .state import effective, item_state


def orders(actor, params):
    check_role(actor, {"ADMIN", "REPLENISHER"})
    rows = matching(
        PurchaseOrder.objects.all(),
        params.get("q", ""),
        ("supplier_name", "reference", "external_reference"),
    )
    status = params.get("status", "")
    if status:
        if status not in PurchaseOrder.Status.values:
            raise BusinessError("INVALID_INPUT", gettext("État de commande invalide."), status=400)
        rows = rows.filter(status=status)
    return (
        rows.select_related("creation_operation")
        .prefetch_related("lines__product__stock", "lines__photo")
        .order_by("-creation_operation__confirmed_at", "-pk")
    )


def effective_receipts(actor, start, end):
    """A11.5 : régulier net à la date du reçu ; surplus et décisions exclus.

    Lecture de lignes seulement, sans nouvelle statistique ni export M10.
    """
    from django.db.models import BigIntegerField, F, OuterRef, Subquery, Sum, Value
    from django.db.models.functions import Coalesce

    from apps.core.periods import origin_period

    from .models import ReceiptCorrectionLine, ReceiptLine

    check_role(actor, {"ADMIN"})
    deltas = (
        ReceiptCorrectionLine.objects.filter(line_id=OuterRef("pk"))
        .values("line_id")
        .annotate(total=Sum(F("regular_after") - F("regular_before")))
        .values("total")
    )
    rows = ReceiptLine.objects.select_related("receipt__event__operation", "order_line").annotate(
        effective_quantity=F("regular")
        + Coalesce(Subquery(deltas), Value(0), output_field=BigIntegerField())
    )
    return origin_period(rows, "receipt__event__operation__confirmed_at", start, end)


def get_order(actor, identifier):
    order = orders(actor, {}).filter(pk=identifier).first()
    if order is None:
        raise BusinessError("NOT_FOUND", gettext("Commande introuvable."), status=404)
    return order


def order_data(order):
    reminder = (
        ManualReminder.objects.filter(order=order)
        .select_related("operation__actor")
        .order_by("-operation__confirmed_at", "-pk")
        .first()
    )
    return {
        "id": str(order.pk),
        "reference": order.reference,
        "supplier_name": order.supplier_name,
        "external_reference": order.external_reference,
        "purchased_on": order.purchased_on.isoformat(),
        "status": order.status,
        "status_label": order.get_status_display(),
        "version": order.version,
        "lines": [
            {
                "id": str(line.pk),
                "product_id": str(line.product_id),
                "name": line.product_name,
                "code": line.product_code,
                "unit": line.unit,
                "quantity": line.quantity,
                "received": line.received,
                "cancelled": line.cancelled,
                "waiting": line.waiting,
                "stock_version": line.product.stock.version,
                "physical": line.product.stock.physical,
                "reserved": line.product.stock.reserved,
                "photo_url": f"/api/v1/receiving/orders/{order.pk}/lines/{line.pk}/photo"
                if line.photo_id
                else None,
                "unit_compatible": line.unit == line.product.unit,
                "inventoried": line.product.counted_at is not None,
            }
            for line in order.lines.select_related("product__stock", "photo").order_by(
                "product_name", "id"
            )
        ],
        "last_reminder": {
            "id": str(reminder.pk),
            "at": reminder.operation.confirmed_at.isoformat(),
            "author": reminder.operation.actor.display_name,
        }
        if reminder
        else None,
    }


def receipts(actor):
    check_role(actor, {"ADMIN", "REPLENISHER"})
    rows = (
        Receipt.objects.select_related("event__order", "event__operation__actor", "source_report")
        .prefetch_related(
            Prefetch(
                "lines",
                queryset=ReceiptLine.objects.select_related("order_line", "source_consumption")
                .order_by("order_line__product_name", "id")
                .prefetch_related(
                    Prefetch(
                        "corrections",
                        # Une seule dernière révision par ligne, via fenêtre SQL.
                        queryset=ReceiptCorrectionLine.objects.order_by("-correction__revision")[
                            :1
                        ],
                        to_attr="latest_corrections",
                    )
                ),
                to_attr="loaded_lines",
            ),
            Prefetch(
                "corrections",
                queryset=ReceiptCorrection.objects.select_related(
                    "event__operation__actor"
                ).order_by("revision"),
                to_attr="loaded_corrections",
            ),
        )
        .order_by("-event__operation__confirmed_at", "-id")
    )
    if actor.role == "REPLENISHER":
        rows = rows.filter(event__operation__actor=actor)
    return rows


def get_receipt(actor, identifier):
    row = receipts(actor).filter(pk=identifier).first()
    if row is None:
        raise BusinessError(
            "NOT_FOUND", gettext("Réception introuvable dans votre périmètre."), status=404
        )
    return row


def receipt_data(row):
    lines = getattr(row, "loaded_lines", None)
    if lines is None:
        lines = row.lines.select_related("order_line", "source_consumption").order_by(
            "order_line__product_name", "id"
        )
    corrections = getattr(row, "loaded_corrections", None)
    if corrections is None:
        corrections = row.corrections.select_related("event__operation__actor").order_by("revision")
    return {
        "id": str(row.pk),
        "reference": row.reference,
        "order_id": str(row.event.order_id),
        "order_reference": row.event.order.reference,
        "supplier_name": row.event.order.supplier_name,
        "author": row.event.operation.actor.display_name,
        "at": row.event.operation.confirmed_at.isoformat(),
        "version": row.version,
        "source_report_id": str(row.source_report_id) if row.source_report_id else None,
        "lines": [
            {
                "id": str(line.pk),
                "order_line_id": str(line.order_line_id),
                "product_id": str(line.order_line.product_id),
                "name": line.order_line.product_name,
                "unit": line.order_line.unit,
                "initial": {
                    key: getattr(line, key)
                    for key in [
                        "conforming",
                        "nonconforming",
                        "regular",
                        "surplus",
                        "nonconformity",
                    ]
                },
                "effective": effective(line),
            }
            for line in lines
        ],
        "corrections": [
            {
                "id": str(c.pk),
                "at": c.event.operation.confirmed_at.isoformat(),
                "author": c.event.operation.actor.display_name,
                "message": c.message,
                "revision": c.revision,
            }
            for c in corrections
        ],
    }


def available_sources(order):
    reports = ArrivalReport.objects.filter(
        order_id=order.pk, kind="LATE_CANCELLED_DELIVERY"
    ) | ArrivalReport.objects.filter(order_link__order_id=order.pk)
    result = []
    for report in reports.select_related("case").order_by("operation__confirmed_at", "id"):
        states = [
            item_state(item.pk)
            for item in QuarantineItem.objects.filter(case_id=report.case_id).order_by("pk")
        ]
        if any(item["remaining"] for item in states):
            result.append(
                {
                    "id": str(report.pk),
                    "reference": report.reference,
                    "kind": report.kind,
                    "case_version": report.case.version,
                    "items": states,
                }
            )
    return result
