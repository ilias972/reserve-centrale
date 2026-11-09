from django.db import transaction
from django.http import FileResponse, Http404, JsonResponse

from apps.catalog.storage import photo_storage
from apps.core.api import actor_context, endpoint, json_body
from apps.core.policies import check_role, lock_actor
from apps.inventory.selectors import page_offset
from apps.requests.api import perform

from . import selectors


@endpoint(access="authenticated", methods=["GET"])
def orders(request, order_id=None):
    with transaction.atomic():
        actor = lock_actor(actor_context(request))
        if order_id:
            order = selectors.get_order(actor, order_id)
            return JsonResponse(
                {
                    "order": selectors.order_data(order),
                    "isolated_sources": selectors.available_sources(order),
                }
            )
        offset = page_offset(request.GET)
        rows = list(selectors.orders(actor, request.GET)[offset : offset + 51])
        return JsonResponse(
            {
                "orders": [selectors.order_data(row) for row in rows[:50]],
                "next_offset": offset + 50 if len(rows) > 50 else None,
            }
        )


@endpoint(access="authenticated", methods=["GET"])
def receipts(request, receipt_id=None):
    with transaction.atomic():
        actor = lock_actor(actor_context(request))
        if receipt_id:
            return JsonResponse(
                {"receipt": selectors.receipt_data(selectors.get_receipt(actor, receipt_id))}
            )
        offset = page_offset(request.GET)
        rows = list(selectors.receipts(actor)[offset : offset + 51])
        return JsonResponse(
            {
                "receipts": [selectors.receipt_data(row) for row in rows[:50]],
                "next_offset": offset + 50 if len(rows) > 50 else None,
            }
        )


@endpoint(access="authenticated", methods=["POST"])
def receive(request, order_id):
    return perform(request, "receipts.create", json_body(request), order_id)


@endpoint(access="authenticated", methods=["POST"])
def order_action(request, order_id, action):
    return perform(
        request,
        {
            "late-delivery-reports": "receiving.late",
            "surplus-reports": "receiving.extra",
            "reinstate-remainder": "orders.reinstate_remainder",
            "reminders": "orders.remind",
        }[action],
        json_body(request),
        order_id,
    )


@endpoint(access="authenticated", methods=["POST"])
def unknown(request):
    return perform(request, "receiving.unknown", json_body(request))


@endpoint(access="authenticated", methods=["POST"])
def report_error(request, receipt_id):
    return perform(request, "receipts.report_error", json_body(request), receipt_id)


@endpoint(access="authenticated", methods=["POST"])
def quarantine(request, item_id, action):
    return perform(
        request,
        {"accept-surplus": "quarantine.accept_surplus", "refuse": "quarantine.refuse"}[action],
        json_body(request),
        item_id,
    )


@endpoint(access="authenticated", methods=["POST"])
def link_order(request, case_id):
    return perform(request, "cases.link_order", json_body(request), case_id)


@endpoint(access="authenticated", methods=["GET"])
def photo(request, order_id, line_id):
    # Périmètre historique de tâche, comme les photos des demandes M4 : aucun
    # élargissement de l'endpoint catalogue ni du droit de parcourir ses versions.
    with transaction.atomic():
        actor = lock_actor(actor_context(request))
        check_role(actor, {"ADMIN", "REPLENISHER"})
        order = selectors.get_order(actor, order_id)
        line = order.lines.select_related("photo__asset").filter(pk=line_id).first()
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
def product(request):
    from apps.catalog.models import Product

    with transaction.atomic():
        actor = lock_actor(actor_context(request))
        check_role(actor, {"ADMIN", "REPLENISHER"})
        row = Product.objects.filter(code=request.GET.get("code", "")).first()
        return JsonResponse(
            {"product": {"id": str(row.pk), "name": row.name, "unit": row.unit} if row else None}
        )
