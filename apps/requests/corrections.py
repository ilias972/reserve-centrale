"""M6 : décisions additives et deltas, avec le registre transactionnel M3/M4."""

from django.db import connection
from django.db.models import F, Sum
from django.utils.translation import gettext

from apps.accounts.models import User
from apps.cases.models import Case, StockHold
from apps.cases.previews import fingerprint
from apps.cases.workflow import add_event, reviewed
from apps.core.commands import Command, Effect, Event
from apps.core.errors import BusinessError
from apps.core.payloads import invalid_input, parse_uuid
from apps.core.policies import check_version
from apps.inventory.primitives import append_change, lock_stocks

from .assignment import operational_scope
from .models import (
    DecisionLine,
    Delivery,
    DeliveryCorrection,
    DeliveryCorrectionLine,
    InternalRequest,
    RequestDecision,
    RequestEvent,
    ReservationChange,
)

ADMIN = frozenset({"ADMIN"})
OPEN = {"NEW", "PREPARING", "PARTIAL_OPEN"}


def lock_requests(identifiers):
    rows = list(
        InternalRequest.objects.select_for_update().filter(pk__in=identifiers).order_by("pk")
    )
    if len(rows) != len(identifiers):
        raise BusinessError("NOT_FOUND", gettext("Demande introuvable."), status=404)
    lines = {r.pk: list(r.lines.select_related("reservation").order_by("product_id")) for r in rows}
    return rows, lines


def resources(actor, identifier):
    rows, lines = lock_requests({identifier})
    return (
        rows[0],
        lines[identifier],
        lock_stocks({line_item.product_id for line_item in lines[identifier]}),
    )


def correction_scope(actor, identifier):
    if not Delivery.objects.filter(pk=identifier).exists():
        from apps.receiving.models import Receipt

        if not Receipt.objects.filter(pk=identifier).exists():
            raise BusinessError(
                "NOT_FOUND", gettext("Opération d’origine introuvable."), status=404
            )


def correction_resources(actor, identifier):
    d = Delivery.objects.get(pk=identifier)
    return d, *resources(actor, d.request_id)


def effective(delivery_line):
    delta = (
        delivery_line.corrections.aggregate(n=Sum(F("effective_after") - F("effective_before")))[
            "n"
        ]
        or 0
    )
    return delivery_line.handed + delta


def project(request, lines):
    u = sum(line_item.reservation.remaining for line_item in lines)
    delivered_total = sum(row.delivered for row in lines)
    c = sum(row.closed for row in lines)
    if u:
        request.status = (
            "PARTIAL_OPEN"
            if delivered_total
            else ("PREPARING" if request.assigned_to_id else "NEW")
        )
    elif c:
        request.status = "PARTIAL_CLOSED"
    else:
        request.status = "DELIVERED"
    if not c:
        request.closure_reason = ""
    request.save(update_fields=["version", "status", "closure_reason", "assigned_to"])


def decision(
    execution, request, kind, *, case=None, cause="", message="", disposition="", next_operator=None
):
    request.version += 1
    event = RequestEvent.objects.create(
        request=request, operation_id=execution.operation_id, kind=kind, revision=request.version
    )
    return RequestDecision.objects.create(
        event=event,
        case=case,
        cause=cause,
        verification=True,
        message=message,
        disposition=disposition,
        previous_operator_id=request.assigned_to_id,
        next_operator_id=next_operator or request.assigned_to_id,
    )


