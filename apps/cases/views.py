from django.db import transaction
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.translation import gettext, gettext_lazy

from apps.accounts.models import User
from apps.accounts.views import endpoint
from apps.core.api import actor_context
from apps.core.errors import BusinessError
from apps.core.policies import check_role, lock_actor
from apps.core.presentation import case_navigation
from apps.inventory.models import StockBalance, StockEntry
from apps.requests.corrections import OPEN, effective
from apps.requests.models import Delivery, InternalRequest, RequestLine
from apps.requests.selectors import request_data

from . import reading
from .models import Case

LABELS = {
    "OPEN": gettext_lazy("Ouvert"),
    "UNDER_REVIEW": gettext_lazy("En vérification"),
    "RESOLVED": gettext_lazy("Résolu"),
}
TYPES = {
    "STOCK_CONFLICT": gettext_lazy("Stock incompatible avec les réservations"),
    "DELIVERY_ERROR": gettext_lazy("Erreur de livraison"),
    "SHORTAGE": gettext_lazy("Manque physique signalé"),
    "SURPLUS": gettext_lazy("Surplus fournisseur"),
    "NONCONFORMITY": gettext_lazy("Produit non conforme"),
    "UNKNOWN_ORDER": gettext_lazy("Commande introuvable"),
    "LATE_CANCELLED_DELIVERY": gettext_lazy("Livraison d’un reliquat annulé"),
    "RECEIPT_ERROR": gettext_lazy("Erreur de réception"),
}


def display(case):
    from django.utils.dateparse import parse_datetime

    d = reading.data(case)
    d["display_at"] = parse_datetime(d["at"])
    for event in d["events"]:
        event["display_at"] = parse_datetime(event["at"])
    return {**d, "status_label": LABELS[d["status"]], "type_label": TYPES[d["type"]]}


@endpoint(access="authenticated", methods=["GET"])
def listing(request):
    with transaction.atomic():
        actor = lock_actor(actor_context(request))
        rows = reading.query(actor)
        if request.GET.get("status"):
            rows = rows.filter(status=request.GET["status"])
        from apps.inventory.selectors import page_offset

        offset = page_offset(request.GET)
        found = list(rows[offset : offset + 51])
        return render(
            request,
            "cases/list.html",
            {
                "cases": [display(c) for c in found[:50]],
                "statuses": LABELS.items(),
                "filters": request.GET,
                "next_offset": offset + 50 if len(found) > 50 else None,
            },
        )


def render_detail(request, actor, case):
    from apps.receiving.state import CASE_KINDS

    from .consultation import dossier_context

    if case.kind in CASE_KINDS:
        return redirect("receiving-case", case_id=case.pk)
    navigation = case_navigation(request, case)
    cfg = {
        "actor_id": str(actor.pk),
        "mode": "review",
        "kind": "cases.review",
        "resource_id": str(case.pk),
        "url": f"/api/v1/cases/{case.pk}/review",
        "expected_version": case.version,
        "success_url": reverse("case-detail", args=[case.pk]) + navigation["case_context_query"],
        "auto_return": True,
    }
    return render(
        request,
        "cases/detail.html",
        {
            "case": display(case),
            **navigation,
            **dossier_context(request, actor, case),
            "config": cfg,
            "can_admin": actor.role == "ADMIN",
            "can_review": actor.role == "ADMIN"
            and not (case.kind == "STOCK_CONFLICT" and case.status == "RESOLVED"),
            "delivery": case.delivery,
            "corrections": case.delivery.corrections.select_related(
                "decision__event__operation__actor"
            )
            .prefetch_related("lines__delivery_line__line")
            .order_by("decision__event__revision")
            if case.delivery_id
            else [],
        },
    )


@endpoint(access="authenticated", methods=["GET"])
def detail(request, case_id):
    with transaction.atomic():
        actor = lock_actor(actor_context(request))
        return render_detail(request, actor, reading.get(actor, case_id))


