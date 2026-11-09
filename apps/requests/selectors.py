from django.db.models import Q
from django.utils.translation import gettext

from apps.core.errors import BusinessError
from apps.core.search import matching

from .models import InternalRequest, RequestEvent


def request_query(actor):
    query = InternalRequest.objects.select_related("creation_operation").prefetch_related(
        "lines__reservation"
    )
    if actor.role == "ADMIN":
        return query
    if actor.is_requester:
        return query.filter(assistant=actor)
    if actor.role == "REPLENISHER":
        return query.filter(
            Q(status__in=["NEW", "PREPARING", "PARTIAL_OPEN"])
            | Q(assigned_to=actor)
            | Q(deliveries__operator=actor)
            | Q(events__decision__previous_operator=actor)
            | Q(events__decision__next_operator=actor)
        ).distinct()
    raise BusinessError(
        "PERMISSION_DENIED", gettext("Accès aux demandes non autorisé."), status=403
    )


def get_request(actor, identifier):
    request = request_query(actor).filter(pk=identifier).first()
    if request is None:
        raise BusinessError("NOT_FOUND", gettext("Demande introuvable."), status=404)
    return request


def request_data(request, actor=None):
    from .states import status_label

    return {
        "id": str(request.pk),
        "reference": request.reference,
        "assistant_name": request.assistant_name,
        "floor_code": request.floor_code,
        "floor_name": request.floor_name,
        "at": request.creation_operation.confirmed_at.isoformat(),
        "status": request.status,
        "status_label": status_label(request),
        "assigned_to_id": str(request.assigned_to_id) if request.assigned_to_id else None,
        "closure_reason": request.closure_reason,
        "version": request.version,
        "has_confirmed_delivery": bool(request.first_delivery_operation_id),
        "can_cancel": request.status in {"NEW", "PREPARING"}
        and not request.first_delivery_operation_id
        and (actor is None or (actor.is_requester and actor.pk == request.assistant_id)),
        "can_refuse": request.status == "PREPARING"
        and not request.first_delivery_operation_id
        and actor is not None
        and actor.role == "REPLENISHER"
        and actor.pk == request.assigned_to_id,
        "lines": [
            {
                "id": str(line.pk),
                "product_id": str(line.product_id),
                "code": line.product_code,
                "name": line.product_name,
                "unit": line.unit,
                "quantity": line.quantity,
                "delivered": line.delivered,
                "closed": line.closed,
                "reserved": line.reservation.remaining,
                "maximum_at_creation": line.maximum,
                "photo_url": f"/api/v1/requests/{request.pk}/lines/{line.pk}/photo"
                if line.photo_id
                else None,
            }
            for line in request.lines.all()
        ],
    }


def filtered_requests(actor, params):
    query = request_query(actor)
    if params.get("status"):
        if params["status"] not in InternalRequest.Status.values:
            raise BusinessError("INVALID_INPUT", gettext("Statut invalide."), status=400)
        query = query.filter(status=params["status"])
    matched = matching(
        query,
        params.get("q", "")[:150],
        ("reference", "lines__product_name", "lines__product_code", "floor_name", "assistant_name"),
    )
    # IN sur les identifiants : une demande multi-produits reste une seule ligne.
    return query.filter(pk__in=matched.order_by().values("pk")).order_by(
        "-creation_operation__confirmed_at", "-id"
    )


def events_query(actor, params):
    query = RequestEvent.objects.select_related(
        "request", "request__creation_operation", "operation__actor"
    ).filter(request__in=request_query(actor))
    if actor.role == "REPLENISHER":
        query = query.filter(
            Q(operation__actor=actor)
            | (
                Q(kind__in=["CORRECTED", "CLOSED", "REOPENED", "REASSIGNED", "RECONCILED"])
                & (
                    Q(request__assigned_to=actor)
                    | Q(decision__previous_operator=actor)
                    | Q(decision__next_operator=actor)
                    | Q(request__deliveries__operator=actor)
                )
            )
        ).distinct()
    if params.get("request_id"):
        from apps.core.payloads import parse_uuid

        query = query.filter(request_id=parse_uuid(params["request_id"]))
    from apps.core.payloads import parse_uuid

    if params.get("product_id"):
        query = query.filter(request__lines__product_id=parse_uuid(params["product_id"])).distinct()
    from datetime import date

    for key, lookup in [("from", "gte"), ("to", "lte")]:
        if params.get(key):
            try:
                value = date.fromisoformat(params[key])
            except ValueError as exc:
                raise BusinessError("INVALID_INPUT", gettext("Date invalide."), status=400) from exc
            query = query.filter(**{f"operation__confirmed_at__date__{lookup}": value})
    return query.order_by("-operation__confirmed_at", "-id")