def persist_line(
    execution,
    dec,
    line,
    stock,
    *,
    delivered,
    closed,
    reserved,
    physical_delta=0,
    kind="RELEASE",
    sequence,
):
    before = (line.delivered, line.closed, line.reservation.remaining)
    dr = reserved - before[2]
    entry = None
    if physical_delta or dr:
        entry = append_change(
            execution,
            stock,
            kind=kind,
            physical=stock.balance.physical + physical_delta,
            delta_r=dr,
            sequence=sequence,
        )
        ReservationChange.objects.create(event=dec.event, line=line, entry=entry)
        line.reservation.version += 1
        line.reservation.remaining = reserved
        line.reservation.save(update_fields=["remaining", "version"])
    line.delivered = delivered
    line.closed = closed
    line.save(update_fields=["delivered", "closed"])
    DecisionLine.objects.create(
        decision=dec,
        line=line,
        entry=entry,
        delivered_before=before[0],
        delivered_after=delivered,
        closed_before=before[1],
        closed_after=closed,
        reserved_before=before[2],
        reserved_after=reserved,
    )


def result_effect(request, event_type, *, extra_events=(), details=None, extra_users=()):
    from .selectors import request_data

    result = request_data(request)
    recipients = ({request.assistant_id, request.assigned_to_id} | set(extra_users)) - {None}
    # Destinataires explicites dans l’événement applicatif ; aucun canal externe exécuté.
    events = (
        Event(
            event_type,
            request.pk,
            request.version,
            {"request_id": str(request.pk), "recipient_ids": [str(r) for r in sorted(recipients)]},
        ),
    )
    return Effect(result, after=result, details=details or [], events=events + tuple(extra_events))


