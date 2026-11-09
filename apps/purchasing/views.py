from collections import OrderedDict

from django.db import transaction
from django.http import Http404
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext

from apps.accounts.views import endpoint
from apps.catalog.models import Supplier
from apps.catalog.policies import catalog_admin
from apps.catalog.selectors import get_product
from apps.core.api import actor_context
from apps.core.payloads import parse_uuid
from apps.core.policies import lock_actor
from apps.inventory.selectors import page_offset

from . import recommendations, selectors
from .models import PurchaseOrder, RecommendationEvent


def admin(request):
    actor = lock_actor(actor_context(request))
    catalog_admin(actor)
    return actor


def display_order(order):
    waiting_lines = [line for line in order.lines.all() if line.waiting]
    return {
        **selectors.order_data(order),
        "purchased_on_display": order.purchased_on,
        "registered_at_display": order.creation_operation.confirmed_at,
        "single_waiting_line": waiting_lines[0] if len(waiting_lines) == 1 else None,
    }


@endpoint(access="authenticated", methods=["GET"])
def needs(request):
    with transaction.atomic():
        actor = admin(request)
        rows, next_offset = selectors.needs(actor, request.GET)
        suppliers = list(Supplier.objects.filter(active=True).order_by("name"))
        supplier_sites = {str(supplier.pk): supplier.website for supplier in suppliers}
        groups = OrderedDict()
        for row in rows:
            supplier = row["group_supplier"]
            key = supplier["id"] if supplier else None
            if key not in groups:
                groups[key] = {
                    "supplier": supplier,
                    "website": supplier_sites.get(key, ""),
                    "rows": [],
                }
            groups[key]["rows"].append(row)
        return render(
            request,
            "purchasing/needs.html",
            {
                "groups": list(groups.values()),
                "next_offset": next_offset,
                "filters": request.GET,
                "states": recommendations.LABELS.items(),
                "suppliers": suppliers,
            },
        )


@endpoint(access="authenticated", methods=["GET"])
def need_detail(request, product_id):
    with transaction.atomic():
        admin(request)
        product = recommendations.query().filter(pk=product_id).first()
        if product is None:
            raise Http404
        offset = page_offset(request.GET)
        events = list(
            RecommendationEvent.objects.filter(recommendation_id=product_id)
            .select_related("operation__actor")
            .order_by("-revision")[offset : offset + 51]
        )
        need = recommendations.data(product)
        for supplier, link in zip(need["suppliers"], product.available_suppliers, strict=True):
            supplier["link_is_product"] = bool(link.website)
        return render(
            request,
            "purchasing/need.html",
            {
                "need": need,
                "events": events[:50],
                "next_offset": offset + 50 if len(events) > 50 else None,
            },
        )


@endpoint(access="authenticated", methods=["GET"])
def orders(request):
    with transaction.atomic():
        actor = admin(request)
        offset = page_offset(request.GET)
        rows = list(selectors.order_query(actor, request.GET)[offset : offset + 51])
        return render(
            request,
            "purchasing/orders.html",
            {
                "orders": [display_order(o) for o in rows[:50]],
                "filters": request.GET,
                "statuses": PurchaseOrder.Status.choices,
                "next_offset": offset + 50 if len(rows) > 50 else None,
            },
        )


@endpoint(access="authenticated", methods=["GET"])
def detail(request, order_id):
    with transaction.atomic():
        actor = admin(request)
        order = selectors.get_order(actor, order_id)
        offset = page_offset(request.GET)
        events = list(
            order.events.select_related("operation__actor")
            .prefetch_related("lines__line")
            .order_by("-revision")[offset : offset + 51]
        )
        return render(
            request,
            "purchasing/order.html",
            {
                "order": display_order(order),
                "events": events[:50],
                "next_offset": offset + 50 if len(events) > 50 else None,
                "ack": getattr(order, "duplicateacknowledgement", None),
            },
        )


@endpoint(access="authenticated", methods=["GET"])
def editor(request, mode="purchase", product_id=None, order_id=None):
    with transaction.atomic():
        actor = admin(request)
        context = {"mode": mode}
        cfg = {"actor_id": str(actor.pk), "mode": mode}
        if mode == "purchase":
            cfg.update(kind="orders.create", resource_id=None, url="/api/v1/orders")
            context.update(
                title=gettext("Enregistrer un achat réellement passé"),
                back_url=reverse("purchase-orders"),
                suppliers=Supplier.objects.order_by("name"),
                today=timezone.localdate(),
                selected_supplier=request.GET.get("supplier", ""),
            )
            if request.GET.get("product"):
                product = get_product(actor, parse_uuid(request.GET["product"]))
                rec = recommendations.data(recommendations.query().get(pk=product.pk))
                context["back_url"] = reverse("purchase-need", args=[product.pk])
                context["back_label"] = gettext("Retour au besoin « %(product)s »") % {
                    "product": product.name
                }
                cfg["initial_product"] = {
                    **selectors.purchase_product_data(product),
                    "recommended": rec["recommended"],
                }

        elif mode == "cancel":
            order = selectors.get_order(actor, order_id)
            d = display_order(order)
            if not any(line["waiting"] for line in d["lines"]):
                raise Http404
            cfg.update(
                kind="orders.cancel_remainder",
                resource_id=str(order.pk),
                url=f"/api/v1/orders/{order.pk}/cancel-remainder",
                expected_version=order.version,
                lines=d["lines"],
            )
            context.update(
                title=gettext("Annuler les quantités restantes"),
                back_url=reverse("purchase-order", args=[order.pk]),
                order=d,
            )
        else:
            product = recommendations.query().filter(pk=product_id).first()
            if product is None or mode not in {"defer", "reactivate"}:
                raise Http404
            row = recommendations.data(product)
            if row["state"] != ("ACTIVE" if mode == "defer" else "DEFERRED"):
                raise Http404
            cfg.update(
                kind="recommendations." + mode,
                resource_id=str(product_id),
                url=f"/api/v1/recommendations/{product_id}/{mode}",
                expected_version=row["version"],
                state_fingerprint=row["state_fingerprint"],
            )
            context.update(
                title=(
                    gettext("Mettre ce besoin en attente")
                    if mode == "defer"
                    else gettext("Réactiver ce besoin")
                ),
                back_url=reverse("purchase-need", args=[product_id]),
                need=row,
            )
        context["config"] = cfg
        return render(request, "purchasing/editor.html", context)
