"""Correction additive d'un reçu : aperçu M6, delta net, jamais réécriture d'origine."""

from dataclasses import dataclass
from types import SimpleNamespace

from django.utils.translation import gettext

from apps.cases.models import Case, StockHold
from apps.cases.previews import fingerprint
from apps.cases.workflow import add_event, reviewed
from apps.catalog.services import exact, quantity
from apps.core.commands import Effect, Event
from apps.core.errors import BusinessError
from apps.core.payloads import invalid_input, parse_uuid
from apps.core.policies import check_version
from apps.inventory.primitives import append_change
from apps.purchasing.services import order_status, project_order

from . import locks
from .common import isolation_event, new_case, opened
from .models import (
    OrderEvent,
    QuarantineItem,
    Receipt,
    ReceiptCorrection,
    ReceiptCorrectionLine,
    SourceConsumption,
)
from .state import NONCONFORMITIES, effective, item_state


@dataclass
class CorrectionScope:
    order_scope: object
    receipt: object
    lines: dict
    items: dict
    cases: dict
    case: object

    @property
    def stocks(self):
        return self.order_scope.stocks


def resources(actor, identifier, payload):
    source = Receipt.objects.select_related("event").filter(pk=identifier).first()
    if source is None:
        raise BusinessError("NOT_FOUND", gettext("Réception introuvable."), status=404)
    scope = locks.order_scope(actor, source.event.order_id)
    receipt = (
        Receipt.objects.select_for_update(of=("self",)).select_related("event").get(pk=identifier)
    )
    lines = {
        line.pk: line
        for line in receipt.lines.select_related("order_line", "source_consumption").order_by(
            "order_line__product_id"
        )
    }
    item_ids = set(
        QuarantineItem.objects.filter(receipt_line__receipt=receipt).values_list("pk", flat=True)
    ) | set(
        SourceConsumption.objects.filter(line__receipt=receipt).values_list("item_id", flat=True)
    )
    items = {
        item.pk: item
        for item in QuarantineItem.objects.select_for_update()
        .filter(pk__in=item_ids)
        .order_by("pk")
    }
    cid = parse_uuid(payload.get("case_id"))
    if not Case.objects.filter(pk=cid, receipt=receipt, kind="RECEIPT_ERROR").exists():
        raise BusinessError(
            "CASE_SOURCE_MISMATCH", gettext("Ce dossier ne concerne pas cette réception.")
        )
    case_ids = {cid} | {item.case_id for item in items.values()}
    cases = {
        case.pk: case
        for case in Case.objects.select_for_update(of=("self",))
        .select_related("origin_operation")
        .filter(pk__in=case_ids)
        .order_by("pk")
    }
    return CorrectionScope(scope, receipt, lines, items, cases, cases[cid])


def blocking(item):
    return list(item.decisions.values("id", "kind", "quantity"))