def correction_plan(resources, payload):
    delivery, request, lines, stocks = resources
    allowed = {
        "kind",
        "source_id",
        "case_id",
        "expected_versions",
        "corrected_lines",
        "disposition",
        "reopen_quantities",
        "verification",
        "message",
        "preview_hash",
    }
    if (
        set(payload) - allowed
        or payload.get("kind") != "DELIVERY"
        or parse_uuid(payload.get("source_id")) != delivery.pk
    ):
        raise invalid_input()
    reviewed(payload)
    versions = payload.get("expected_versions", {})
    if not isinstance(versions, dict) or set(versions) != {"request", "case", "stocks"}:
        raise invalid_input()
    check_version(request.version, versions["request"])
    case = (
        Case.objects.select_for_update(of=("self",))
        .select_related("origin_operation")
        .filter(pk=parse_uuid(payload.get("case_id")), delivery=delivery)
        .first()
    )
    if case is None:
        raise BusinessError(
            "CASE_SOURCE_MISMATCH",
            gettext("Ce dossier ne concerne pas cette livraison."),
            status=400,
        )
    check_version(case.version, versions["case"])
    if not isinstance(versions["stocks"], dict) or set(versions["stocks"]) != {
        str(p) for p in stocks
    }:
        raise invalid_input()
    for pid, stock in stocks.items():
        check_version(stock.balance.version, versions["stocks"][str(pid)])
    closed = request.status not in OPEN
    disposition = payload.get("disposition", "KEEP_CLOSED" if closed else "KEEP_OPEN")
    if disposition not in ({"KEEP_CLOSED", "REOPEN"} if closed else {"KEEP_OPEN"}):
        raise BusinessError(
            "INVALID_STATE",
            gettext("Le choix de clôture ne correspond pas à l'état actuel de la demande."),
        )
    rows = payload.get("corrected_lines")
    if not isinstance(rows, list) or not 1 <= len(rows) <= 100:
        raise invalid_input()
    source = {row.pk: row for row in delivery.lines.select_related("line").all()}
    changes = {}
    corrections = []
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"delivery_line_id", "quantity"}:
            raise invalid_input()
        key = parse_uuid(row["delivery_line_id"])
        q = row["quantity"]
        if key not in source or key in changes or type(q) is not int or not 0 <= q <= 1000000000:
            raise invalid_input()
        changes[key] = q
        original = source[key]
        before = effective(original)
        corrections.append(
            {
                "delivery_line_id": str(key),
                "request_line_id": str(original.line_id),
                "initial": original.handed,
                "effective_before": before,
                "effective_after": q,
                "delta": q - before,
            }
        )
    reopen = {}
    reopen_rows = payload.get("reopen_quantities", [])
    if not isinstance(reopen_rows, list) or len(reopen_rows) > 100:
        raise invalid_input()
    for row in reopen_rows:
        if not isinstance(row, dict) or set(row) != {"request_line_id", "quantity"}:
            raise invalid_input()
        key = parse_uuid(row["request_line_id"])
        if (
            key in reopen
            or key not in {line_item.pk for line_item in lines}
            or type(row["quantity"]) is not int
            or row["quantity"] < 0
        ):
            raise invalid_input()
        reopen[key] = row["quantity"]
    if disposition != "REOPEN" and reopen:
        raise invalid_input()
    proposed = []
    shortages = []
    for line in lines:
        d = sum(c["delta"] for c in corrections if c["request_line_id"] == str(line.pk))
        new_delivered = line.delivered + d
        u = (
            (reopen.get(line.pk, 0) if disposition == "REOPEN" else 0)
            if closed
            else line.quantity - new_delivered - line.closed
        )
        c = line.quantity - new_delivered - u
        if min(new_delivered, c, u) < 0 or new_delivered > line.quantity:
            raise BusinessError(
                "QUANTITY_EXCEEDS_REQUEST",
                gettext("La correction dépasse la demande ou contredit une clôture ultérieure."),
                details={"blocking_request_id": str(request.pk), "request_line_id": str(line.pk)},
            )
        stock = stocks[line.product_id]
        p = stock.balance.physical - d
        r = stock.balance.reserved + u - line.reservation.remaining
        if StockHold.objects.filter(product_id=line.product_id).exists() and (
            d > 0 or u > line.reservation.remaining
        ):
            raise BusinessError(
                "PRODUCT_ON_HOLD",
                gettext("Produit en vérification : régularisez d'abord son stock."),
            )
        if p > 1000000000:
            raise BusinessError(
                "INVALID_QUANTITY",
                gettext("La correction dépasse la limite de quantité autorisée."),
                status=400,
            )
        if p < r:
            shortages.append({"product_id": str(line.product_id), "attempted_physical": p})
        proposed.append(
            {
                "request_line_id": str(line.pk),
                "product_id": str(line.product_id),
                "name": line.product_name,
                "unit": line.unit,
                "Q": line.quantity,
                "before": {
                    "L": line.delivered,
                    "C": line.closed,
                    "U": line.reservation.remaining,
                    "P": stock.balance.physical,
                    "R": stock.balance.reserved,
                    "D": stock.balance.available,
                },
                "after": {"L": new_delivered, "C": c, "U": u, "P": p, "R": r, "D": p - r},
                "delta_delivered": d,
            }
        )
    if shortages:
        raise BusinessError(
            "STOCK_BELOW_RESERVED",
            gettext(
                "La correction laisserait un stock inférieur aux réservations. Aucun mouvement enregistré."
            ),
            details={"lines": shortages, "source_case_id": str(case.pk)},
        )
    if disposition == "REOPEN" and not any(row["after"]["U"] for row in proposed):
        raise BusinessError(
            "EMPTY_REOPEN", gettext("Choisissez une quantité à rouvrir."), status=400
        )
    future = (
        "PARTIAL_OPEN"
        if any(row["after"]["U"] for row in proposed) and any(row["after"]["L"] for row in proposed)
        else ("PREPARING" if request.assigned_to_id else "NEW")
        if any(row["after"]["U"] for row in proposed)
        else "PARTIAL_CLOSED"
        if any(row["after"]["C"] for row in proposed)
        else "DELIVERED"
    )
    plan = {
        "source_id": str(delivery.pk),
        "case_id": str(case.pk),
        "request_id": str(request.pk),
        "request_reference": request.reference,
        "disposition": disposition,
        "status_before": request.status,
        "status_after": future,
        "versions": versions,
        "corrections": corrections,
        "lines": proposed,
    }
    plan["preview_hash"] = fingerprint(plan)
    return plan, case


