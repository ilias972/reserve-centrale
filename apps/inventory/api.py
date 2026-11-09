from django.db import transaction
from django.http import Http404, JsonResponse

from apps.catalog.api import serve_photo
from apps.catalog.models import Product
from apps.core.api import actor_context, endpoint, json_body
from apps.core.idempotency import execute_intent, register_intent
from apps.core.payloads import payload_hash
from apps.core.policies import lock_actor

from .selectors import activity_query, entry_data, page_offset, stock_reader, stocks_list
from .services import ensure_registered


def perform(request, kind, payload):
    ensure_registered()
    context = actor_context(request)
    identifier = request.headers.get("Operation-Intent")
    key = request.headers.get("Idempotency-Key")
    register_intent(
        context, intent_id=identifier, key=key, fingerprint=payload_hash(payload), command_type=kind
    )
    return execute_intent(
        context,
        intent_id=identifier,
        key=key,
        payload=payload,
        correlation_id=request.correlation_id,
    )


@endpoint(access="authenticated", methods=["POST"])
def initial(request):
    return perform(request, "inventory.initial", json_body(request))


@endpoint(access="authenticated", methods=["POST"])
def adjust(request):
    return perform(request, "inventory.adjust", json_body(request))


@endpoint(access="authenticated", methods=["GET"])
def stocks(request):
    ensure_registered()
    with transaction.atomic():
        actor = lock_actor(actor_context(request))
        rows, next_offset = stocks_list(actor, request.GET)
        return JsonResponse({"products": rows, "next_offset": next_offset})


@endpoint(access="authenticated", methods=["GET"])
def activity(request, entry_id=None):
    with transaction.atomic():
        actor = lock_actor(actor_context(request))
        query = activity_query(actor)
        if entry_id:
            entry = query.filter(pk=entry_id).first()
            if entry is None:
                raise Http404
            return JsonResponse({"movement": entry_data(entry)})
        if request.GET.get("product_id"):
            from apps.core.payloads import parse_uuid

            query = query.filter(product_id=parse_uuid(request.GET["product_id"]))
        offset = page_offset(request.GET)
        rows = list(query[offset : offset + 51])
        return JsonResponse(
            {
                "movements": [entry_data(row) for row in rows[:50]],
                "next_offset": offset + 50 if len(rows) > 50 else None,
            }
        )


@endpoint(access="authenticated", methods=["GET"])
def photo(request, product_id):
    with transaction.atomic():
        actor = lock_actor(actor_context(request))
        stock_reader(actor)
        product = Product.objects.filter(pk=product_id).first()
        if product is None:
            raise Http404
        return serve_photo(request, actor, product)
