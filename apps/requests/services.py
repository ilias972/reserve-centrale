"""M4 : toutes les réservations passent par M0 et le registre M3."""

from django.db import connection
from django.utils.translation import gettext

from apps.accounts.models import REQUESTER_ROLES
from apps.catalog.models import Floor, FloorProduct
from apps.core import commands
from apps.core.commands import Command, Effect, Event
from apps.core.errors import BusinessError
from apps.core.payloads import invalid_input, parse_uuid
from apps.core.policies import check_version
from apps.inventory.primitives import append_change, lock_stocks

from .models import InternalRequest, RequestEvent, RequestLine, Reservation, ReservationChange

REQUESTERS = REQUESTER_ROLES


def create_scope(actor, identifier):
    if identifier is not None:
        raise invalid_input()


def own_request(actor, identifier):
    if not InternalRequest.objects.filter(pk=identifier, assistant=actor).exists():
        raise BusinessError("NOT_FOUND", gettext("Demande introuvable."), status=404)


def parse_lines(payload):
    if (
        not isinstance(payload, dict)
        or set(payload) != {"lines"}
        or not isinstance(payload["lines"], list)
        or not 1 <= len(payload["lines"]) <= 100
    ):
        raise BusinessError(
            "INVALID_REQUEST", gettext("Sélectionnez de 1 à 100 produits."), status=400
        )
    result = []
    seen = set()
    for row in payload["lines"]:
        if not isinstance(row, dict) or set(row) != {"product_id", "quantity"}:
            raise invalid_input()
        identifier = parse_uuid(row["product_id"])
        if identifier in seen:
            raise BusinessError(
                "DUPLICATE_PRODUCT", gettext("Un produit apparaît deux fois."), status=400
            )
        seen.add(identifier)
        if type(row["quantity"]) is not int or not 1 <= row["quantity"] <= 1000000000:
            raise BusinessError(
                "INVALID_QUANTITY",
                gettext("La quantité doit être un entier strictement positif."),
                status=400,
            )
        result.append({"product_id": identifier, "quantity": row["quantity"]})
    return result


def lock_floor(actor, identifier):
    # Même ordre que M2 : unité avant intention et produits. FOR SHARE exclut
    # les modifications d'unité/droits (FOR UPDATE), sans sérialiser les
    # demandes de produits indépendants. Les produits/soldes restent exclusifs.
    if actor.floor_id is None:
        raise BusinessError("FLOOR_UNAVAILABLE", gettext("Votre compte nécessite un unité actif."))
    with connection.cursor() as cursor:
        cursor.execute("SELECT id FROM catalog_floor WHERE id = %s FOR SHARE", [actor.floor_id])
        if cursor.fetchone() is None:
            raise Floor.DoesNotExist


def create_resources(actor, identifier, payload):
    rows = parse_lines(payload)
    return rows, lock_stocks({row["product_id"] for row in rows})


def create(execution, resources, payload):
    rows, stocks = resources
    floor = Floor.objects.get(pk=execution.actor.floor_id)
    if not floor.active:
        raise BusinessError(
            "FLOOR_UNAVAILABLE", gettext("Votre unité est désactivé. Aucune demande envoyée.")
        )
    from apps.cases.models import StockHold

    rights = {
        row.product_id: row
        for row in FloorProduct.objects.filter(floor=floor, product_id__in=stocks)
    }
    held = set(StockHold.objects.filter(product_id__in=stocks).values_list("product_id", flat=True))
    problems = []
    for row in rows:
        stock = stocks[row["product_id"]]
        p = stock.product
        right = rights.get(p.pk)
        if not right or not right.visible or not right.demandable or not right.maximum:
            # Aucun nom/quantité d'un produit hors portée dans l'erreur.
            problems.append(
                {
                    "product_id": str(p.pk),
                    "code": "PRODUCT_NOT_AUTHORIZED",
                    "message": "Produit non autorisé ou plafond absent.",
                    "maximum_allowed": 0,
                }
            )
            continue
        maximum = (
            min(stock.balance.available or 0, right.maximum)
            if p.status == "ACTIVE" and p.counted_at and p.pk not in held
            else 0
        )
        if not maximum or row["quantity"] > maximum:
            problems.append(
                {
                    "product_id": str(p.pk),
                    "name": p.name,
                    "unit": p.unit,
                    "code": "PRODUCT_UNAVAILABLE" if not maximum else "LIMIT_EXCEEDED",
                    "message": "Produit indisponible ou en vérification."
                    if not maximum
                    else "Quantité supérieure à la disponibilité ou au plafond.",
                    "available": stock.balance.available,
                    "maximum": right.maximum,
                    "maximum_allowed": maximum,
                }
            )
    if problems:
        raise BusinessError(
            "REQUEST_REFUSED",
            gettext("Aucune demande envoyée. Vérifiez les produits indiqués."),
            details={"lines": problems},
        )
    with connection.cursor() as cursor:
        cursor.execute("SELECT nextval('public.request_reference_seq')")
        reference = f"DEM-{cursor.fetchone()[0]:08d}"
    request = InternalRequest.objects.create(
        reference=reference,
        assistant=execution.actor,
        assistant_name=execution.actor.display_name,
        floor=floor,
        floor_code=floor.code,
        floor_name=floor.name,
        creation_operation_id=execution.operation_id,
    )
    event = RequestEvent.objects.create(
        request=request, operation_id=execution.operation_id, kind="CREATED"
    )
    for sequence, row in enumerate(rows, 1):
        stock = stocks[row["product_id"]]
        p = stock.product
        right = rights[p.pk]
        line = RequestLine.objects.create(
            request=request,
            product=p,
            quantity=row["quantity"],
            product_code=p.code,
            product_name=p.name,
            unit=p.unit,
            maximum=right.maximum,
            permission_version=p.version,
            product_version=p.version,
            photo_id=p.current_photo_id,
        )
        Reservation.objects.create(line=line, product=p, remaining=line.quantity)
        entry = append_change(
            execution,
            stock,
            kind="RESERVE",
            physical=stock.balance.physical,
            delta_r=line.quantity,
            sequence=sequence,
        )
        ReservationChange.objects.create(event=event, line=line, entry=entry)
    from .selectors import request_data

    data = request_data(request)
    # Uniquement D, jamais P/R globaux dans la réponse demandeur — profil A ou son historique.
    data["available_after"] = [
        {
            "product_id": str(row["product_id"]),
            "available": stocks[row["product_id"]].balance.available,
        }
        for row in rows
    ]
    return Effect(
        data,
        after=data,
        details=[
            {"line_id": str(line.pk), "quantity": line.quantity, "unit": line.unit}
            for line in request.lines.all()
        ],
        events=(
            Event(
                "request.created",
                request.pk,
                1,
                {"request_id": str(request.pk), "recipient_role": "REPLENISHER"},
            ),
        ),
        http_status=201,
    )


