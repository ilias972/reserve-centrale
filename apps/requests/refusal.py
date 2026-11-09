"""Refus avant toute remise : libérer l'engagement, sans déclarer de manque."""

from django.utils.translation import gettext

from apps.core.commands import Command, Effect, Event
from apps.core.errors import BusinessError
from apps.core.payloads import invalid_input
from apps.core.policies import check_version
from apps.inventory.primitives import append_change

from .assignment import operational_scope
from .delivery_services import REPLENISHER, resources
from .models import RequestEvent, ReservationChange
from .selectors import request_data


def refuse(execution, resources, payload):
    if (
        set(payload) != {"expected_version", "message"}
        or not isinstance(payload["message"], str)
        or len(payload["message"]) > 2000
        or "\x00" in payload["message"]
    ):
        raise invalid_input()
    request, lines, stocks = resources
    check_version(request.version, payload["expected_version"])
    if request.assigned_to_id != execution.actor.pk:
        raise BusinessError(
            "NOT_ASSIGNED",
            gettext("Seul le préparateur assigné peut refuser cette demande."),
            status=403,
        )
    if request.status != "PREPARING" or request.first_delivery_operation_id:
        raise BusinessError(
            "REFUSAL_FORBIDDEN",
            gettext(
                "Le refus est possible uniquement avant la première remise confirmée. Après une remise, contactez la responsable."
            ),
        )
    before = request_data(request)
    request.version += 1
    event = RequestEvent.objects.create(
        request=request,
        operation_id=execution.operation_id,
        kind="REFUSED",
        revision=request.version,
        message=payload["message"].strip(),
    )
    for sequence, line in enumerate(lines, 1):
        reservation = line.reservation
        stock = stocks[line.product_id]
        entry = append_change(
            execution,
            stock,
            kind="RELEASE",
            physical=stock.balance.physical,
            delta_r=-reservation.remaining,
            sequence=sequence,
        )
        ReservationChange.objects.create(event=event, line=line, entry=entry)
        line.closed += reservation.remaining
        line.save(update_fields=["closed"])
        reservation.remaining = 0
        reservation.version += 1
        reservation.save(update_fields=["remaining", "version"])
    request.status = "REFUSED"
    request.closure_reason = "REFUSAL"
    request.save(update_fields=["status", "closure_reason", "version"])
    after = request_data(request)
    return Effect(
        after,
        before=before,
        after=after,
        details=[{"message": event.message}],
        events=(
            Event(
                "request.refused",
                request.pk,
                request.version + 1,
                {"request_id": str(request.pk), "recipient_id": str(request.assistant_id)},
            ),
        ),
    )


COMMANDS = [Command("requests.refuse", REPLENISHER, operational_scope, resources, refuse)]