@endpoint(access="authenticated", methods=["GET"])
def verify_shortage(request, case_id):
    from .consultation import current_stocks

    with transaction.atomic():
        actor = lock_actor(actor_context(request))
        check_role(actor, {"ADMIN"})
        case = reading.get(actor, case_id)
        if case.kind != "SHORTAGE" or case.status == "RESOLVED":
            raise BusinessError(
                "INVALID_STATE", gettext("Ce dossier ne nécessite pas de vérification du manque.")
            )
        rows = current_stocks(case)
        navigation = case_navigation(request, case)
        return render(
            request,
            "cases/verify_shortage.html",
            {
                "case": display(case),
                **navigation,
                "rows": rows,
                "blocked": any(row["hold"] or row["physical"] is None for row in rows),
                "config": {
                    "actor_id": str(actor.pk),
                    "mode": "resolve",
                    "kind": "cases.resolve",
                    "resource_id": str(case.pk),
                    "expected_version": case.version,
                    "url": f"/api/v1/cases/{case.pk}/resolve",
                    "product_counts": [
                        {"product_id": row["id"], "expected_stock_version": row["version"]}
                        for row in rows
                    ],
                    "current_stocks": rows if not any(row["hold"] for row in rows) else [],
                    "success_url": reverse("case-detail", args=[case.pk])
                    + navigation["case_context_query"],
                    "auto_return": True,
                }
                if not any(row["hold"] or row["physical"] is None for row in rows)
                else None,
            },
        )


@endpoint(access="authenticated", methods=["GET"])
def from_delivery(request, delivery_id):
    with transaction.atomic():
        actor = lock_actor(actor_context(request))
        check_role(actor, {"ADMIN"})
        source = (
            Delivery.objects.select_related("request", "operation").filter(pk=delivery_id).first()
        )
        if source is None:
            raise BusinessError("NOT_FOUND", gettext("Livraison introuvable."), status=404)
        existing = Case.objects.filter(delivery=source, kind="DELIVERY_ERROR").first()
        if existing:
            return redirect("case-detail", case_id=existing.pk)
        return render(
            request,
            "cases/editor.html",
            {
                "title": gettext("Ouvrir une vérification de livraison"),
                "source": source,
                "config": {
                    "actor_id": str(actor.pk),
                    "mode": "open",
                    "kind": "cases.open",
                    "resource_id": None,
                    "url": "/api/v1/cases",
                    "source_id": str(source.pk),
                },
                "rows": [],
            },
        )