def event_data(event):
    return {
        "id": str(event.pk),
        "request_id": str(event.request_id),
        "reference": event.request.reference,
        "kind": event.kind,
        "label": event.get_kind_display(),
        "message": event.message,
        "at": event.operation.confirmed_at.isoformat(),
        "actor": event.operation.actor.display_name,
        "operation_id": str(event.operation_id),
        "floor_name": event.request.floor_name,
    }


def request_catalog(actor, params):
    if not actor.is_requester:
        raise BusinessError(
            "PERMISSION_DENIED",
            gettext(
                "La sélection de fournitures est réservée aux demandeur — profil As et aux demandeur — profil Bs."
            ),
            status=403,
        )
    from apps.catalog.selectors import catalog_list, product_data

    if params.get("product_ids") is not None:
        # Lecture ciblée des sélections masquées par un filtre/pagination.
        # Le périmètre autorisé est appliqué avant les identifiants demandés.
        from apps.catalog.policies import visible_products
        from apps.core.payloads import parse_uuid

        identifiers = params["product_ids"].split(",")
        if not 1 <= len(identifiers) <= 100:
            raise BusinessError(
                "INVALID_INPUT", gettext("Sélection de produits invalide."), status=400
            )
        identifiers = [parse_uuid(value) for value in identifiers]
        products = visible_products(actor).filter(pk__in=identifiers).order_by("code")
        next_offset = None
    else:
        products, next_offset = catalog_list(actor, params)
    rows = []
    for product in products:
        data = product_data(product, actor)
        rows.append(
            {
                "product_id": str(product.pk),
                "name": product.name,
                "code": product.code,
                "unit": product.unit,
                "photo_url": data["photo_url"],
                "available": data["available_quantity"],
                "maximum": data.get("maximum"),
                "maximum_allowed": data.get("maximum_allowed", 0),
                "demandable": data["demandable"],
                "reason": data.get("request_reason", ""),
            }
        )
    return rows, next_offset


def delivery_data(delivery):

    return {
        "id": str(delivery.pk),
        "operator": delivery.operator_name,
        "at": delivery.operation.confirmed_at.isoformat(),
        "mode": delivery.mode,
        "mode_label": delivery.get_mode_display(),
        "operation_id": str(delivery.operation_id),
        "lines": [
            {
                "request_line_id": str(row.line_id),
                "name": row.line.product_name,
                "unit": row.line.unit,
                "handed": row.handed,
                "effective_handed": __import__(
                    "apps.requests.corrections", fromlist=["effective"]
                ).effective(row),
                "corrected": row.corrections.exists(),
                "missing": row.missing,
            }
            for row in delivery.lines.select_related("line").all()
        ],
    }


def issue_query(actor):
    from .models import DeliveryIssue

    query = DeliveryIssue.objects.select_related("delivery__request", "operation__actor")
    if actor.role == "ADMIN":
        return query.order_by("-operation__confirmed_at")
    if actor.role == "REPLENISHER":
        return query.filter(operation__actor=actor).order_by("-operation__confirmed_at")
    raise BusinessError(
        "PERMISSION_DENIED",
        gettext("Ces signalements sont réservés à la gestionnaire et à votre activité autorisée."),
        status=403,
    )


def effective_deliveries(actor, start, end):
    """A11.5 : une ligne d'origine, état effectif actuel, date de la remise.

    Contrat de lecture uniquement : aucun écran/statistique/export M10. Les deltas
    gardent leur date dans DeliveryCorrection/Operation, sans devenir des remises.
    """
    from django.db.models import BigIntegerField, F, OuterRef, Subquery, Sum, Value
    from django.db.models.functions import Coalesce

    from apps.core.periods import origin_period
    from apps.core.policies import check_role

    from .models import DeliveryCorrectionLine, DeliveryLine

    check_role(actor, {"ADMIN"})
    deltas = (
        DeliveryCorrectionLine.objects.filter(delivery_line_id=OuterRef("pk"))
        .values("delivery_line_id")
        .annotate(total=Sum(F("effective_after") - F("effective_before")))
        .values("total")
    )
    rows = DeliveryLine.objects.select_related("delivery__operation", "line").annotate(
        effective_quantity=F("handed")
        + Coalesce(Subquery(deltas), Value(0), output_field=BigIntegerField())
    )
    return origin_period(rows, "delivery__operation__confirmed_at", start, end)