def correct(execution, resources, payload):
    plan, case = correction_plan(resources, payload)
    if payload.get("preview_hash") != plan["preview_hash"]:
        raise BusinessError(
            "PREVIEW_CHANGED",
            gettext("Prévisualisez à nouveau les conséquences avant confirmation."),
        )
    delivery, request, lines, stocks = resources
    dec = decision(
        execution,
        request,
        "CORRECTED",
        case=case,
        cause="DELIVERY_CORRECTION",
        message=reviewed(payload),
        disposition=plan["disposition"],
    )
    previous = delivery.corrections.order_by("-decision__event__revision").first()
    fact = DeliveryCorrection.objects.create(delivery=delivery, decision=dec, previous=previous)
    for row in plan["corrections"]:
        DeliveryCorrectionLine.objects.create(
            correction=fact,
            delivery_line_id=row["delivery_line_id"],
            effective_before=row["effective_before"],
            effective_after=row["effective_after"],
        )
    for seq, (line, row) in enumerate(zip(lines, plan["lines"], strict=True), 1):
        after = row["after"]
        persist_line(
            execution,
            dec,
            line,
            stocks[line.product_id],
            delivered=after["L"],
            closed=after["C"],
            reserved=after["U"],
            physical_delta=-row["delta_delivered"],
            kind="DELIVERY_CORRECTION",
            sequence=seq,
        )
    if any(line_item.closed for line_item in lines) and not request.closure_reason:
        request.closure_reason = "DELIVERY_CORRECTION"
    project(request, lines)
    ev = add_event(
        execution,
        case,
        "DELIVERY_CORRECTED",
        "RESOLVED",
        {"correction_id": str(fact.pk), "verification": True, "message": dec.message},
    )
    return result_effect(
        request,
        "request.corrected",
        extra_events=(ev,),
        details=plan["lines"],
        extra_users=(delivery.operator_id,),
    )


def rejected(execution, resources, error):
    if error.code != "STOCK_BELOW_RESERVED":
        return {}
    from apps.cases.services import open_stock_case

    return open_stock_case(
        execution,
        resources[-1],
        error.details["lines"],
        source_case_id=error.details["source_case_id"],
    )


def close_remainder(execution, data, payload):
    return apply_request_decision(execution, data, payload, "close-remainder")


def lock_reassignment_accounts(context, identifier, payload):
    target = parse_uuid(payload.get("new_operator_id"))
    with connection.cursor() as cursor:
        for identifier in sorted({context.actor_id, target}):
            cursor.execute("SELECT public.reserve_lock_actor(%s)", [identifier])


def reassign(execution, data, payload):
    return apply_request_decision(execution, data, payload, "reassign")


def correction_references(actor, identifier, payload):
    if payload.get("kind") == "RECEIPT":
        from apps.receiving.locks import receipt_supplier

        receipt_supplier(actor, identifier)


def correction_dispatch_resources(actor, identifier, payload):
    if payload.get("kind") == "RECEIPT":
        from apps.receiving.corrections import resources as receipt_resources

        return receipt_resources(actor, identifier, payload)
    return correction_resources(actor, identifier)


def correction_dispatch(execution, data, payload):
    if payload.get("kind") == "RECEIPT":
        from apps.receiving.corrections import confirm

        return confirm(execution, data, payload)
    return correct(execution, data, payload)


def correction_rejected(execution, data, error):
    from apps.receiving.corrections import CorrectionScope
    from apps.receiving.corrections import rejected as receipt_rejected

    if isinstance(data, CorrectionScope):
        return receipt_rejected(execution, data, error)
    return rejected(execution, data, error)


COMMANDS = [
    Command(
        "corrections.confirm",
        ADMIN,
        correction_scope,
        correction_resources,
        correction_dispatch,
        record_rejection=correction_rejected,
        lock_payload_resources=correction_dispatch_resources,
        lock_payload_references=correction_references,
    ),
    Command("requests.close_remainder", ADMIN, operational_scope, resources, close_remainder),
    Command(
        "requests.reassign",
        ADMIN,
        operational_scope,
        resources,
        reassign,
        lock_payload_accounts=lock_reassignment_accounts,
    ),
]