def plan(data, payload):
    exact(
        payload,
        {
            "kind",
            "source_id",
            "case_id",
            "expected_versions",
            "corrected_lines",
            "verification",
            "message",
            "preview_hash",
        },
    )
    if payload.get("kind") != "RECEIPT" or parse_uuid(payload.get("source_id")) != data.receipt.pk:
        raise invalid_input()
    reviewed(payload)
    versions = payload.get("expected_versions")
    if not isinstance(versions, dict) or set(versions) != {
        "order",
        "receipt",
        "case",
        "stocks",
        "quarantine",
    }:
        raise invalid_input()
    scope = data.order_scope
    check_version(scope.order.version, versions["order"])
    check_version(data.receipt.version, versions["receipt"])
    check_version(data.case.version, versions["case"])
    product_ids = {line.order_line.product_id for line in data.lines.values()}
    if not isinstance(versions["stocks"], dict) or set(versions["stocks"]) != {
        str(pid) for pid in product_ids
    }:
        raise invalid_input()
    if not isinstance(versions["quarantine"], dict) or set(versions["quarantine"]) != {
        str(iid) for iid in data.items
    }:
        raise invalid_input()
    for pid in product_ids:
        check_version(scope.stocks[pid].balance.version, versions["stocks"][str(pid)])
    for iid in data.items:
        check_version(item_state(iid)["version"], versions["quarantine"][str(iid)])
    rows = payload.get("corrected_lines")
    if not isinstance(rows, list) or not 1 <= len(rows) <= 100:
        raise invalid_input()
    result, seen, shortages = [], set(), []
    own_items = {
        (item.receipt_line_id, item.kind): item
        for item in data.items.values()
        if item.receipt_line_id
    }
    for row in rows:
        exact(row, {"receipt_line_id", "conforming", "nonconforming", "nonconformity"})
        lid = parse_uuid(row.get("receipt_line_id"))
        if lid not in data.lines or lid in seen:
            raise invalid_input()
        seen.add(lid)
        source = data.lines[lid]
        order_line = scope.lines[source.order_line_id]
        stock = scope.stocks[order_line.product_id]
        before = effective(source)
        c, nc = quantity(row.get("conforming")), quantity(row.get("nonconforming"))
        kind = row.get("nonconformity", "")
        if c + nc > 1000000000 or (nc and kind not in NONCONFORMITIES) or (not nc and kind != ""):
            raise invalid_input()
        extra = c - before["conforming"]
        if extra > order_line.waiting and order_line.cancelled:
            raise BusinessError(
                "LATE_CANCELLED_DELIVERY",
                gettext(
                    "La hausse dépasse les quantités encore attendues et rencontre un reliquat annulé."
                ),
                details={"order_line_id": str(order_line.pk)},
            )
        regular = (
            before["regular"] + min(extra, order_line.waiting)
            if extra > 0
            else min(before["regular"], c)
        )
        after = {
            "conforming": c,
            "nonconforming": nc,
            "regular": regular,
            "surplus": c - regular,
            "nonconformity": kind,
            "source_grant": 0,
        }
        isolations = []
        for item_kind, key in [("SURPLUS", "surplus"), ("NONCONFORMING", "nonconforming")]:
            item = own_items.get((lid, item_kind))
            current = (
                item_state(item.pk)
                if item
                else {"quantity": 0, "accepted": 0, "consumed": 0, "disposed": 0, "remaining": 0}
            )
            if item:
                if after[key] < current["accepted"] + current["consumed"] + current["disposed"] or (
                    current["refused"] and after[key] != current["quantity"]
                ):
                    raise BusinessError(
                        "ISOLATION_ALREADY_DECIDED",
                        gettext(
                            "Cette correction contredit une décision ou un traitement déjà effectué sur les unités isolées."
                        ),
                        details={
                            "case_id": str(item.case_id),
                            "blocking_decisions": [
                                {**d, "id": str(d["id"])} for d in blocking(item)
                            ],
                        },
                    )
            if item or after[key]:
                isolations.append(
                    {
                        "kind": item_kind,
                        "accepted": current["accepted"],
                        "disposed": current["disposed"],
                        "before": current["remaining"],
                        "after": after[key]
                        - current["accepted"]
                        - current["consumed"]
                        - current["disposed"],
                    }
                )
        consumption = getattr(source, "source_consumption", None)
        if consumption:
            current = item_state(consumption.item_id)
            observed_before = before["conforming"] + before["nonconforming"]
            observed_after = c + nc
            if observed_after > current["remaining"] + observed_before:
                raise BusinessError(
                    "ISOLATION_EXCEEDED",
                    gettext(
                        "Le colis lié a déjà été reçu ou traité : cette correction dépasserait ses unités isolées."
                    ),
                    details={"item_id": str(consumption.item_id)},
                )
            if observed_after > observed_before:
                grant = before["source_grant"] + min(
                    observed_after - observed_before, current["grant_remaining"]
                )
            else:
                grant = min(before["source_grant"], observed_after)
            if current["kind"] == "LATE_CANCELLED" and grant != observed_after:
                raise BusinessError(
                    "REINSTATEMENT_REQUIRED",
                    gettext("Le reliquat correspondant doit d’abord être rétabli."),
                )
            after["source_grant"] = grant
        delta = regular - before["regular"]
        if delta and (stock.balance.physical is None or stock.product.unit != order_line.unit):
            raise BusinessError(
                "NOT_COUNTED_OR_UNIT_CHANGED",
                gettext("L’inventaire ou l’unité ne permet pas cette correction."),
            )
        p = stock.balance.physical + delta if stock.balance.physical is not None else None
        if delta < 0 and StockHold.objects.filter(product_id=order_line.product_id).exists():
            raise BusinessError(
                "PRODUCT_ON_HOLD", gettext("Régularisez d’abord le conflit de stock de ce produit.")
            )
        if p is not None and p > 1000000000:
            raise invalid_input()
        if p is not None and p < stock.balance.reserved:
            shortages.append({"product_id": str(order_line.product_id), "attempted_physical": p})
        result.append(
            {
                "receipt_line_id": str(lid),
                "order_line_id": str(order_line.pk),
                "product_id": str(order_line.product_id),
                "name": order_line.product_name,
                "unit": order_line.unit,
                "Qf": order_line.quantity,
                "isolations": isolations,
                "initial": {
                    key: getattr(source, key)
                    for key in [
                        "conforming",
                        "nonconforming",
                        "regular",
                        "surplus",
                        "nonconformity",
                    ]
                },
                "before": {
                    **before,
                    "P": stock.balance.physical,
                    "R": stock.balance.reserved,
                    "D": stock.balance.available,
                    "F": order_line.received,
                    "A": order_line.cancelled,
                    "W": order_line.waiting,
                },
                "after": {
                    **after,
                    "P": p,
                    "R": stock.balance.reserved,
                    "D": p - stock.balance.reserved if p is not None else None,
                    "F": order_line.received + delta,
                    "A": order_line.cancelled,
                    "W": order_line.waiting - delta,
                },
                "delta_p": delta,
            }
        )
    if shortages:
        raise BusinessError(
            "STOCK_BELOW_RESERVED",
            gettext(
                "La correction laisserait moins d’unités en réserve que de réservations. Aucun mouvement enregistré."
            ),
            details={"lines": shortages, "source_case_id": str(data.case.pk)},
        )
    result_plan = {
        "source_id": str(data.receipt.pk),
        "case_id": str(data.case.pk),
        "order_id": str(scope.order.pk),
        "reference": data.receipt.reference,
        "versions": versions,
        "lines": result,
        "order_status_before": scope.order.status,
        "order_status_after": order_status(
            [
                next(
                    (
                        SimpleNamespace(
                            received=row["after"]["F"],
                            cancelled=row["after"]["A"],
                            waiting=row["after"]["W"],
                        )
                        for row in result
                        if row["order_line_id"] == str(line.pk)
                    ),
                    line,
                )
                for line in scope.lines.values()
            ]
        ),
    }
    result_plan["preview_hash"] = fingerprint(result_plan)
    return result_plan


