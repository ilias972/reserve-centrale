from django.db import transaction
from django.http import Http404
from django.shortcuts import render
from django.utils.dateparse import parse_datetime

from apps.accounts.views import endpoint
from apps.cases.selectors import case_query
from apps.catalog.models import Category
from apps.core.api import actor_context
from apps.core.policies import lock_actor

from .selectors import activity_query, entry_data, page_offset, stock_reader, stocks_list
from .services import ensure_registered


def with_display_date(data):
    return {**data, "display_at": parse_datetime(data["at"])}


@endpoint(access="authenticated", methods=["GET"])
def stocks(request, initial=False):
    ensure_registered()
    with transaction.atomic():
        actor = lock_actor(actor_context(request))
        stock_reader(actor)
        rows, next_offset = stocks_list(actor, {"mode": "initial" if initial else "all"})
        count = case_query(actor).count()
        return render(
            request,
            "inventory/stocks.html",
            {
                "initial_mode": initial,
                "stock_rows": rows,
                "stock_config": {
                    "actor_id": str(actor.pk),
                    "initial": initial,
                    "can_edit": actor.role == "REPLENISHER",
                    "rows": rows,
                    "next_offset": next_offset,
                },
                "next_offset": next_offset,
                "categories": Category.objects.order_by("name"),
                "open_case_count": count,
                "can_edit": actor.role == "REPLENISHER",
            },
        )


@endpoint(access="authenticated", methods=["GET"])
def activity(request):
    with transaction.atomic():
        actor = lock_actor(actor_context(request))
        query = activity_query(actor)
        if request.GET.get("product_id"):
            from apps.core.payloads import parse_uuid

            query = query.filter(product_id=parse_uuid(request.GET["product_id"]))
        offset = page_offset(request.GET)
        rows = list(query[offset : offset + 51])
        return render(
            request,
            "inventory/activity.html",
            {
                "movements": [with_display_date(entry_data(row)) for row in rows[:50]],
                "next_offset": offset + 50 if len(rows) > 50 else None,
                "product_filter": request.GET.get("product_id", ""),
            },
        )


@endpoint(access="authenticated", methods=["GET"])
def cases(request, case_id=None):
    with transaction.atomic():
        actor = lock_actor(actor_context(request))
        from apps.cases.consultation import list_context
        from apps.cases.views import render_detail

        if case_id:
            source = case_query(actor).filter(pk=case_id).first()
            if source is None:
                raise Http404
            # Les deux objets sont distincts : le lien OneToOne est la source de vérité.
            return render_detail(request, actor, source.case)
        return render(request, "cases/signalements.html", list_context(request, actor, "stock"))