@endpoint(access="authenticated", methods=["GET"])
def editor(request, case_id, mode):
    with transaction.atomic():
        actor = lock_actor(actor_context(request))
        check_role(actor, {"ADMIN"})
        case = reading.get(actor, case_id)
        cfg = {
            "actor_id": str(actor.pk),
            "mode": mode,
            "case_id": str(case.pk),
            "resource_id": str(case.pk),
            "success_url": reverse("case-detail", args=[case.pk]),
            "expected_case_version": case.version,
        }
        navigation = case_navigation(request, case)
        cfg["success_url"] += navigation["case_context_query"]
        cfg["auto_return"] = True
        context = {
            "case": display(case),
            "config": cfg,
            "rows": [],
            "commitments": [],
            **navigation,
        }
        if mode == "correction":
            if not case.delivery_id:
                raise BusinessError(
                    "INVALID_STATE", gettext("Ce dossier ne concerne pas une livraison.")
                )
            delivery = case.delivery
            r = delivery.request
            lines = list(r.lines.select_related("reservation"))
            stocks = StockBalance.objects.filter(
                product_id__in=[line_item.product_id for line_item in lines]
            )
            cfg.update(
                kind="corrections.confirm",
                resource_id=str(delivery.pk),
                url="/api/v1/corrections/confirm",
                preview_url="/api/v1/corrections/preview",
                source_id=str(delivery.pk),
                expected_versions={
                    "request": r.version,
                    "case": case.version,
                    "stocks": {str(s.product_id): s.version for s in stocks},
                },
                closed=r.status not in OPEN,
            )
            context.update(
                title=gettext("Corriger la quantité réellement remise"),
                source=delivery,
                rows=[
                    {
                        "id": str(line_item.pk),
                        "name": line_item.line.product_name,
                        "unit": line_item.line.unit,
                        "initial": line_item.handed,
                        "current": effective(line_item),
                        "maximum": line_item.line.quantity,
                    }
                    for line_item in delivery.lines.select_related("line")
                ],
                request_data=request_data(r),
                subsequent=StockEntry.objects.filter(
                    product_id__in=[line_item.product_id for line_item in lines],
                    operation__confirmed_at__gt=delivery.operation.confirmed_at,
                )
                .select_related("operation")
                .order_by("-operation__confirmed_at")[:30],
            )
        else:
            if not case.stock_case_id or case.status == "RESOLVED":
                raise BusinessError(
                    "INVALID_STATE", gettext("Ce dossier ne nécessite pas de régularisation.")
                )
            products = list(case.stock_case.products.select_related("product__stock"))
            cfg.update(
                kind="stock_reconciliations.confirm",
                url="/api/v1/stock-reconciliations/confirm",
                preview_url="/api/v1/stock-reconciliations/preview",
                product_counts=[
                    {
                        "product_id": str(p.product_id),
                        "expected_stock_version": p.product.stock.version,
                        "counted_quantity": None,
                    }
                    for p in products
                ],
            )
            commitments = list(
                RequestLine.objects.filter(
                    product_id__in=[p.product_id for p in products], reservation__remaining__gt=0
                )
                .select_related("request__assigned_to", "reservation")
                .order_by("request_id", "product_id")
            )
            cfg["commitments"] = [
                {
                    "request_id": str(line_item.request_id),
                    "request_line_id": str(line_item.pk),
                    "expected_request_version": line_item.request.version,
                    "release_quantity": 0,
                }
                for line_item in commitments
            ]
            context.update(
                title=gettext("Régulariser le stock et les engagements"),
                rows=[
                    {
                        "id": str(p.product_id),
                        "name": p.product_name,
                        "unit": p.unit,
                        "physical": p.product.stock.physical,
                        "reserved": p.product.stock.reserved,
                        "current": p.attempted_physical if not p.is_calculated else "",
                        "is_calculated": p.is_calculated,
                    }
                    for p in products
                ],
                commitments=commitments,
            )
        return render(request, "cases/editor.html", context)


@endpoint(access="authenticated", methods=["GET"])
def request_editor(request, request_id, action):
    with transaction.atomic():
        actor = lock_actor(actor_context(request))
        check_role(actor, {"ADMIN"})
        r = InternalRequest.objects.filter(pk=request_id).first()
        if r is None:
            raise BusinessError("NOT_FOUND", gettext("Demande introuvable."), status=404)
        labels = {
            "close-remainder": gettext_lazy("Clôturer le reliquat pour abandon"),
            "reassign": gettext_lazy("Réattribuer le reste à livrer"),
            "reopen": gettext_lazy("Rouvrir la demande"),
        }
        if action not in labels:
            raise BusinessError("NOT_FOUND", gettext("Action introuvable."), status=404)
        cfg = {
            "actor_id": str(actor.pk),
            "mode": action,
            "kind": {
                "close-remainder": "requests.close_remainder",
                "reassign": "requests.reassign",
                "reopen": "requests.reopen",
            }[action],
            "resource_id": str(r.pk),
            "url": f"/api/v1/requests/{r.pk}/{action}",
            "preview_url": f"/api/v1/requests/{r.pk}/{action}/preview",
            "expected_version": r.version,
            "success_url": reverse("request-detail", args=[r.pk]),
        }
        return render(
            request,
            "cases/editor.html",
            {
                "title": labels[action],
                "request_data": request_data(r),
                "config": cfg,
                "operators": User.objects.filter(role="REPLENISHER", is_active=True).exclude(
                    pk=r.assigned_to_id
                ),
                "rows": [],
            },
        )