def reopen(execution, data, payload):
    return apply_request_decision(execution, data, payload, "reopen")


COMMANDS.append(Command("requests.reopen", ADMIN, operational_scope, resources, reopen))


def request_plan(action, data, payload):
    request, lines, stocks = data
    keys = {
        "close-remainder": {"expected_version", "reason", "verification"},
        "reopen": {"expected_version", "quantities", "reason", "verification"},
        "reassign": {"expected_version", "new_operator_id", "verification"},
    }[action]
    if not keys <= set(payload) or set(payload) - keys - (
        {"preview_hash", "message"} if action == "reassign" else {"preview_hash"}
    ):
        raise invalid_input()
    check_version(request.version, payload["expected_version"])
    message = payload.get("message", payload.get("reason", ""))
    if payload["verification"] is not True:
        raise BusinessError(
            "VERIFICATION_REQUIRED",
            gettext("Vérifiez les conséquences avant de confirmer."),
            status=400,
        )
    if not isinstance(message, str) or len(message) > 2000:
        raise invalid_input()
    if action != "reassign" and not message.strip():
        raise BusinessError(
            "VERIFICATION_REQUIRED", gettext("Décrivez la vérification et la décision."), status=400
        )
    quantities = {}
    target = None
    if action == "close-remainder":
        if (
            request.status not in OPEN
            or not request.first_delivery_operation_id
            or not any(line.reservation.remaining for line in lines)
        ):
            raise BusinessError(
                "INVALID_STATE",
                gettext(
                    "La clôture d'abandon nécessite un reliquat ouvert après une première remise."
                ),
            )
    elif action == "reassign":
        target = User.objects.filter(
            pk=parse_uuid(payload["new_operator_id"]), is_active=True, role="REPLENISHER"
        ).first()
        if target is None:
            raise BusinessError(
                "INVALID_OPERATOR", gettext("Choisissez un préparateur actif."), status=400
            )
        if (
            request.status not in {"PREPARING", "PARTIAL_OPEN"}
            or not any(line.reservation.remaining for line in lines)
            or target.pk == request.assigned_to_id
        ):
            raise BusinessError(
                "INVALID_STATE",
                gettext(
                    "La demande n'a pas de reliquat réattribuable ou utilise déjà cet opérateur."
                ),
            )
    else:
        if request.status not in {"PARTIAL_CLOSED", "DELIVERED"}:
            raise BusinessError(
                "INVALID_STATE",
                gettext("La demande n'est pas clôturée avec des quantités à rouvrir."),
            )
        rows = payload["quantities"]
        if not isinstance(rows, list) or not 1 <= len(rows) <= 100:
            raise invalid_input()
        allowed = {line.pk: line for line in lines}
        for row in rows:
            if not isinstance(row, dict) or set(row) != {"request_line_id", "quantity"}:
                raise invalid_input()
            lid = parse_uuid(row["request_line_id"])
            q = row["quantity"]
            if (
                lid not in allowed
                or lid in quantities
                or type(q) is not int
                or not 0 <= q <= allowed[lid].closed
            ):
                raise invalid_input()
            quantities[lid] = q
        if not any(quantities.values()):
            raise BusinessError(
                "EMPTY_REOPEN", gettext("Choisissez une quantité à rouvrir."), status=400
            )
    planned = []
    for line in lines:
        stock = stocks[line.product_id]
        u = line.reservation.remaining
        if action == "close-remainder":
            new_u = 0
            new_c = line.closed + u
        elif action == "reopen":
            new_u = quantities.get(line.pk, 0)
            new_c = line.closed - new_u
        else:
            new_u = u
            new_c = line.closed
        new_r = stock.balance.reserved + new_u - u
        if new_r > stock.balance.physical:
            raise BusinessError(
                "INSUFFICIENT_STOCK",
                gettext("Stock disponible insuffisant pour cette réouverture."),
                details={"blocking_product_id": str(line.product_id)},
            )
        if new_u > u and StockHold.objects.filter(product_id=line.product_id).exists():
            raise BusinessError(
                "PRODUCT_ON_HOLD",
                gettext(
                    "Le produit est en vérification ; régularisez-le avant de rouvrir des quantités."
                ),
            )
        planned.append(
            {
                "request_line_id": str(line.pk),
                "product_id": str(line.product_id),
                "name": line.product_name,
                "unit": line.unit,
                "Q": line.quantity,
                "before": {
                    "L": line.delivered,
                    "C": line.closed,
                    "U": u,
                    "P": stock.balance.physical,
                    "R": stock.balance.reserved,
                    "D": stock.balance.available,
                },
                "after": {
                    "L": line.delivered,
                    "C": new_c,
                    "U": new_u,
                    "P": stock.balance.physical,
                    "R": new_r,
                    "D": stock.balance.physical - new_r,
                },
                "stock_version": stock.balance.version,
            }
        )
    explanation = {
        "close-remainder": "Le reliquat sera abandonné : libération des seules réservations restantes, sans sortie physique.",
        "reopen": "Les seules quantités choisies seront réservées à nouveau ; aucune réouverture automatique.",
        "reassign": "Le nouvel opérateur prendra le relais ; aucun mouvement de stock ni changement d'auteur des remises précédentes.",
    }[action]
    status_after = (
        "PARTIAL_CLOSED"
        if action == "close-remainder"
        else (
            "PARTIAL_OPEN"
            if any(row["after"]["L"] for row in planned)
            else "PREPARING"
            if request.assigned_to_id
            else "NEW"
        )
        if action == "reopen"
        else request.status
    )
    result = {
        "action": action,
        "request_id": str(request.pk),
        "request_version": request.version,
        "request_reference": request.reference,
        "lines": planned,
        "status_after": status_after,
        "explanation": explanation,
        "next_operator_id": str(target.pk) if target else None,
        "next_operator_name": target.display_name if target else None,
        "operator_auth_version": target.auth_version if target else None,
    }
    result["preview_hash"] = fingerprint(result)
    return result


