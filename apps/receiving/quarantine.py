"""Décisions Gestionnaire sur des quantités identifiées, sans acceptation partielle inventée."""

from django.utils.translation import gettext

from apps.cases.models import Case
from apps.cases.workflow import add_event
from apps.catalog.services import exact, quantity
from apps.core.commands import Command, Effect, Event
from apps.core.errors import BusinessError
from apps.core.payloads import invalid_input, parse_uuid
from apps.core.policies import check_version
from apps.inventory.primitives import append_change
from apps.purchasing.services import project_order

from . import locks
from .common import isolation_event, note
from .models import (
    ArrivalReport,
    OrderEvent,
    QuarantineDecision,
    QuarantineItem,
    Reinstatement,
    ReinstatementLine,
    UnknownLink,
    UnknownLinkLine,
)
from .state import item_state

N = frozenset({"ADMIN"})


def verified(payload):
    if payload.get("verification", payload.get("external_verification")) is not True:
        raise BusinessError(
            "VERIFICATION_REQUIRED",
            gettext("Confirmez la vérification avant de poursuivre."),
            status=400,
        )
    return note(payload)


def decide(kind):
    def apply(execution, resources, payload):
        scope, item, case = resources
        exact(
            payload,
            {"expected_version", "quantity", "verification", "message", "expected_stock_version"},
        )
        message = verified(payload)
        current = item_state(item.pk)
        check_version(current["version"], payload.get("expected_version"))
        q = quantity(payload.get("quantity"), zero=False)
        if item.kind != "SURPLUS" or q != current["remaining"] or current["refused"]:
            raise BusinessError(
                "FULL_REMAINING_REQUIRED",
                gettext("La décision porte sur tout le surplus encore isolé et non refusé."),
            )
        entry = None
        if kind == "ACCEPT":
            stock = scope.stocks[parse_uuid(current["product_id"])]
            check_version(stock.balance.version, payload.get("expected_stock_version"))
            if stock.balance.physical is None or stock.product.unit != current["unit"]:
                raise BusinessError(
                    "NOT_COUNTED_OR_UNIT_CHANGED",
                    gettext("Vérifiez l’inventaire et l’unité avant d’accepter ce surplus."),
                )
            entry = append_change(
                execution,
                stock,
                kind="SURPLUS_ACCEPT",
                physical=stock.balance.physical + q,
                sequence=1,
            )
        fact = QuarantineDecision.objects.create(
            operation_id=execution.operation_id,
            item=item,
            kind=kind,
            quantity=q,
            remaining_before=q,
            version_before=current["version"],
            verification=True,
            message=message,
            entry=entry,
        )
        ev = isolation_event(
            execution,
            case,
            "QUARANTINE_ACCEPTED" if kind == "ACCEPT" else "QUARANTINE_REFUSED",
            message=message,
        )
        after = item_state(item.pk)
        result = {
            "id": str(fact.pk),
            "item_id": str(item.pk),
            "case_id": str(case.pk),
            "state": after,
            "stock_changed": kind == "ACCEPT",
        }
        return Effect(
            result,
            before=current,
            after=result,
            events=(
                ev,
                Event(
                    "quarantine.decided",
                    item.pk,
                    after["version"],
                    {"case_id": str(case.pk), "recipient_roles": ["ADMIN"]},
                ),
            ),
        )

    return apply


def case_scope(actor, identifier):
    if not Case.objects.filter(
        pk=identifier,
        kind__in=["UNKNOWN_ORDER", "SURPLUS", "NONCONFORMITY", "LATE_CANCELLED_DELIVERY"],
    ).exists():
        raise BusinessError("NOT_FOUND", gettext("Dossier fournisseur introuvable."), status=404)


def case_supplier(actor, identifier):
    case = Case.objects.select_related("receipt__event", "arrival_report__order_link").get(
        pk=identifier
    )
    report = getattr(case, "arrival_report", None)
    order_id = (
        case.receipt.event.order_id
        if case.receipt_id
        else (locks.report_order(report) if report else None)
    )
    if order_id:
        locks.order_supplier(actor, order_id)


def case_resources(actor, identifier):
    initial = Case.objects.select_related("receipt__event", "arrival_report__order_link").get(
        pk=identifier
    )
    report = getattr(initial, "arrival_report", None)
    order_id = (
        initial.receipt.event.order_id
        if initial.receipt_id
        else (locks.report_order(report) if report else None)
    )
    order_scope = locks.order_scope(actor, order_id) if order_id else None
    if report:
        report = ArrivalReport.objects.select_for_update(of=("self",)).get(pk=report.pk)
    items = list(
        QuarantineItem.objects.select_for_update().filter(case_id=identifier).order_by("pk")
    )
    case = (
        Case.objects.select_for_update(of=("self",))
        .select_related("origin_operation")
        .get(pk=identifier)
    )
    return order_scope, report, items, case