def cancel_resources(actor, identifier):
    request = InternalRequest.objects.select_for_update().get(pk=identifier, assistant=actor)
    lines = list(request.lines.select_related("reservation").order_by("product_id"))
    stocks = lock_stocks({line.product_id for line in lines})
    return request, lines, stocks


def cancel(execution, resources, payload):
    if set(payload) != {"expected_version"}:
        raise invalid_input()
    request, lines, stocks = resources
    check_version(request.version, payload["expected_version"])
    if request.first_delivery_operation_id or request.status not in {"NEW", "PREPARING"}:
        raise BusinessError(
            "CANCELLATION_FORBIDDEN",
            gettext(
                "Cette demande ne peut plus être annulée. Pour un reliquat après livraison, contactez la gestionnaire."
            ),
        )
    from .selectors import request_data

    before = request_data(request)
    event = RequestEvent.objects.create(
        request=request,
        operation_id=execution.operation_id,
        kind="CANCELLED",
        revision=request.version + 1,
    )
    for sequence, line in enumerate(lines, 1):
        reservation = line.reservation
        if reservation.remaining:
            stock = stocks[line.product_id]
            entry = append_change(
                execution,
                stock,
                kind="RELEASE",
                physical=stock.balance.physical,
                delta_r=-reservation.remaining,
                sequence=sequence,
            )
            line.closed += reservation.remaining
            line.save(update_fields=["closed"])
            reservation.remaining = 0
            reservation.version += 1
            reservation.save(update_fields=["remaining", "version"])
            ReservationChange.objects.create(event=event, line=line, entry=entry)
    request.status = "CANCELLED"
    request.version += 1
    request.save(update_fields=["status", "version"])
    after = request_data(request)
    return Effect(
        after,
        before=before,
        after=after,
        events=(
            Event(
                "request.cancelled",
                request.pk,
                request.version + 1,
                {
                    "request_id": str(request.pk),
                    **(
                        {"recipient_id": str(request.assigned_to_id)}
                        if request.assigned_to_id
                        else {"recipient_role": "REPLENISHER"}
                    ),
                },
            ),
        ),
    )


COMMANDS = [
    Command(
        "requests.create",
        REQUESTERS,
        create_scope,
        lambda a, i: None,
        create,
        lock_references=lock_floor,
        lock_payload_resources=create_resources,
    ),
    Command("requests.cancel", REQUESTERS, own_request, cancel_resources, cancel),
]


def ensure_registered():
    from apps.cases.workflow import COMMANDS as CASE_COMMANDS
    from apps.inventory.reconciliation import COMMAND as RECONCILIATION_COMMAND

    from .corrections import COMMANDS as CORRECTION_COMMANDS
    from .delivery_services import COMMANDS as DELIVERY_COMMANDS
    from .refusal import COMMANDS as REFUSAL_COMMANDS

    for command in [
        *COMMANDS,
        *DELIVERY_COMMANDS,
        *REFUSAL_COMMANDS,
        *CORRECTION_COMMANDS,
        *CASE_COMMANDS,
        RECONCILIATION_COMMAND,
    ]:
        if command.name not in commands._registry:
            commands.register_command(command)
