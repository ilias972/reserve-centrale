"""Arrivées confirmées une seule fois par M0 ; seul inventory modifie P/R."""

from django.utils.translation import gettext

from apps.cases.models import Case
from apps.catalog.services import exact, quantity
from apps.core import commands
from apps.core.commands import Command, Effect, Event
from apps.core.errors import BusinessError
from apps.core.payloads import invalid_input, parse_uuid
from apps.core.policies import check_version
from apps.inventory.primitives import append_change
from apps.purchasing.services import project_order

from . import locks
from .common import isolation_event, new_case, opened, reference
from .models import (
    ArrivalReport,
    ManualReminder,
    OrderEvent,
    QuarantineItem,
    Receipt,
    ReceiptLine,
    SourceConsumption,
)
from .state import NONCONFORMITIES, item_state

R = frozenset({"REPLENISHER"})
N = frozenset({"ADMIN"})


def parse_arrival(payload, scope):
    exact(
        payload,
        {
            "expected_version",
            "lines",
            "unknown_delivery_id",
            "late_delivery_report_id",
            "expected_source_version",
        },
    )
    check_version(scope.order.version, payload.get("expected_version"))
    rows = payload.get("lines")
    if not isinstance(rows, list) or not 1 <= len(rows) <= 100:
        raise invalid_input()
    result, seen = [], set()
    for row in rows:
        exact(
            row,
            {
                "order_line_id",
                "conforming",
                "nonconforming",
                "nonconformity",
                "expected_stock_version",
            },
        )
        lid = parse_uuid(row.get("order_line_id"))
        if lid in seen or lid not in scope.lines:
            raise invalid_input()
        seen.add(lid)
        c, nc = quantity(row.get("conforming")), quantity(row.get("nonconforming"))
        kind = row.get("nonconformity", "")
        if (nc and kind not in NONCONFORMITIES) or (not nc and kind != "") or c + nc > 1000000000:
            raise invalid_input()
        line = scope.lines[lid]
        stock = scope.stocks[line.product_id]
        if not c + nc:
            continue
        check_version(stock.balance.version, row.get("expected_stock_version"))
        if c > line.waiting and line.cancelled:
            raise BusinessError(
                "LATE_CANCELLED_DELIVERY",
                gettext(
                    "Une partie est encore annulée. Recevez séparément les quantités valablement attendues et signalez la livraison du reliquat annulé."
                ),
                details={
                    "order_line_id": str(lid),
                    "waiting": line.waiting,
                    "cancelled": line.cancelled,
                },
            )
        regular = min(c, line.waiting)
        if regular and (stock.balance.physical is None or stock.product.unit != line.unit):
            raise BusinessError(
                "NOT_COUNTED_OR_UNIT_CHANGED",
                gettext(
                    "Vérifiez l’inventaire initial et l’unité du produit avant son entrée en réserve."
                ),
                details={"order_line_id": str(lid)},
            )
        if regular and stock.balance.physical + regular > 1000000000:
            raise invalid_input()
        result.append(
            {
                "line": line,
                "conforming": c,
                "nonconforming": nc,
                "regular": regular,
                "surplus": c - regular,
                "nonconformity": kind,
            }
        )
    if not result:
        raise BusinessError(
            "EMPTY_ARRIVAL",
            gettext("Saisissez au moins une quantité constatée pour cette arrivée."),
            status=400,
        )
    return result


def source_items(payload, scope, rows):
    unknown, late = payload.get("unknown_delivery_id"), payload.get("late_delivery_report_id")
    if unknown and late:
        raise invalid_input()
    if not (unknown or late):
        if payload.get("expected_source_version") is not None:
            raise invalid_input()
        return None, {}, None
    report = (
        ArrivalReport.objects.select_for_update(of=("self",))
        .select_related("order_link")
        .filter(pk=parse_uuid(unknown or late))
        .first()
    )
    if (
        report is None
        or report.kind != ("UNKNOWN_ORDER" if unknown else "LATE_CANCELLED_DELIVERY")
        or locks.report_order(report) != scope.order.pk
    ):
        raise BusinessError(
            "ISOLATION_SOURCE_MISMATCH",
            gettext("Ce colis isolé ne correspond pas à cette commande."),
        )
    items = list(
        QuarantineItem.objects.select_for_update().filter(case_id=report.case_id).order_by("pk")
    )
    indexed = {parse_uuid(item_state(item.pk)["order_line_id"]): item for item in items}
    case = (
        Case.objects.select_for_update(of=("self",))
        .select_related("origin_operation")
        .get(pk=report.case_id)
    )
    check_version(case.version, payload.get("expected_source_version"))
    for row in rows:
        item = indexed.get(row["line"].pk)
        current = item_state(item.pk) if item else None
        observed = row["conforming"] + row["nonconforming"]
        if current is None or current["refused"] or observed > current["remaining"]:
            raise BusinessError(
                "ISOLATION_EXCEEDED",
                gettext("La réception dépasse les marchandises encore isolées sur ce dossier."),
            )
        if late and (observed > current["grant_remaining"] or row["surplus"]):
            raise BusinessError(
                "REINSTATEMENT_REQUIRED",
                gettext(
                    "Ces unités doivent d’abord être rétablies explicitement par la responsable."
                ),
            )
    return report, indexed, case


