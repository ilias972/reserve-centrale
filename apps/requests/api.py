from django.db import transaction
from django.http import FileResponse, Http404, JsonResponse

from apps.catalog.storage import photo_storage
from apps.core.api import actor_context, endpoint, json_body
from apps.core.idempotency import execute_intent, register_intent
from apps.core.payloads import payload_hash
from apps.core.policies import lock_actor
from apps.inventory.selectors import page_offset

from .selectors import event_data, events_query, filtered_requests, get_request, request_data
from .services import ensure_registered


def perform(request, kind, payload, resource=None):
    ensure_registered()
    context = actor_context(request)
    identifier = request.headers.get("Operation-Intent")
    key = request.headers.get("Idempotency-Key")
    register_intent(
        context,
        intent_id=identifier,
        key=key,
        fingerprint=payload_hash(payload),
        command_type=kind,
        resource_id=resource,
    )
    return execute_intent(
        context,
        intent_id=identifier,
        key=key,
        payload=payload,
        correlation_id=request.correlation_id,
    )


@endpoint(access="authenticated", methods=["GET", "POST"])
def requests(request):
    if request.method == "POST":
        return perform(request, "requests.create", json_body(request))
    with transaction.atomic():
        actor = lock_actor(actor_context(request))
        offset = page_offset(request.GET)
        rows = list(filtered_requests(actor, request.GET)[offset : offset + 51])
        return JsonResponse(
            {
                "requests": [request_data(row, actor) for row in rows[:50]],
                "next_offset": offset + 50 if len(rows) > 50 else None,
            }
        )


@endpoint(access="authenticated", methods=["GET"])
def detail(request, request_id):
    with transaction.atomic():
        actor = lock_actor(actor_context(request))
        row = get_request(actor, request_id)
        events = events_query(actor, {"request_id": request_id})
        return JsonResponse(
            {
                "request": request_data(row, actor),
                "events": [event_data(event) for event in events[:50]],
            }
        )


@endpoint(access="authenticated", methods=["POST"])
def cancel(request, request_id):
    return perform(request, "requests.cancel", json_body(request), request_id)


@endpoint(access="authenticated", methods=["GET"])
def history(request):
    with transaction.atomic():
        actor = lock_actor(actor_context(request))
        offset = page_offset(request.GET)
        rows = list(events_query(actor, request.GET)[offset : offset + 51])
        return JsonResponse(
            {
                "events": [event_data(row) for row in rows[:50]],
                "next_offset": offset + 50 if len(rows) > 50 else None,
            }
        )


@endpoint(access="authenticated", methods=["GET"])
def photo(request, request_id, line_id):
    # Accès historique propre au dossier, indépendant des droits actuels/catalogue.
    with transaction.atomic():
        actor = lock_actor(actor_context(request))
        row = get_request(actor, request_id)
        line = row.lines.select_related("photo__asset").filter(pk=line_id).first()
        if line is None or line.photo_id is None:
            raise Http404
        asset = line.photo.asset
        key = asset.thumbnail_key if request.GET.get("thumbnail") == "1" else asset.object_key
        try:
            stream = photo_storage().open(key, "rb")
        except FileNotFoundError:
            raise Http404 from None
        response = FileResponse(stream, content_type="image/jpeg", filename="produit.jpg")
        response["X-Content-Type-Options"] = "nosniff"
        return response


@endpoint(access="authenticated", methods=["GET"])
def catalog(request):
    with transaction.atomic():
        actor = lock_actor(actor_context(request))
        from .selectors import request_catalog

        rows, next_offset = request_catalog(actor, request.GET)
        return JsonResponse({"products": rows, "next_offset": next_offset})


@endpoint(access="authenticated", methods=["POST"])
def claim(request, request_id):
    return perform(request, "requests.claim", json_body(request), request_id)


@endpoint(access="authenticated", methods=["GET", "POST"])
def deliveries(request, request_id):
    if request.method == "POST":
        return perform(request, "requests.deliver", json_body(request), request_id)
    with transaction.atomic():
        actor = lock_actor(actor_context(request))
        row = get_request(actor, request_id)
        from .selectors import delivery_data

        offset = page_offset(request.GET)
        rows = list(
            row.deliveries.select_related("operation").order_by("-event__revision")[
                offset : offset + 51
            ]
        )
        return JsonResponse(
            {
                "deliveries": [delivery_data(d) for d in rows[:50]],
                "next_offset": offset + 50 if len(rows) > 50 else None,
            }
        )


@endpoint(access="authenticated", methods=["POST"])
def report_error(request, delivery_id):
    return perform(request, "deliveries.report_error", json_body(request), delivery_id)


@endpoint(access="authenticated", methods=["GET"])
def delivery_issues(request):
    with transaction.atomic():
        actor = lock_actor(actor_context(request))
        from .selectors import issue_query

        offset = page_offset(request.GET)
        rows = list(issue_query(actor)[offset : offset + 51])
        return JsonResponse(
            {
                "issues": [
                    {
                        "id": str(x.pk),
                        "request_id": str(x.delivery.request_id),
                        "reference": x.delivery.request.reference,
                        "delivery_id": str(x.delivery_id),
                        "kind": x.kind,
                        "message": x.message,
                        "status": x.case.status,
                        "at": x.operation.confirmed_at.isoformat(),
                    }
                    for x in rows[:50]
                ],
                "next_offset": offset + 50 if len(rows) > 50 else None,
            }
        )


@endpoint(access="authenticated", methods=["POST"])
def refuse(request, request_id):
    return perform(request, "requests.refuse", json_body(request), request_id)


@endpoint(access="authenticated", methods=["GET"])
def pending_count(request):
    from .navigation import pending_count as count_for_actor

    with transaction.atomic():
        actor = lock_actor(actor_context(request))
        response = JsonResponse({"pending_count": count_for_actor(actor)})
        response["Cache-Control"] = "no-store"
        return response
