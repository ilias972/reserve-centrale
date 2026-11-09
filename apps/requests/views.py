from django.db import transaction
from django.shortcuts import render
from django.utils.dateparse import parse_datetime

from apps.accounts.views import endpoint
from apps.catalog.models import Category
from apps.catalog.policies import visible_products
from apps.core.api import actor_context
from apps.core.policies import lock_actor
from apps.inventory.selectors import page_offset

from .selectors import (
    event_data,
    events_query,
    filtered_requests,
    get_request,
    request_catalog,
    request_data,
)


def dated(data):
    return {**data, "display_at": parse_datetime(data["at"])}


@endpoint(access="authenticated", methods=["GET"])
def create(request):
    with transaction.atomic():
        actor = lock_actor(actor_context(request))
        rows, next_offset = request_catalog(actor, {})
        categories = Category.objects.filter(
            pk__in=visible_products(actor).values("category_id")
        ).order_by("name")
        return render(
            request,
            "assistant/catalog.html",
            {
                "floor": actor.floor,
                "categories": categories,
                "cart_config": {
                    "actor_id": str(actor.pk),
                    "rows": rows,
                    "next_offset": next_offset,
                },
            },
        )


@endpoint(access="authenticated", methods=["GET"])
def listing(request):
    with transaction.atomic():
        actor = lock_actor(actor_context(request))
        offset = page_offset(request.GET)
        rows = list(filtered_requests(actor, request.GET)[offset : offset + 51])
        from .models import InternalRequest

        return render(
            request,
            "mobile/request_queue.html" if actor.role == "REPLENISHER" else "assistant/list.html",
            {
                "requests": [dated(request_data(row, actor)) for row in rows[:50]],
                "next_offset": offset + 50 if len(rows) > 50 else None,
                "filters": request.GET,
                "statuses": InternalRequest.Status.choices,
            },
        )


@endpoint(access="authenticated", methods=["GET"])
def detail(request, request_id):
    with transaction.atomic():
        actor = lock_actor(actor_context(request))
        row = get_request(actor, request_id)
        data = request_data(row, actor)
        events = [
            dated(event_data(e)) for e in events_query(actor, {"request_id": request_id})[:50]
        ]
        if actor.role == "REPLENISHER":
            return mobile_detail(request, actor, row, data, events)
        from .selectors import delivery_data

        return render(
            request,
            "assistant/detail.html",
            {
                "request_data": dated(data),
                "admin_actions": {
                    "close": actor.role == "ADMIN"
                    and bool(row.first_delivery_operation_id)
                    and row.status in {"PREPARING", "PARTIAL_OPEN"},
                    "reassign": actor.role == "ADMIN"
                    and row.status in {"PREPARING", "PARTIAL_OPEN"},
                    "reopen": actor.role == "ADMIN" and row.status == "PARTIAL_CLOSED",
                },
                "deliveries": [
                    {**dated(delivery_data(d)), "can_report": False}
                    for d in row.deliveries.select_related("operation").order_by(
                        "-event__revision"
                    )[:50]
                ],
                "events": events,
                "cancel_config": {
                    "actor_id": str(actor.pk),
                    "request_id": str(row.pk),
                    "expected_version": row.version,
                },
                "can_cancel": actor.is_requester and data["can_cancel"],
            },
        )


@endpoint(access="authenticated", methods=["GET"])
def history(request):
    with transaction.atomic():
        actor = lock_actor(actor_context(request))
        offset = page_offset(request.GET)
        rows = list(events_query(actor, request.GET)[offset : offset + 51])
        from .models import RequestLine

        products = (
            RequestLine.objects.filter(request__in=filtered_requests(actor, {}))
            .values("product_id", "product_name")
            .distinct()
            .order_by("product_name")
        )
        return render(
            request,
            "assistant/history.html",
            {
                "events": [dated(event_data(e)) for e in rows[:50]],
                "next_offset": offset + 50 if len(rows) > 50 else None,
                "filters": request.GET,
                "products": products,
            },
        )


def mobile_detail(request, actor, row, data, events):
    from .selectors import delivery_data

    can_claim = row.status == "NEW" and not row.assigned_to_id
    can_deliver = row.assigned_to_id == actor.pk and row.status in {"PREPARING", "PARTIAL_OPEN"}
    config = {
        "actor_id": str(actor.pk),
        "request_id": str(row.pk),
        "expected_version": row.version,
        "kind": "requests.claim" if can_claim else "requests.deliver",
        "lines": data["lines"],
    }
    return render(
        request,
        "mobile/delivery.html",
        {
            "request_data": dated(data),
            "events": events,
            "can_claim": can_claim,
            "can_deliver": can_deliver,
            "can_refuse": data["can_refuse"],
            "delivery_config": config,
            "deliveries": [
                {**dated(delivery_data(d)), "can_report": d.operator_id == actor.pk}
                for d in row.deliveries.select_related("operation").order_by("-event__revision")[
                    :50
                ]
            ],
        },
    )


@endpoint(access="authenticated", methods=["GET"])
def report_error_page(request, delivery_id):
    with transaction.atomic():
        actor = lock_actor(actor_context(request))
        from apps.core.policies import check_role

        from .delivery_services import report_scope
        from .models import Delivery
        from .selectors import delivery_data

        check_role(actor, {"REPLENISHER"})
        report_scope(actor, delivery_id)
        delivery = Delivery.objects.select_related("operation", "request").get(pk=delivery_id)
        return render(
            request,
            "mobile/delivery_error.html",
            {
                "delivery": dated(delivery_data(delivery)),
                "request_reference": delivery.request.reference,
                "request_id": delivery.request_id,
                "already_reported": delivery.issues.filter(kind="DELIVERY_ERROR").exists()
                or delivery.case_set.filter(kind="DELIVERY_ERROR").exists(),
                "report_config": {"actor_id": str(actor.pk), "delivery_id": str(delivery.pk)},
            },
        )


@endpoint(access="authenticated", methods=["GET"])
def issues_page(request):
    with transaction.atomic():
        actor = lock_actor(actor_context(request))
        from apps.cases.consultation import list_context

        return render(
            request, "cases/signalements.html", list_context(request, actor, "deliveries")
        )


@endpoint(access="authenticated", methods=["GET"])
def refuse_page(request, request_id):
    with transaction.atomic():
        actor = lock_actor(actor_context(request))
        from apps.core.policies import check_role

        check_role(actor, {"REPLENISHER"})
        row = get_request(actor, request_id)
        data = request_data(row, actor)
        return render(
            request,
            "mobile/refusal.html",
            {
                "request_data": dated(data),
                "refusal_config": {
                    "actor_id": str(actor.pk),
                    "request_id": str(row.pk),
                    "expected_version": row.version,
                },
            },
        )
