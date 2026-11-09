"""Projection d'historique par lots bornés ; aucun cache partagé ni fait réécrit."""

from itertools import islice
from uuid import UUID

from django.db.models import Prefetch
from django.utils import formats
from django.utils.translation import gettext

from apps.accounts.models import User
from apps.cases.models import Case, CaseEvent, StockReconciliation
from apps.catalog.models import Category, Floor, Product, Supplier
from apps.purchasing.models import RemainderEvent
from apps.receiving.models import (
    ArrivalReport,
    ManualReminder,
    QuarantineDecision,
    ReceiptCorrection,
    ReceiptCorrectionLine,
    UnknownLink,
)
from apps.requests.models import DeliveryCorrection, DeliveryCorrectionLine

from .filters import PARIS
from .iteration import stream

VENDOR_KINDS = {
    "SURPLUS",
    "NONCONFORMITY",
    "UNKNOWN_ORDER",
    "LATE_CANCELLED_DELIVERY",
    "RECEIPT_ERROR",
}


def references(actor, operation):
    reqs = list(operation.requestevent_set.all())
    if actor.is_requester:
        reqs = [event for event in reqs if event.request.assistant_id == actor.pk]
    result = [(e.request.reference, f"/demandes/{e.request_id}") for e in reqs[:20]]
    if actor.role == "ADMIN" and hasattr(operation, "purchaseorder"):
        order = operation.purchaseorder
        result.append((order.reference, f"/achats/{order.pk}"))
    if actor.role in {"ADMIN", "REPLENISHER"} and hasattr(operation, "orderevent"):
        event = operation.orderevent
        if event.kind == "RECEIPT":
            result.append((event.receipt.reference, f"/receptions/recus/{event.receipt.pk}"))
        elif event.kind == "CORRECTION":
            receipt = event.receipt_correction.receipt
            result.append((receipt.reference, f"/receptions/recus/{receipt.pk}"))
        else:
            result.append(
                (
                    event.order.reference,
                    ("/achats/" if actor.role == "ADMIN" else "/receptions/commandes/")
                    + str(event.order_id),
                )
            )
    return result


