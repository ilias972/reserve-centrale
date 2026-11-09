"""Traitement explicite des dossiers ; déclarations originales jamais modifiées."""

from django.utils.translation import gettext

from apps.core.commands import Command, Effect, Event
from apps.core.errors import BusinessError
from apps.core.payloads import invalid_input
from apps.core.policies import check_version

from .models import Case, CaseEvent


def stock_projection(source):
    return Case.objects.create(
        id=source.pk,
        stock_case=source,
        origin_operation_id=source.operation_id,
        kind="STOCK_CONFLICT",
    )


def delivery_projection(source):
    return Case.objects.create(
        id=source.pk,
        delivery_id=source.delivery_id,
        delivery_issue=source,
        origin_operation_id=source.operation_id,
        kind=source.kind,
    )


def scope(actor, identifier):
    if not Case.objects.filter(pk=identifier).exists():
        raise BusinessError("NOT_FOUND", gettext("Dossier introuvable."), status=404)


def reviewed(payload):
    if payload.get("verification") is not True:
        raise BusinessError(
            "VERIFICATION_REQUIRED",
            gettext("Confirmez la vérification avant de poursuivre."),
            status=400,
        )
    message = payload.get("message", "")
    if not isinstance(message, str) or not 1 <= len(message.strip()) <= 2000:
        raise BusinessError(
            "VERIFICATION_REQUIRED",
            gettext("Décrivez brièvement la vérification effectuée."),
            status=400,
        )
    return message.strip()


def add_event(execution, case, kind, status, details):
    case.version += 1
    CaseEvent.objects.create(
        case=case,
        operation_id=execution.operation_id,
        revision=case.version,
        kind=kind,
        status=status,
        details=details,
    )
    case.status = status
    case.save(update_fields=["version", "status"])
    return Event(
        "case.updated",
        case.pk,
        case.version,
        {"case_id": str(case.pk), "recipient_id": str(case.origin_operation.actor_id)},
    )


def review(execution, case, payload):
    if set(payload) != {"expected_version", "verification", "message"}:
        raise invalid_input()
    check_version(case.version, payload["expected_version"])
    message = reviewed(payload)
    from apps.receiving.state import CASE_KINDS

    if case.kind in CASE_KINDS and case.status == "RESOLVED":
        raise BusinessError(
            "NEW_REPORT_REQUIRED",
            gettext(
                "Le traitement résolu reste conservé. Ouvrez une nouvelle vérification de la source si nécessaire."
            ),
        )
    if case.kind == "STOCK_CONFLICT" and case.status == "RESOLVED":
        raise BusinessError(
            "NEW_REPORT_REQUIRED",
            gettext(
                "Un nouveau constat de stock nécessite un nouveau signalement ; le dossier résolu reste conservé."
            ),
        )
    event = add_event(
        execution, case, "REVIEWED", "UNDER_REVIEW", {"verification": True, "message": message}
    )
    result = {"id": str(case.pk), "status": case.status, "version": case.version}
    return Effect(result, after=result, events=(event,))


COMMANDS = [
    Command(
        "cases.review",
        frozenset({"ADMIN"}),
        scope,
        lambda a, i: (
            Case.objects.select_for_update(of=("self",))
            .select_related("origin_operation")
            .get(pk=i)
        ),
        review,
    )
]


def open_case(execution, source, payload):

    if payload.get("kind") == "RECEIPT_ERROR":
        from apps.receiving.reports import create_error_case

        if set(payload) != {"kind", "source_id", "verification", "message"}:
            raise invalid_input()
        return create_error_case(execution, source, reviewed(payload))

    if (
        set(payload) != {"kind", "source_id", "verification", "message"}
        or payload["kind"] != "DELIVERY_ERROR"
    ):
        raise invalid_input()
    reviewed(payload)
    delivery = source
    if delivery is None:
        raise BusinessError("NOT_FOUND", gettext("Livraison introuvable."), status=404)
    if Case.objects.filter(delivery=delivery, kind="DELIVERY_ERROR").exists():
        raise BusinessError(
            "ALREADY_REPORTED", gettext("Un dossier existe déjà pour cette livraison.")
        )
    case = Case.objects.create(
        delivery=delivery, origin_operation_id=execution.operation_id, kind="DELIVERY_ERROR"
    )
    result = {"id": str(case.pk), "status": "OPEN", "version": 0}
    return Effect(
        result,
        after={**result, "verification": True, "message": payload["message"]},
        events=(
            Event("case.opened", case.pk, 1, {"case_id": str(case.pk), "recipient_role": "ADMIN"}),
        ),
    )


