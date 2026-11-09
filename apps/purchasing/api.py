from django.db import transaction
from django.http import JsonResponse
from django.utils.translation import gettext

from apps.core.api import actor_context, endpoint, json_body
from apps.core.errors import BusinessError
from apps.core.policies import check_role, lock_actor
from apps.inventory.selectors import page_offset
from apps.requests.api import perform

from . import navigation, recommendations, selectors


@endpoint(access="authenticated", methods=["GET"])
def pending_count(request):
    with transaction.atomic():
        actor = lock_actor(actor_context(request))
        return JsonResponse({"pending_count": navigation.pending_count(actor)})


@endpoint(access="authenticated", methods=["GET"])
def needs(request, product_id=None):
    with transaction.atomic():
        actor = lock_actor(actor_context(request))
        check_role(actor, {"ADMIN"})
        if product_id:
            product = recommendations.query().filter(pk=product_id).first()
            if product is None:
                raise BusinessError("NOT_FOUND", gettext("Produit introuvable."), status=404)
            return JsonResponse({"recommendation": recommendations.data(product)})
        rows, next_offset = selectors.needs(actor, request.GET)
        return JsonResponse(
            {"recommendations": rows, "next_offset": next_offset, "policy": recommendations.POLICY}
        )


@endpoint(access="authenticated", methods=["POST"])
def recommendation_action(request, product_id, action):
    return perform(request, "recommendations." + action, json_body(request), product_id)


@endpoint(access="authenticated", methods=["GET", "POST"])
def orders(request):
    if request.method == "POST":
        return perform(request, "orders.create", json_body(request))
    with transaction.atomic():
        actor = lock_actor(actor_context(request))
        offset = page_offset(request.GET)
        found = list(selectors.order_query(actor, request.GET)[offset : offset + 51])
        return JsonResponse(
            {
                "orders": [selectors.order_data(o) for o in found[:50]],
                "next_offset": offset + 50 if len(found) > 50 else None,
            }
        )


@endpoint(access="authenticated", methods=["GET"])
def order(request, order_id):
    with transaction.atomic():
        actor = lock_actor(actor_context(request))
        row = selectors.get_order(actor, order_id)
        offset = page_offset(request.GET)
        events = list(
            row.events.select_related("operation__actor")
            .prefetch_related("lines")
            .order_by("-revision")[offset : offset + 51]
        )
        return JsonResponse(
            {
                "order": selectors.order_data(row),
                "events": [
                    {
                        "at": e.operation.confirmed_at.isoformat(),
                        "author": e.operation.actor.display_name,
                        "revision": e.revision,
                        "external_verification": e.external_verification,
                        "lines": [
                            {
                                "order_line_id": str(item.line_id),
                                "quantity": item.quantity,
                                "cancelled_before": item.cancelled_before,
                                "cancelled_after": item.cancelled_after,
                            }
                            for item in e.lines.all()
                        ],
                    }
                    for e in events[:50]
                ],
                "next_offset": offset + 50 if len(events) > 50 else None,
            }
        )


@endpoint(access="authenticated", methods=["POST"])
def cancel(request, order_id):
    return perform(request, "orders.cancel_remainder", json_body(request), order_id)


@endpoint(access="authenticated", methods=["GET"])
def purchase_product(request):
    from apps.catalog.models import Product
    from apps.catalog.services import code

    with transaction.atomic():
        actor = lock_actor(actor_context(request))
        check_role(actor, {"ADMIN"})
        product = (
            Product.objects.select_related("stock")
            .filter(code=code(request.GET.get("code"), 64))
            .first()
        )
        if product is None:
            raise BusinessError(
                "NOT_FOUND",
                gettext("Choisissez une référence exacte dans les suggestions."),
                status=404,
            )
        from .selectors import purchase_product_data

        return JsonResponse({"product": purchase_product_data(product)})