def resolve(execution, resources, payload):
    _, report, items, case = resources
    exact(payload, {"expected_version", "item_versions", "external_verification", "message"})
    message = verified(payload)
    check_version(case.version, payload.get("expected_version"))
    if case.status == "RESOLVED":
        raise BusinessError("ALREADY_RESOLVED", gettext("Ce dossier a déjà un traitement vérifié."))
    versions = payload.get("item_versions", {})
    if not isinstance(versions, dict) or set(versions) != {str(item.pk) for item in items}:
        raise invalid_input()
    if (
        report
        and report.kind == "UNKNOWN_ORDER"
        and not UnknownLink.objects.filter(report=report).exists()
    ):
        QuarantineDecision.objects.create(
            operation_id=execution.operation_id,
            report=report,
            kind="EXTERNAL",
            version_before=case.version,
            verification=True,
            message=message,
        )
    else:
        states = [(item, item_state(item.pk)) for item in items]
        for item, current in states:
            check_version(current["version"], versions[str(item.pk)])
            if current["remaining"] and item.kind == "SURPLUS" and not current["refused"]:
                raise BusinessError(
                    "SURPLUS_DECISION_REQUIRED",
                    gettext(
                        "Acceptez ou refusez d’abord le surplus ; le traitement externe doit ensuite être vérifié."
                    ),
                )
        if not any(current["remaining"] for _, current in states):
            raise BusinessError(
                "TREATMENT_REQUIRED",
                gettext("Aucune marchandise en attente de traitement externe sur ce dossier."),
            )
        for item, current in states:
            if current["remaining"]:
                QuarantineDecision.objects.create(
                    operation_id=execution.operation_id,
                    item=item,
                    kind="EXTERNAL",
                    quantity=current["remaining"],
                    remaining_before=current["remaining"],
                    version_before=current["version"],
                    grant_released=current["grant_remaining"],
                    verification=True,
                    message=message,
                )
    ev = add_event(
        execution, case, "EXTERNAL_VERIFIED", "RESOLVED", {"verification": True, "message": message}
    )
    result = {
        "id": str(case.pk),
        "status": case.status,
        "version": case.version,
        "stock_changed": False,
    }
    return Effect(result, after=result, events=(ev,))


def link_supplier(actor, identifier, payload):
    locks.order_supplier(actor, parse_uuid(payload.get("order_id")))


def link_resources(actor, identifier, payload):
    scope = locks.order_scope(actor, parse_uuid(payload.get("order_id")))
    report = (
        ArrivalReport.objects.select_for_update(of=("self",))
        .filter(case_id=identifier, kind="UNKNOWN_ORDER")
        .first()
    )
    if report is None:
        raise BusinessError("NOT_FOUND", gettext("Colis non identifié introuvable."), status=404)
    case = (
        Case.objects.select_for_update(of=("self",))
        .select_related("origin_operation")
        .get(pk=identifier)
    )
    return scope, report, case


def link(execution, resources, payload):
    scope, report, case = resources
    exact(payload, {"order_id", "expected_version", "verification", "message", "lines"})
    message = verified(payload)
    check_version(case.version, payload.get("expected_version"))
    if case.status == "RESOLVED" or UnknownLink.objects.filter(report=report).exists():
        raise BusinessError(
            "PARCEL_ALREADY_LINKED",
            gettext(
                "Ce colis est déjà rattaché ou traité. Il ne peut pas être enregistré sous une autre commande."
            ),
        )
    rows = payload.get("lines")
    if not isinstance(rows, list) or not 1 <= len(rows) <= 100:
        raise invalid_input()
    selected = {}
    for row in rows:
        exact(row, {"order_line_id", "quantity"})
        lid = parse_uuid(row.get("order_line_id"))
        if lid not in scope.lines or lid in selected:
            raise invalid_input()
        selected[lid] = quantity(row.get("quantity"), zero=False)
    fact = UnknownLink.objects.create(
        report=report,
        order=scope.order,
        operation_id=execution.operation_id,
        verification=True,
        message=message,
    )
    for lid, q in selected.items():
        line = UnknownLinkLine.objects.create(link=fact, order_line=scope.lines[lid], quantity=q)
        QuarantineItem.objects.create(
            case=case,
            creation_operation_id=execution.operation_id,
            kind="UNKNOWN_ORDER",
            link_line=line,
            quantity=q,
        )
    ev = add_event(
        execution,
        case,
        "UNKNOWN_LINKED",
        "UNDER_REVIEW",
        {
            "verification": True,
            "message": message,
            "order_id": str(scope.order.pk),
            "supplier_declared": report.supplier_name,
            "supplier_verified": scope.order.supplier_name,
        },
    )
    result = {
        "id": str(report.pk),
        "case_id": str(case.pk),
        "order_id": str(scope.order.pk),
        "stock_changed": False,
    }
    return Effect(
        result,
        after=result,
        details=[
            {"order_line_id": str(lid), "verified_quantity": q} for lid, q in selected.items()
        ],
        events=(ev,),
    )