def resolve_case(execution, case, payload):
    if set(payload) != {"expected_version", "verification", "message", "product_counts"}:
        raise invalid_input()
    check_version(case.version, payload["expected_version"])
    message = reviewed(payload)
    if case.kind != "SHORTAGE" or case.status == "RESOLVED":
        raise BusinessError(
            "TREATMENT_REQUIRED",
            gettext(
                "Une correction ou une régularisation effective est nécessaire pour résoudre ce dossier."
            ),
        )
    # Les stocks ont été verrouillés avant le dossier par resolve_resources.
    from apps.inventory.models import StockBalance

    rows = payload["product_counts"]
    expected = set(case.delivery.lines.values_list("line__product_id", flat=True))
    from apps.core.payloads import parse_uuid

    if (
        not isinstance(rows, list)
        or {parse_uuid(r.get("product_id")) for r in rows if isinstance(r, dict)} != expected
        or len(rows) != len(expected)
    ):
        raise invalid_input()
    from .models import StockHold

    if StockHold.objects.filter(product_id__in=expected).exists():
        raise BusinessError(
            "TREATMENT_REQUIRED", gettext("Un conflit de stock reste à régulariser.")
        )
    for row in rows:
        b = StockBalance.objects.get(product_id=parse_uuid(row["product_id"]))
        check_version(b.version, row.get("expected_stock_version"))
        if type(row.get("counted_quantity")) is not int or row["counted_quantity"] != b.physical:
            raise BusinessError(
                "TREATMENT_REQUIRED",
                gettext("Le comptage diffère du stock enregistré. Régularisez d'abord cet écart."),
            )
    ev = add_event(
        execution,
        case,
        "SHORTAGE_VERIFIED",
        "RESOLVED",
        {"verification": True, "message": message, "product_counts": rows},
    )
    result = {"id": str(case.pk), "status": case.status, "version": case.version}
    return Effect(result, after=result, events=(ev,))


def resolve_resources(actor, identifier):
    from apps.inventory.primitives import lock_stocks

    case = Case.objects.get(pk=identifier)
    if case.delivery_id:
        lock_stocks(set(case.delivery.lines.values_list("line__product_id", flat=True)))
    return (
        Case.objects.select_for_update(of=("self",))
        .select_related("origin_operation", "delivery")
        .get(pk=identifier)
    )


def open_resources(actor, identifier, payload):
    from apps.core.payloads import parse_uuid
    from apps.requests.models import Delivery

    if identifier is not None:
        raise invalid_input()
    if payload.get("kind") == "RECEIPT_ERROR":
        from apps.receiving.models import Receipt

        row = (
            Receipt.objects.select_for_update()
            .filter(pk=parse_uuid(payload.get("source_id")))
            .first()
        )
        if row is None:
            raise BusinessError("NOT_FOUND", gettext("Réception introuvable."), status=404)
        return row
    row = (
        Delivery.objects.select_for_update().filter(pk=parse_uuid(payload.get("source_id"))).first()
    )
    if row is None:
        raise BusinessError("NOT_FOUND", gettext("Livraison introuvable."), status=404)
    return row


COMMANDS.extend(
    [
        Command(
            "cases.open",
            frozenset({"ADMIN"}),
            lambda a, i: None,
            lambda a, i: None,
            open_case,
            lock_payload_resources=open_resources,
        ),
        Command("cases.resolve", frozenset({"ADMIN"}), scope, resolve_resources, resolve_case),
    ]
)
