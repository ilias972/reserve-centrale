"""Présentation en lecture seule des signalements M0–M8, sans nouvelle règle métier."""

from urllib.parse import parse_qsl, urlencode

from django.urls import reverse
from django.utils.translation import gettext, gettext_lazy

from apps.core.errors import BusinessError
from apps.core.presentation import case_navigation
from apps.core.search import matching
from apps.inventory.models import StockBalance, StockEntry
from apps.inventory.selectors import page_offset
from apps.requests.models import RequestLine
from apps.requests.selectors import request_data

from . import reading
from .models import StockHold
from .selectors import case_query

STATUS = {
    "OPEN": gettext_lazy("Ouvert"),
    "UNDER_REVIEW": gettext_lazy("En vérification"),
    "RESOLVED": gettext_lazy("Résolu"),
}
FILTERS = (
    ("pending", gettext_lazy("À traiter")),
    ("resolved", gettext_lazy("Résolus")),
    ("all", gettext_lazy("Tous")),
)
EVENTS = {
    "REVIEWED": gettext_lazy("Examen du dossier"),
    "DELIVERY_CORRECTED": gettext_lazy("Correction des quantités remises"),
    "STOCK_RECONCILED": gettext_lazy("Stock et réservations régularisés"),
    "SHORTAGE_VERIFIED": gettext_lazy("Manque vérifié — dossier résolu"),
    "CORRECTION_BLOCKED": gettext_lazy("Correction refusée — conflit de stock à vérifier"),
}


def stock_title(case):
    rows = list(case.stock_case.products.all())
    kind = (
        gettext("Correction bloquée")
        if any(p.is_calculated for p in rows)
        else gettext("Écart de comptage")
    )
    return kind + " — " + ", ".join(p.product_name for p in rows)


def source_products(case):
    if case.stock_case_id:
        return [p.product_id for p in case.stock_case.products.all()]
    return list(case.delivery.lines.values_list("line__product_id", flat=True))


def current_stocks(case):
    product_ids = source_products(case)
    holds = {
        h.product_id: h
        for h in StockHold.objects.filter(product_id__in=product_ids).select_related("case__case")
    }
    return [
        {
            "id": str(b.product_id),
            "name": b.product.name,
            "unit": b.product.unit,
            "physical": b.physical,
            "reserved": b.reserved,
            "available": b.available,
            "version": b.version,
            "hold": holds.get(b.product_id),
        }
        for b in StockBalance.objects.filter(product_id__in=product_ids)
        .select_related("product")
        .order_by("product__name", "product_id")
    ]


def list_context(request, actor, tab):
    rows = reading.query(actor)
    state = request.GET.get("state", "pending")
    if state not in dict(FILTERS):
        raise BusinessError(
            "INVALID_INPUT", gettext("Choisissez À traiter, Résolus ou Tous."), status=400
        )
    text = request.GET.get("q", "").strip()[:150]
    if tab == "stock":
        rows = rows.filter(stock_case__in=case_query(actor))
        fields = (
            "stock_case__products__product_name",
            "stock_case__products__product_code",
            "origin_operation__actor__display_name",
        )
    else:
        rows = rows.filter(kind__in=["DELIVERY_ERROR", "SHORTAGE"])
        fields = (
            "delivery__request__reference",
            "delivery__request__floor_name",
            "delivery__request__assistant_name",
            "delivery__operator_name",
            "delivery__lines__line__product_name",
            "delivery__lines__line__product_code",
        )
    if state == "pending":
        rows = rows.filter(status__in=["OPEN", "UNDER_REVIEW"])
    elif state == "resolved":
        rows = rows.filter(status="RESOLVED")
    if text:
        # Sous-requête : les jointures de plusieurs produits ne dupliquent jamais une carte.
        rows = rows.filter(pk__in=matching(rows, text, fields).order_by().values("pk"))
    rows = rows.select_related("delivery__request__creation_operation").prefetch_related(
        "stock_case__products",
        "delivery__lines__line",
        "delivery__request__lines__reservation",
    )
    offset = page_offset(request.GET)
    found = list(rows[offset : offset + 51])
    origin = "stock" if tab == "stock" else "deliveries"
    context_query = urlencode({"from": origin, "state": state, "q": text, "offset": offset})
    held_products = set(
        StockHold.objects.filter(
            product_id__in=[
                p.product_id
                for c in found[:50]
                if c.stock_case_id
                for p in c.stock_case.products.all()
            ]
        ).values_list("product_id", flat=True)
    )
    cards = []
    for case in found[:50]:
        card = {
            "id": case.pk,
            "status": case.status,
            "status_label": STATUS[case.status],
            "author": case.origin_operation.actor.display_name,
            "at": case.origin_operation.confirmed_at,
            "url": reverse("case-detail", args=[case.pk]) + "?" + context_query,
        }
        if tab == "stock":
            card.update(
                title=stock_title(case),
                products=case.stock_case.products.all(),
                blocked=any(p.product_id in held_products for p in case.stock_case.products.all()),
            )
        else:
            card.update(
                title=(
                    gettext("Manque signalé")
                    if case.kind == "SHORTAGE"
                    else gettext("Erreur de quantité remise")
                ),
                request=request_data(case.delivery.request, actor),
                operator=case.delivery.operator_name,
                products=case.delivery.lines.all(),
            )
        cards.append(card)
    if text:
        empty = gettext(
            "Aucun signalement ne correspond à votre recherche. Modifiez les mots ou le filtre."
        )
    elif state == "resolved":
        empty = gettext("Aucun signalement résolu dans votre périmètre pour le moment.")
    elif state == "pending":
        empty = gettext("Aucun signalement à traiter dans votre périmètre.")
    else:
        empty = gettext("Aucun signalement dans votre historique accessible.")
    route = reverse("stock-cases" if tab == "stock" else "delivery-issues")

    def page_url(value):
        return route + "?" + urlencode({"state": state, "q": text, "offset": value})

    return {
        "signalement_tab": tab,
        "title": gettext("Signalements de stock")
        if tab == "stock"
        else gettext("Problèmes lors des remises aux unités"),
        "cards": cards,
        "states": FILTERS,
        "state": state,
        "query": text,
        "empty_message": empty,
        "next_url": page_url(offset + 50) if len(found) > 50 else None,
        "previous_url": page_url(max(0, offset - 50)) if offset else None,
    }


