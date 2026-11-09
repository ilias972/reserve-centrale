"""M5 : attribution et faits de remise, registre M3 unique, aucune correction M6."""

from django.utils.translation import gettext

from apps.accounts.models import User
from apps.core.commands import Command, Effect, Event
from apps.core.errors import BusinessError
from apps.core.payloads import invalid_input, parse_uuid
from apps.core.policies import check_version
from apps.inventory.primitives import append_change, lock_stocks

from .assignment import lock_request, operational_scope
from .models import (
    Delivery,
    DeliveryIssue,
    DeliveryLine,
    RequestEvent,
    ReservationChange,
)
from .states import delivered_state

REPLENISHER = frozenset({User.Role.REPLENISHER})


def claim(execution, request, payload):
    if set(payload) != {"expected_version"}:
        raise invalid_input()
    check_version(request.version, payload["expected_version"])
    if request.status != "NEW" or request.assigned_to_id:
        raise BusinessError(
            "ALREADY_ASSIGNED",
            gettext("Cette demande est déjà prise en charge ou n'est plus disponible."),
        )
    request.assigned_to = execution.actor
    request.status = "PREPARING"
    request.version += 1
    request.save(update_fields=["assigned_to", "status", "version"])
    RequestEvent.objects.create(
        request=request,
        operation_id=execution.operation_id,
        kind="CLAIMED",
        revision=request.version,
    )
    return effect(request, "request.claimed")


def resources(actor, identifier):
    request = lock_request(actor, identifier)
    lines = list(request.lines.select_related("reservation").order_by("product_id"))
    return request, lines, lock_stocks({line.product_id for line in lines})


def delivery_payload(payload, lines):
    if (
        set(payload) != {"expected_version", "mode", "lines"}
        or payload["mode"] not in Delivery.Mode.values
        or not isinstance(payload["lines"], list)
        or not 1 <= len(payload["lines"]) <= 100
    ):
        raise invalid_input()
    rows = {}
    allowed = {line.pk for line in lines}
    for row in payload["lines"]:
        if not isinstance(row, dict) or set(row) != {
            "request_line_id",
            "delivered_quantity",
            "missing_quantity",
        }:
            raise invalid_input()
        identifier = parse_uuid(row["request_line_id"])
        if identifier not in allowed or identifier in rows:
            raise invalid_input()
        y, m = row["delivered_quantity"], row["missing_quantity"]
        if any(type(q) is not int or not 0 <= q <= 1000000000 for q in (y, m)):
            raise invalid_input()
        rows[identifier] = (y, m)
    if payload["mode"] == "SHORTAGE" and set(rows) != allowed:
        raise BusinessError(
            "EXPLICIT_REMAINDERS_REQUIRED",
            gettext("Présentez explicitement toutes les lignes avant une clôture pour manque."),
            status=400,
        )
    return rows