def reinstate(execution, scope, payload):
    exact(
        payload,
        {
            "expected_version",
            "report_id",
            "expected_report_version",
            "external_verification",
            "lines",
            "message",
        },
    )
    message = verified(payload)
    check_version(scope.order.version, payload.get("expected_version"))
    report = (
        ArrivalReport.objects.select_for_update(of=("self",))
        .select_related("order_link")
        .filter(pk=parse_uuid(payload.get("report_id")))
        .first()
    )
    if (
        report is None
        or report.kind not in {"LATE_CANCELLED_DELIVERY", "UNKNOWN_ORDER"}
        or locks.report_order(report) != scope.order.pk
    ):
        raise BusinessError(
            "ISOLATION_SOURCE_MISMATCH",
            gettext(
                "Choisissez le dossier des marchandises réellement isolées sur cette commande."
            ),
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
    check_version(case.version, payload.get("expected_report_version"))
    rows = payload.get("lines")
    if not isinstance(rows, list) or not 1 <= len(rows) <= 100:
        raise invalid_input()
    selected = {}
    for row in rows:
        exact(row, {"order_line_id", "quantity"})
        lid = parse_uuid(row.get("order_line_id"))
        q = quantity(row.get("quantity"), zero=False)
        if lid not in scope.lines or lid not in indexed or lid in selected:
            raise invalid_input()
        current = item_state(indexed[lid].pk)
        if q > scope.lines[lid].cancelled or q > current["remaining"] - current["grant_remaining"]:
            raise BusinessError(
                "REINSTATEMENT_EXCEEDED",
                gettext(
                    "Le rétablissement dépasse le reliquat annulé ou les unités isolées non déjà régularisées."
                ),
            )
        selected[lid] = q
    scope.order.version += 1
    event = OrderEvent.objects.create(
        order=scope.order,
        operation_id=execution.operation_id,
        revision=scope.order.version,
        kind="REINSTATE",
    )
    fact = Reinstatement.objects.create(event=event, report=report, external_verification=True)
    for lid, q in selected.items():
        line = scope.lines[lid]
        ReinstatementLine.objects.create(
            reinstatement=fact,
            line=line,
            item=indexed[lid],
            quantity=q,
            cancelled_before=line.cancelled,
            cancelled_after=line.cancelled - q,
        )
        line.cancelled -= q
        line.save(update_fields=["cancelled"])
    project_order(scope.order)
    ev = add_event(
        execution,
        case,
        "REMAINDER_REINSTATED",
        "UNDER_REVIEW",
        {"verification": True, "message": message, "reinstatement_id": str(fact.pk)},
    )
    result = {
        "id": str(fact.pk),
        "order_id": str(scope.order.pk),
        "version": scope.order.version,
        "report_id": str(report.pk),
        "case_id": str(case.pk),
        "stock_changed": False,
        "purchase_email_created": False,
    }
    return Effect(
        result,
        after=result,
        events=(
            ev,
            Event(
                "purchase.remainder_reinstated",
                scope.order.pk,
                scope.order.version,
                {"report_id": str(report.pk), "recipient_roles": ["ADMIN"]},
            ),
        ),
    )


COMMANDS = [
    Command(
        "quarantine.accept_surplus",
        N,
        lambda a, i: None,
        locks.item_scope,
        decide("ACCEPT"),
        lock_references=locks.item_supplier,
    ),
    Command(
        "quarantine.refuse",
        N,
        lambda a, i: None,
        locks.item_scope,
        decide("REFUSE"),
        lock_references=locks.item_supplier,
    ),
    Command(
        "receiving.case_resolve",
        N,
        case_scope,
        case_resources,
        resolve,
        lock_references=case_supplier,
    ),
    Command(
        "cases.link_order",
        N,
        case_scope,
        lambda a, i: None,
        link,
        lock_payload_references=link_supplier,
        lock_payload_resources=link_resources,
    ),
    Command(
        "orders.reinstate_remainder",
        N,
        locks.order_exists,
        locks.order_scope,
        reinstate,
        lock_references=locks.order_supplier,
    ),
]