def dossier_context(request, actor, case):
    """Contexte actuel distinct des faits initiaux ; aucune écriture à la consultation."""
    stocks = current_stocks(case)
    products = [row["id"] for row in stocks]
    movements = StockEntry.objects.filter(
        product_id__in=products,
        operation__confirmed_at__gt=case.origin_operation.confirmed_at,
    ).select_related("operation__actor")
    if actor.role != "ADMIN":
        movements = movements.filter(operation__actor=actor)
    raw_offset = request.GET.get("movement_offset", "0")
    offset = page_offset({"offset": raw_offset})
    found = list(movements.order_by("-operation__confirmed_at", "id")[offset : offset + 51])
    params = dict(parse_qsl(case_navigation(request, case)["case_context_query"].removeprefix("?")))
    params["movement_offset"] = offset + 50
    events = []
    for event in case.events.select_related("operation__actor", "operation__auditevent").order_by(
        "revision"
    ):
        events.append(
            {
                "title": gettext(EVENTS.get(event.kind, "Décision enregistrée")),
                "actor": event.operation.actor.display_name,
                "at": event.operation.confirmed_at,
                "status": STATUS[event.status],
                "message": event.details.get("message", ""),
                "products": event.operation.auditevent.after.get("products", [])
                if event.kind == "STOCK_RECONCILED"
                else [],
                "releases": [
                    r
                    for r in event.operation.auditevent.after.get("commitments", [])
                    if r.get("release_quantity", 0) > 0
                ]
                if event.kind == "STOCK_RECONCILED" and actor.role == "ADMIN"
                else [],
            }
        )
    result = {
        "signalement_tab": "stock" if case.stock_case_id else "deliveries",
        "dossier_title": stock_title(case)
        if case.stock_case_id
        else ("Manque signalé" if case.kind == "SHORTAGE" else "Erreur de quantité remise"),
        "current_stocks": stocks,
        "blocked": any(row["hold"] for row in stocks),
        "can_verify_shortage": case.kind == "SHORTAGE"
        and case.status != "RESOLVED"
        and bool(stocks)
        and all(row["physical"] is not None and not row["hold"] for row in stocks),
        "case_history": events,
        "last_case_event": events[-1] if events else None,
        "later_movements": found[:50],
        "next_movements_url": "?" + urlencode(params) + "#current-situation"
        if len(found) > 50
        else None,
        "movement_offset": offset,
        "commitments": [],
    }
    if case.delivery_id:
        result["request_data"] = request_data(case.delivery.request, actor)
        result["initial_movements"] = StockEntry.objects.filter(
            operation=case.delivery.operation
        ).order_by("product_name")
    if actor.role == "ADMIN":
        result["commitments"] = (
            RequestLine.objects.filter(product_id__in=products, reservation__remaining__gt=0)
            .select_related("request__assigned_to", "reservation")
            .order_by("request_id", "product_id")
        )
    return result