class Batch:
    """Même priorité de résolution que l'ancien sélecteur, requêtes groupées.

    Au plus 100 opérations. Les droits sont ceux du queryset serveur déjà filtré.
    Pas de réutilisation entre requêtes HTTP, acteurs ou snapshots.
    """

    def __init__(self, actor, operations):
        self.actor = actor
        self.refs = {o.pk: references(actor, o) for o in operations}
        self.objects, self.delivery_corrections, self.receipt_corrections = {}, {}, {}
        correcting = [o.pk for o in operations if o.command_type == "corrections.confirm"]
        if correcting:
            for correction in (
                DeliveryCorrection.objects.filter(decision__event__operation_id__in=correcting)
                .select_related("decision__event", "delivery__operation", "delivery__request")
                .prefetch_related(
                    Prefetch(
                        "lines",
                        queryset=DeliveryCorrectionLine.objects.select_related(
                            "delivery_line__line"
                        ),
                    )
                )
                .order_by("pk")
            ):
                self.delivery_corrections.setdefault(
                    correction.decision.event.operation_id, correction
                )
            for correction in (
                ReceiptCorrection.objects.filter(event__operation_id__in=correcting)
                .select_related("event", "receipt__event__operation", "receipt__event__order")
                .prefetch_related(
                    Prefetch(
                        "lines",
                        queryset=ReceiptCorrectionLine.objects.select_related("line__order_line"),
                    )
                )
                .order_by("pk")
            ):
                self.receipt_corrections.setdefault(correction.event.operation_id, correction)
        fallback = []
        for operation in operations:
            if self.refs[operation.pk]:
                continue
            if actor.role in {"ADMIN", "REPLENISHER"} and operation.stock_entries.all():
                self.objects[operation.pk] = (
                    ", ".join(e.product_name for e in operation.stock_entries.all()),
                    "/stocks/mouvements",
                )
            elif actor.is_requester:
                self.objects[operation.pk] = self.personal(operation)
            else:
                fallback.append(operation)
        if fallback:
            self.resolve_objects(fallback)

    @staticmethod
    def personal(operation):
        return (
            (gettext("My account"), "/mon-compte")
            if operation.command_type.startswith("accounts.")
            else (gettext("Personal action recorded"), "")
        )

    def resolve_objects(self, operations):
        from .selectors import case_title

        ids = [o.pk for o in operations]
        cases = {}
        # .first() utilisait la PK pour départager plusieurs faits : priorité conservée.
        for model, operation_field, case_field in [
            (Case, "origin_operation_id", "pk"),
            (CaseEvent, "operation_id", "case_id"),
            (StockReconciliation, "operation_id", "case_id"),
            (ArrivalReport, "operation_id", "case_id"),
            (UnknownLink, "operation_id", "report__case_id"),
        ]:
            remaining = [identifier for identifier in ids if identifier not in cases]
            if not remaining:
                break
            for operation_id, case_id in (
                model.objects.filter(**{operation_field + "__in": remaining})
                .order_by("pk")
                .values_list(operation_field, case_field)
            ):
                if case_id:
                    cases.setdefault(operation_id, case_id)
        remaining = [identifier for identifier in ids if identifier not in cases]
        if remaining:
            for operation_id, item_case, report_case in (
                QuarantineDecision.objects.filter(operation_id__in=remaining)
                .order_by("pk")
                .values_list("operation_id", "item__case_id", "report__case_id")
            ):
                cases.setdefault(operation_id, item_case or report_case)
        generic_cases = {}
        for operation in operations:
            try:
                identifier = UUID(
                    str(operation.resource_id or operation.auditevent.after.get("id"))
                )
            except (ValueError, TypeError):
                continue
            if operation.command_type.startswith("cases."):
                generic_cases[operation.pk] = identifier
        case_objects = (
            Case.objects.filter(pk__in=set(cases.values()) | set(generic_cases.values()))
            .select_related("delivery__request", "receipt", "stock_case", "arrival_report")
            .prefetch_related("stock_case__products")
            .in_bulk()
        )
        for operation_id, case_id in cases.items():
            case = case_objects[case_id]
            self.objects[operation_id] = (
                case_title(case),
                ("/receptions/dossiers/" if case.kind in VENDOR_KINDS else "/dossiers/")
                + str(case.pk),
            )
        for model in [ManualReminder, RemainderEvent]:
            remaining = [identifier for identifier in ids if identifier not in self.objects]
            if not remaining:
                break
            for entry in (
                model.objects.filter(operation_id__in=remaining)
                .select_related("order")
                .order_by("pk")
            ):
                self.objects.setdefault(
                    entry.operation_id,
                    (
                        entry.order.reference,
                        ("/achats/" if self.actor.role == "ADMIN" else "/receptions/commandes/")
                        + str(entry.order_id),
                    ),
                )
        remaining = [o for o in operations if o.pk not in self.objects]
        if self.actor.role != "ADMIN":
            for operation in remaining:
                self.objects[operation.pk] = self.personal(operation)
            return
        identifiers = {}
        for operation in remaining:
            try:
                identifiers[operation.pk] = UUID(
                    str(operation.resource_id or operation.auditevent.after.get("id"))
                )
            except (ValueError, TypeError):
                self.objects[operation.pk] = (gettext("Action recorded"), "")
        for prefix, model, path in [
            ("recommendations.", Product, "/achats/besoins/"),
            ("catalog.product.", Product, "/catalogue/"),
            ("catalog.floor.", Floor, "/configuration/floor/"),
            ("catalog.category.", Category, "/configuration/category/"),
            ("catalog.supplier.", Supplier, "/configuration/supplier/"),
            ("accounts.", User, "/administration/utilisateurs/"),
            ("cases.", Case, ""),
        ]:
            grouped = {
                o.pk: identifiers[o.pk]
                for o in remaining
                if o.pk in identifiers and o.command_type.startswith(prefix)
            }
            if not grouped:
                continue
            objects = case_objects if model is Case else model.objects.in_bulk(grouped.values())
            for operation_id, identifier in grouped.items():
                obj = objects.get(identifier)
                if obj is not None:
                    if model is Case:
                        label, destination = (
                            case_title(obj),
                            ("/receptions/dossiers/" if obj.kind in VENDOR_KINDS else "/dossiers/")
                            + str(obj.pk),
                        )
                    else:
                        label, destination = (
                            getattr(
                                obj, "name", getattr(obj, "display_name", gettext("Related object"))
                            ),
                            path + str(identifier),
                        )
                    self.objects[operation_id] = (label, destination)
        for operation in remaining:
            self.objects.setdefault(operation.pk, (gettext("Action recorded"), ""))

    def correction(self, operation):
        correction = self.delivery_corrections.get(operation.pk)
        if correction:
            source = correction.delivery
            date = formats.date_format(
                source.operation.confirmed_at.astimezone(PARIS), "DATETIME_FORMAT", use_l10n=True
            )
            text = gettext("Delivery from %(date)s — %(reference)s") % {
                "date": date,
                "reference": source.request.reference,
            }
            details = [
                gettext(
                    "%(product)s (%(unit)s): handed over %(before)s → %(after)s; change %(delta)s"
                )
                % {
                    "product": line.delivery_line.line.product_name,
                    "unit": line.delivery_line.line.unit,
                    "before": line.effective_before,
                    "after": line.effective_after,
                    "delta": f"{line.effective_after - line.effective_before:+d}",
                }
                for line in correction.lines.all()
            ]
            return text, details
        correction = self.receipt_corrections.get(operation.pk)
        if correction:
            source = correction.receipt
            date = formats.date_format(
                source.event.operation.confirmed_at.astimezone(PARIS),
                "DATETIME_FORMAT",
                use_l10n=True,
            )
            text = gettext("%(reference)s — received on %(date)s") % {
                "reference": source.reference,
                "date": date,
            }
            details = [
                gettext(
                    "%(product)s (%(unit)s): regular received %(before)s → %(after)s; change %(delta)s"
                )
                % {
                    "product": line.line.order_line.product_name,
                    "unit": line.line.order_line.unit,
                    "before": line.regular_before,
                    "after": line.regular_after,
                    "delta": f"{line.regular_after - line.regular_before:+d}",
                }
                for line in correction.lines.all()
            ]
            return text, details
        return "", []