def apply_request_decision(execution, data, payload, action):
    plan = request_plan(action, data, payload)
    if "preview_hash" in payload and payload["preview_hash"] != plan["preview_hash"]:
        raise BusinessError(
            "PREVIEW_CHANGED",
            gettext("Les données ont changé. Prévisualisez à nouveau les conséquences."),
        )
    request, lines, stocks = data
    kind, cause, event_type = {
        "close-remainder": ("CLOSED", "REMAINDER_ABANDONED", "request.remainder_closed"),
        "reopen": ("REOPENED", "REOPENING", "request.reopened"),
        "reassign": ("REASSIGNED", "REASSIGNMENT", "request.reassigned"),
    }[action]
    dec = decision(
        execution,
        request,
        kind,
        cause=cause,
        message=payload.get("message", payload.get("reason", "Réattribution vérifiée.")),
        next_operator=parse_uuid(plan["next_operator_id"]) if plan["next_operator_id"] else None,
    )
    if action == "reassign":
        request.assigned_to_id = parse_uuid(plan["next_operator_id"])
    else:
        for sequence, (line, row) in enumerate(zip(lines, plan["lines"], strict=True), 1):
            persist_line(
                execution,
                dec,
                line,
                stocks[line.product_id],
                delivered=line.delivered,
                closed=row["after"]["C"],
                reserved=row["after"]["U"],
                kind="DELIVERY_CORRECTION" if action == "reopen" else "RELEASE",
                sequence=sequence,
            )
    if action == "close-remainder":
        request.closure_reason = "REMAINDER_ABANDONED"
    project(request, lines)
    return result_effect(
        request, event_type, details=plan["lines"], extra_users=(dec.previous_operator_id,)
    )