def receive(execution, scope, payload):
    if scope.order.status not in {"WAITING", "PARTIAL"}:
        raise BusinessError(
            "ORDER_CLOSED",
            gettext(
                "Cette commande est close. Utilisez le signalement d’une arrivée supplémentaire ou du reliquat annulé."
            ),
        )
    rows = parse_arrival(payload, scope)
    report, sources, source_case = source_items(payload, scope, rows)
    scope.order.version += 1
    event = OrderEvent.objects.create(
        order=scope.order,
        operation_id=execution.operation_id,
        revision=scope.order.version,
        kind="RECEIPT",
    )
    receipt = Receipt.objects.create(
        reference=reference("receipt"), event=event, source_report=report
    )
    cases, entries = {}, []
    for seq, row in enumerate(rows, 1):
        line = row["line"]
        stock = scope.stocks[line.product_id]
        entry = (
            append_change(
                execution,
                stock,
                kind="RECEIPT_REGULAR",
                physical=stock.balance.physical + row["regular"],
                sequence=seq,
            )
            if row["regular"]
            else None
        )
        fact = ReceiptLine.objects.create(
            receipt=receipt,
            order_line=line,
            waiting_before=line.waiting,
            entry=entry,
            **{
                key: row[key]
                for key in ["conforming", "nonconforming", "regular", "surplus", "nonconformity"]
            },
        )
        if report:
            SourceConsumption.objects.create(
                line=fact,
                item=sources[line.pk],
                quantity=row["conforming"] + row["nonconforming"],
                reinstated_quantity=min(
                    row["conforming"] + row["nonconforming"],
                    item_state(sources[line.pk].pk)["grant_remaining"],
                ),
            )
        line.received += row["regular"]
        line.save(update_fields=["received"])
        for kind, value, case_kind in [
            ("SURPLUS", row["surplus"], "SURPLUS"),
            ("NONCONFORMING", row["nonconforming"], "NONCONFORMITY"),
        ]:
            if value:
                if case_kind not in cases:
                    cases[case_kind] = new_case(execution, case_kind, receipt=receipt)
                QuarantineItem.objects.create(
                    case=cases[case_kind],
                    creation_operation_id=execution.operation_id,
                    kind=kind,
                    receipt_line=fact,
                    quantity=value,
                )
        entries.append(
            {
                "order_line_id": str(line.pk),
                "product_id": str(line.product_id),
                "regular": row["regular"],
                "surplus": row["surplus"],
                "nonconforming": row["nonconforming"],
                "entry_id": str(entry.pk) if entry else None,
                "before": {
                    "P": entry.physical_before if entry else stock.balance.physical,
                    "R": stock.balance.reserved,
                    "F": line.received - row["regular"],
                    "A": line.cancelled,
                    "W": fact.waiting_before,
                },
                "after": {
                    "P": stock.balance.physical,
                    "R": stock.balance.reserved,
                    "F": line.received,
                    "A": line.cancelled,
                    "W": line.waiting,
                },
            }
        )
    project_order(scope.order)
    events = [opened(case) for case in cases.values()]
    if source_case:
        events.append(
            isolation_event(
                execution,
                source_case,
                "ISOLATION_CONSUMED",
                message="Marchandises identifiées traitées par cette réception ; seules les unités régulières sont entrées en réserve.",
            )
        )
    result = {
        "id": str(receipt.pk),
        "reference": receipt.reference,
        "order_id": str(scope.order.pk),
        "order_version": scope.order.version,
        "order_status": scope.order.status,
        "lines": entries,
        "cases": [str(c.pk) for c in cases.values()],
    }
    return Effect(
        result,
        before={
            "order_id": str(scope.order.pk),
            "version": scope.order.version - 1,
            "lines": [row["before"] for row in entries],
        },
        after=result,
        details=entries,
        http_status=201,
        events=(
            *events,
            Event(
                "purchase.received",
                scope.order.pk,
                scope.order.version,
                {"receipt_id": str(receipt.pk), "recipient_roles": ["ADMIN"]},
            ),
        ),
    )


def reminder(execution, scope, payload):
    exact(payload, {"expected_last_reminder"})
    if scope.order.status != "PARTIAL":
        raise BusinessError(
            "NOT_PARTIAL",
            gettext("Le rappel concerne une commande partiellement reçue avec un reste attendu."),
        )
    previous = (
        ManualReminder.objects.filter(order=scope.order)
        .select_related("operation")
        .order_by("-operation__confirmed_at", "-pk")
        .first()
    )
    expected = payload.get("expected_last_reminder")
    if (str(previous.pk) if previous else None) != expected:
        raise BusinessError(
            "STALE_REMINDER",
            gettext(
                "Un rappel a déjà été enregistré. Consultez son auteur et sa date avant un nouveau rappel."
            ),
        )
    fact = ManualReminder.objects.create(
        order=scope.order, operation_id=execution.operation_id, previous=previous
    )
    result = {
        "id": str(fact.pk),
        "order_id": str(scope.order.pk),
        "author": execution.actor.display_name,
        "stock_changed": False,
        "order_changed": False,
    }
    return Effect(
        result,
        after=result,
        events=(
            Event(
                "purchase.manual_reminder",
                scope.order.pk,
                ManualReminder.objects.filter(order=scope.order).count(),
                {"reminder_id": str(fact.pk), "recipient_roles": ["ADMIN"]},
            ),
        ),
    )


COMMANDS = [
    Command(
        "receipts.create",
        R,
        locks.order_exists,
        locks.order_scope,
        receive,
        lock_references=locks.order_supplier,
    ),
    Command(
        "orders.remind",
        R,
        locks.order_exists,
        locks.order_scope,
        reminder,
        lock_references=locks.order_supplier,
    ),
]


def ensure_registered():
    from .quarantine import COMMANDS as QUARANTINE_COMMANDS
    from .reports import COMMANDS as REPORT_COMMANDS

    for command in [*COMMANDS, *REPORT_COMMANDS, *QUARANTINE_COMMANDS]:
        if command.name not in commands._registry:
            commands.register_command(command)
