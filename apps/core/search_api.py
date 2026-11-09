"""Suggestions bornées, uniquement dans le périmètre de lecture de l'acteur."""

from django.db import transaction
from django.http import JsonResponse

from apps.accounts.models import User
from apps.catalog.models import Category, Floor, Product, Supplier
from apps.catalog.policies import catalog_admin, visible_products
from apps.core.payloads import invalid_input, parse_uuid
from apps.core.policies import check_role, lock_actor
from apps.requests.selectors import filtered_requests

from .api import actor_context, endpoint
from .search import matching, normalized


@endpoint(access="authenticated", methods=["GET"])
def suggestions(request):
    scope = request.GET.get("scope")
    if scope not in {
        "products",
        "stocks",
        "requests",
        "users",
        "category",
        "floor",
        "supplier",
        "orders",
        "receiving-orders",
    }:
        raise invalid_input()
    text = request.GET.get("q", "")[:150]
    with transaction.atomic():
        actor = lock_actor(actor_context(request))
        results = []
        if scope in {"products", "stocks"}:
            if scope == "stocks":
                check_role(actor, {"ADMIN", "REPLENISHER"})
                query = Product.objects.all()
                mode = request.GET.get("mode")
                if mode == "initial":
                    query = query.filter(counted_at__isnull=True)
                elif mode == "counted":
                    query = query.filter(counted_at__isnull=False)
                elif mode not in {None, "", "all"}:
                    raise invalid_input()
            else:
                query = visible_products(actor)
            if request.GET.get("category"):
                query = query.filter(category_id=parse_uuid(request.GET["category"]))
            if request.GET.get("status"):
                if request.GET["status"] not in Product.Status.values:
                    raise invalid_input()
                query = query.filter(status=request.GET["status"])
            if normalized(text):
                rows = matching(query, text, ("name", "code")).order_by(
                    "_search_rank", "name", "pk"
                )[:8]
                results = [
                    {
                        "value": row.code,
                        "label": f"{row.name} · {row.code}",
                        "photo_url": f"/api/v1/products/{row.pk}/photo?thumbnail=1"
                        if row.current_photo_id
                        else None,
                    }
                    for row in rows
                ]
        elif scope == "receiving-orders":
            from apps.receiving.selectors import orders

            check_role(actor, {"ADMIN", "REPLENISHER"})
            rows = orders(actor, request.GET)[:8] if normalized(text) else []
            results = [
                {"value": o.reference, "label": f"{o.reference} · {o.supplier_name}"} for o in rows
            ]
        elif scope == "orders":
            from apps.purchasing.selectors import order_query

            rows = order_query(actor, request.GET)[:8] if normalized(text) else []
            # Contrôle du rôle également pour une recherche vide.
            catalog_admin(actor)
            results = [
                {"value": o.reference, "label": f"{o.reference} · {o.supplier_name}"} for o in rows
            ]
        elif scope == "requests":
            rows = filtered_requests(actor, request.GET)[:8] if normalized(text) else []
            results = [
                {
                    "value": row.reference,
                    "label": f"{row.reference} · {', '.join(line.product_name for line in list(row.lines.all())[:3])}",
                }
                for row in rows
            ]
        else:
            catalog_admin(actor)
            model, fields, key = {
                "users": (User, ("display_name", "username"), "username"),
                "category": (Category, ("name",), "name"),
                "floor": (Floor, ("name", "code"), "code"),
                "supplier": (Supplier, ("name", "code"), "code"),
            }[scope]
            rows = (
                matching(model.objects.all(), text, fields).order_by(
                    "_search_rank", fields[0], "pk"
                )[:8]
                if normalized(text)
                else []
            )
            for row in rows:
                name = row.display_name if scope == "users" else row.name
                value = getattr(row, key)
                results.append(
                    {"value": value, "label": name if name == value else f"{name} · {value}"}
                )
        return JsonResponse({"suggestions": results})