def rows(actor, query):
    from .selectors import ROLES, operation_label

    iterator = stream(query)
    while operations := list(islice(iterator, 100)):
        batch = Batch(actor, operations)
        for operation in operations:
            refs = batch.refs[operation.pk]
            obj = refs[0] if refs else batch.objects[operation.pk]
            corrected, corrections = batch.correction(operation)
            changes = []
            if actor.role == "ADMIN":
                changes = [
                    gettext(
                        "%(product)s (%(unit)s): stock %(physical_before)s → %(physical_after)s; reserved %(reserved_before)s → %(reserved_after)s"
                    )
                    % {
                        "product": e.product_name,
                        "unit": e.unit,
                        "physical_before": e.physical_before,
                        "physical_after": e.physical_after,
                        "reserved_before": e.reserved_before,
                        "reserved_after": e.reserved_after,
                    }
                    for e in operation.stock_entries.all()
                ]
                changes.extend(corrections)
            yield {
                "cells": [
                    operation.confirmed_at,
                    operation.actor.display_name,
                    ROLES.get(operation.actor_role, operation.actor_role),
                    operation_label(operation.command_type),
                    ", ".join(x[0] for x in refs) or obj[0],
                    "; ".join(changes) if actor.role == "ADMIN" else "",
                    corrected,
                ],
                "url": obj[1],
            }