def confirm(execution, data, payload):
    calculated = plan(data, payload)
    if payload.get("preview_hash") != calculated["preview_hash"]:
        raise BusinessError(
            "PREVIEW_CHANGED",
            gettext("Prévisualisez à nouveau les conséquences avant de confirmer."),
        )
    scope, receipt = data.order_scope, data.receipt
    scope.order.version += 1
    event = OrderEvent.objects.create(
        order=scope.order,
        operation_id=execution.operation_id,
        kind="CORRECTION",
        revision=scope.order.version,
    )
    previous = receipt.corrections.order_by("-revision").first()
    correction = ReceiptCorrection.objects.create(
        event=event,
        receipt=receipt,
        case=data.case,
        previous=previous,
        revision=receipt.version + 1,
        verification=True,
        message=reviewed(payload),
    )
    touched_cases = {}
    new_events = []
    for sequence, row in enumerate(calculated["lines"], 1):
        source = data.lines[parse_uuid(row["receipt_line_id"])]
        line = scope.lines[source.order_line_id]
        entry = (
            append_change(
                execution,
                scope.stocks[line.product_id],
                kind="RECEIPT_CORRECTION",
                physical=row["after"]["P"],
                sequence=sequence,
            )
            if row["delta_p"]
            else None
        )
        values = {
            key + "_" + side: row[side][key]
            for key in ["conforming", "nonconforming", "regular", "surplus", "nonconformity"]
            for side in ["before", "after"]
        }
        ReceiptCorrectionLine.objects.create(
            correction=correction,
            line=source,
            entry=entry,
            source_grant_before=row["before"]["source_grant"],
            source_grant_after=row["after"]["source_grant"],
            **values,
        )
        line.received += row["delta_p"]
        line.save(update_fields=["received"])
        for item_kind, key, case_kind in [
            ("SURPLUS", "surplus", "SURPLUS"),
            ("NONCONFORMING", "nonconforming", "NONCONFORMITY"),
        ]:
            item = next(
                (
                    i
                    for i in data.items.values()
                    if i.receipt_line_id == source.pk and i.kind == item_kind
                ),
                None,
            )
            if not item and row["after"][key]:
                case = Case.objects.filter(receipt=receipt, kind=case_kind).first()
                if case is None:
                    case = new_case(execution, case_kind, receipt=receipt)
                    new_events.append(opened(case))
                item = QuarantineItem.objects.create(
                    case=case,
                    creation_operation_id=execution.operation_id,
                    kind=item_kind,
                    receipt_line=source,
                    quantity=getattr(source, key),
                )
            if item:
                case = data.cases.get(item.case_id) or Case.objects.select_related(
                    "origin_operation"
                ).get(pk=item.case_id)
                touched_cases[case.pk] = case
        consumption = getattr(source, "source_consumption", None)
        if consumption:
            item = data.items[consumption.item_id]
            touched_cases[item.case_id] = data.cases[item.case_id]
    receipt.version += 1
    receipt.save(update_fields=["version"])
    project_order(scope.order)
    events = [
        isolation_event(execution, case, "ISOLATION_CORRECTED", message=correction.message)
        for case in touched_cases.values()
    ]
    events.append(
        add_event(
            execution,
            data.case,
            "RECEIPT_CORRECTED",
            "RESOLVED",
            {
                "verification": True,
                "message": correction.message,
                "correction_id": str(correction.pk),
            },
        )
    )
    result = {
        "id": str(correction.pk),
        "receipt_id": str(receipt.pk),
        "order_id": str(scope.order.pk),
        "case_id": str(data.case.pk),
        "receipt_version": receipt.version,
        "order_version": scope.order.version,
        "lines": calculated["lines"],
    }
    return Effect(
        result,
        after=result,
        details=calculated["lines"],
        events=(
            *new_events,
            *events,
            Event(
                "purchase.receipt_corrected",
                scope.order.pk,
                scope.order.version,
                {"receipt_id": str(receipt.pk), "recipient_roles": ["ADMIN"]},
            ),
        ),
    )


def rejected(execution, data, error):
    if error.code != "STOCK_BELOW_RESERVED":
        return {}
    from apps.cases.services import open_stock_case

    return open_stock_case(
        execution,
        data.stocks,
        error.details["lines"],
        source_case_id=error.details["source_case_id"],
    )