def deliver(execution, resources, payload):
    request, lines, stocks = resources
    rows = delivery_payload(payload, lines)
    check_version(request.version, payload["expected_version"])
    if request.assigned_to_id != execution.actor.pk:
        raise BusinessError(
            "NOT_ASSIGNED",
            gettext(
                "Seul le préparateur ayant pris en charge cette demande peut confirmer la remise."
            ),
            status=403,
        )
    if request.status not in {"PREPARING", "PARTIAL_OPEN"}:
        raise BusinessError(
            "INVALID_STATE", gettext("Cette demande n'est plus ouverte. Rechargez son détail.")
        )
    mode = payload["mode"]
    totals = [0, 0]
    for line in lines:
        y, m = rows.get(line.pk, (0, 0))
        u = line.reservation.remaining
        if y + m > u:
            raise BusinessError(
                "QUANTITY_EXCEEDS_REMAINDER",
                gettext("La quantité remise ou manquante dépasse le restant de la demande."),
            )
        if (mode == "COMPLETE" and (y != u or m)) or (mode == "LATER" and m):
            raise BusinessError(
                "INVALID_DELIVERY_MODE",
                gettext("Vérifiez les quantités et le traitement du reliquat."),
            )
        if mode == "SHORTAGE" and m != u - y:
            raise BusinessError(
                "MIXED_REMAINDERS_NOT_SUPPORTED",
                gettext(
                    "Ce parcours clôture tous les reliquats explicitement affichés pour manque. Pour en conserver, choisissez À apporter plus tard ; le traitement mixte reste à préciser."
                ),
            )
        totals[0] += y
        totals[1] += m
    if not totals[0] and not (mode == "SHORTAGE" and totals[1]):
        raise BusinessError(
            "EMPTY_DELIVERY",
            gettext("Indiquez au moins une quantité réellement remise ou manquante."),
            status=400,
        )
    if mode == "SHORTAGE" and not totals[1]:
        raise BusinessError(
            "INVALID_DELIVERY_MODE",
            gettext("Aucun manque indiqué : utilisez Remise complète."),
            status=400,
        )
    from .selectors import request_data

    before = request_data(request)
    request.version += 1
    event = RequestEvent.objects.create(
        request=request,
        operation_id=execution.operation_id,
        kind="DELIVERED",
        revision=request.version,
    )
    delivery = Delivery.objects.create(
        request=request,
        operation_id=execution.operation_id,
        event=event,
        operator=execution.actor,
        operator_name=execution.actor.display_name,
        mode=mode,
    )
    sequence = 0
    for line in lines:
        y, m = rows.get(line.pk, (0, 0))
        stock = stocks[line.product_id]
        DeliveryLine.objects.create(
            delivery=delivery,
            line=line,
            remaining_before=line.reservation.remaining,
            handed=y,
            missing=m,
        )
        for kind, dp, dr in [("DELIVER", -y, -y), ("RELEASE", 0, -m), ("SHORTAGE", -m, 0)]:
            if not dp and not dr:
                continue
            sequence += 1
            entry = append_change(
                execution,
                stock,
                kind=kind,
                physical=stock.balance.physical + dp,
                delta_r=dr,
                sequence=sequence,
            )
            ReservationChange.objects.create(event=event, line=line, entry=entry)
        if y or m:
            line.delivered += y
            line.closed += m
            line.save(update_fields=["delivered", "closed"])
            line.reservation.remaining -= y + m
            line.reservation.version += 1
            line.reservation.save(update_fields=["remaining", "version"])
    request.status = delivered_state(lines)
    request.closure_reason = (
        (request.closure_reason or "SHORTAGE")
        if any(line_item.closed for line_item in lines)
        else ""
    )
    if not request.first_delivery_operation_id:
        request.first_delivery_operation_id = execution.operation_id
    request.save(update_fields=["status", "closure_reason", "first_delivery_operation", "version"])
    extra_events = []
    issue_id = None
    if totals[1]:
        issue = DeliveryIssue.objects.create(
            delivery=delivery, operation_id=execution.operation_id, kind="SHORTAGE"
        )
        from apps.cases.workflow import delivery_projection

        delivery_projection(issue)
        issue_id = str(issue.pk)
        extra_events.append(
            Event(
                "delivery.shortage.reported",
                issue.pk,
                1,
                {"issue_id": issue_id, "recipient_role": "ADMIN"},
            )
        )
    result = effect(request, "request.delivered", extra_events)
    return Effect(
        {**result.resource, "delivery_id": str(delivery.pk), "shortage_issue_id": issue_id},
        before=before,
        after=result.after,
        details=[
            {
                "line_id": str(line.pk),
                "handed": rows.get(line.pk, (0, 0))[0],
                "missing": rows.get(line.pk, (0, 0))[1],
            }
            for line in lines
        ],
        events=result.events,
        http_status=201,
    )


def effect(request, event_type, extra_events=()):
    from .selectors import request_data

    result = request_data(request)
    return Effect(
        result,
        after=result,
        events=(
            Event(
                event_type,
                request.pk,
                request.version + 1,
                {"request_id": str(request.pk), "recipient_id": str(request.assistant_id)},
            ),
            *extra_events,
        ),
    )


def report_scope(actor, identifier):
    if not Delivery.objects.filter(pk=identifier, operator=actor).exists():
        raise BusinessError(
            "NOT_FOUND", gettext("Livraison introuvable dans votre activité."), status=404
        )


def report_resources(actor, identifier):
    return Delivery.objects.select_for_update().get(pk=identifier, operator=actor)


def report_error(execution, delivery, payload):
    if (
        set(payload) != {"message"}
        or not isinstance(payload["message"], str)
        or len(payload["message"].strip()) > 2000
    ):
        raise invalid_input()
    # Une seule erreur ouverte par remise ; un autre constat ne réécrit pas la déclaration.
    from apps.cases.models import Case

    if (
        delivery.issues.filter(kind="DELIVERY_ERROR").exists()
        or Case.objects.filter(delivery=delivery, kind="DELIVERY_ERROR").exists()
    ):
        raise BusinessError(
            "ALREADY_REPORTED",
            gettext(
                "Une erreur a déjà été signalée pour cette remise. La gestionnaire doit la vérifier."
            ),
        )
    issue = DeliveryIssue.objects.create(
        delivery=delivery,
        operation_id=execution.operation_id,
        kind="DELIVERY_ERROR",
        message=payload["message"].strip(),
    )
    from apps.cases.workflow import delivery_projection

    delivery_projection(issue)
    result = {
        "id": str(issue.pk),
        "delivery_id": str(delivery.pk),
        "status": "OPEN",
        "original_unchanged": True,
    }
    return Effect(
        result,
        after=result,
        events=(
            Event(
                "delivery.error.reported",
                issue.pk,
                1,
                {"issue_id": str(issue.pk), "recipient_role": "ADMIN"},
            ),
        ),
        http_status=201,
    )


COMMANDS = [
    Command("requests.claim", REPLENISHER, operational_scope, lock_request, claim),
    Command("requests.deliver", REPLENISHER, operational_scope, resources, deliver),
    Command("deliveries.report_error", REPLENISHER, report_scope, report_resources, report_error),
]
