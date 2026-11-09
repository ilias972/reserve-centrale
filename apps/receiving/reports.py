"""Constats immuables hors stock ; aucune liaison ou déclaration ne crédite P."""

from django.utils.translation import gettext

from apps.cases.models import Case
from apps.catalog.models import Supplier
from apps.catalog.services import exact, quantity
from apps.core.commands import Command, Effect
from apps.core.errors import BusinessError
from apps.core.payloads import invalid_input, parse_uuid
from apps.core.policies import check_version
from apps.inventory.primitives import lock_stocks

from . import locks
from .common import new_case, note, opened, reference
from .models import ArrivalReport, QuarantineItem, Receipt, ReportLine

R = frozenset({"REPLENISHER"})


def unknown_rows(payload):
    rows = payload.get("lines", [])
    if not isinstance(rows, list) or len(rows) > 100:
        raise invalid_input()
    result, seen = [], set()
    for row in rows:
        exact(row, {"product_id", "quantity"})
        pid = parse_uuid(row["product_id"]) if row.get("product_id") is not None else None
        q = quantity(row["quantity"], zero=False) if row.get("quantity") is not None else None
        if pid is not None:
            if pid in seen:
                raise invalid_input()
            seen.add(pid)
        result.append((pid, q))
    return result


def optional_supplier(actor, identifier, payload):
    if payload.get("supplier_id") is not None:
        supplier = (
            Supplier.objects.select_for_update()
            .filter(pk=parse_uuid(payload["supplier_id"]))
            .first()
        )
        if supplier is None:
            raise BusinessError("NOT_FOUND", gettext("Fournisseur introuvable."), status=404)


def unknown_resources(actor, identifier, payload):
    if identifier is not None:
        raise invalid_input()
    rows = unknown_rows(payload)
    return rows, lock_stocks({pid for pid, _ in rows if pid is not None})


def unknown(execution, resources, payload):
    exact(payload, {"supplier_id", "lines", "message"})
    message = note(payload)
    supplier = (
        Supplier.objects.filter(pk=parse_uuid(payload["supplier_id"])).first()
        if payload.get("supplier_id") is not None
        else None
    )
    rows, stocks = resources
    case = new_case(execution, "UNKNOWN_ORDER")
    report = ArrivalReport.objects.create(
        reference=reference("report"),
        operation_id=execution.operation_id,
        case=case,
        kind="UNKNOWN_ORDER",
        supplier=supplier,
        supplier_name=supplier.name if supplier else "",
        message=message,
    )
    for position, (pid, q) in enumerate(rows, 1):
        product = stocks[pid].product if pid else None
        ReportLine.objects.create(
            report=report,
            position=position,
            product=product,
            product_name=product.name if product else "",
            unit=product.unit if product else "",
            quantity=q,
        )
    result = {
        "id": str(report.pk),
        "reference": report.reference,
        "case_id": str(case.pk),
        "stock_changed": False,
        "message": message,
        "quantities_identified": bool(rows),
    }
    return Effect(result, after=result, http_status=201, events=(opened(case),))


def order_report(kind):
    def apply(execution, scope, payload):
        exact(payload, {"expected_version", "lines", "message"})
        check_version(scope.order.version, payload.get("expected_version"))
        message = note(payload)
        rows = payload.get("lines")
        if not isinstance(rows, list) or not 1 <= len(rows) <= 100:
            raise invalid_input()
        selected = {}
        for row in rows:
            exact(row, {"order_line_id", "observed_quantity"})
            lid = parse_uuid(row.get("order_line_id"))
            q = quantity(row.get("observed_quantity"), zero=False)
            if lid not in scope.lines or lid in selected:
                raise invalid_input()
            line = scope.lines[lid]
            if kind == "LATE_CANCELLED_DELIVERY" and not line.cancelled:
                raise BusinessError(
                    "NO_CANCELLED_REMAINDER",
                    gettext("Cette ligne n’a aucun reliquat encore annulé."),
                )
            if kind == "SURPLUS" and (line.cancelled or line.waiting):
                raise BusinessError(
                    "REGULAR_OR_LATE_REQUIRED",
                    gettext(
                        "Recevez les quantités encore attendues ou signalez le reliquat annulé, sans les transformer en surplus."
                    ),
                )
            selected[lid] = q
        case = new_case(execution, kind)
        report = ArrivalReport.objects.create(
            reference=reference("report"),
            operation_id=execution.operation_id,
            case=case,
            kind=kind,
            order=scope.order,
            order_version=scope.order.version,
            supplier_id=scope.order.supplier_id,
            supplier_name=scope.order.supplier_name,
            message=message,
        )
        for position, (lid, q) in enumerate(selected.items(), 1):
            line = scope.lines[lid]
            fact = ReportLine.objects.create(
                report=report,
                position=position,
                order_line=line,
                product=line.product,
                product_name=line.product_name,
                unit=line.unit,
                quantity=q,
            )
            QuarantineItem.objects.create(
                case=case,
                creation_operation_id=execution.operation_id,
                kind="SURPLUS" if kind == "SURPLUS" else "LATE_CANCELLED",
                report_line=fact,
                quantity=q,
            )
        result = {
            "id": str(report.pk),
            "reference": report.reference,
            "case_id": str(case.pk),
            "order_id": str(scope.order.pk),
            "stock_changed": False,
            "message": message,
        }
        return Effect(result, after=result, http_status=201, events=(opened(case),))

    return apply


def error_scope(actor, identifier):
    row = Receipt.objects.select_related("event__operation").filter(pk=identifier).first()
    if row is None or (actor.role == "REPLENISHER" and row.event.operation.actor_id != actor.pk):
        raise BusinessError(
            "NOT_FOUND", gettext("Réception introuvable dans votre activité."), status=404
        )


def error_resources(actor, identifier):
    return (
        Receipt.objects.select_for_update(of=("self",))
        .select_related("event__operation")
        .get(pk=identifier)
    )


def report_error(execution, receipt, payload):
    exact(payload, {"message"})
    return create_error_case(execution, receipt, note(payload))


def create_error_case(execution, receipt, message):
    if Case.objects.filter(receipt=receipt, kind="RECEIPT_ERROR").exists():
        raise BusinessError(
            "ALREADY_REPORTED", gettext("Un dossier de vérification existe déjà pour ce reçu.")
        )
    case = new_case(execution, "RECEIPT_ERROR", receipt=receipt)
    result = {
        "id": str(case.pk),
        "receipt_id": str(receipt.pk),
        "message": message,
        "stock_changed": False,
    }
    return Effect(result, after=result, http_status=201, events=(opened(case),))


COMMANDS = [
    Command(
        "receiving.unknown",
        R,
        lambda a, i: None,
        lambda a, i: None,
        unknown,
        lock_payload_references=optional_supplier,
        lock_payload_resources=unknown_resources,
    ),
    Command(
        "receiving.late",
        R,
        locks.order_exists,
        locks.order_scope,
        order_report("LATE_CANCELLED_DELIVERY"),
        lock_references=locks.order_supplier,
    ),
    Command(
        "receiving.extra",
        R,
        locks.order_exists,
        locks.order_scope,
        order_report("SURPLUS"),
        lock_references=locks.order_supplier,
    ),
    Command("receipts.report_error", R, error_scope, error_resources, report_error),
]
