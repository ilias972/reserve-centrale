"""Écrans M8 : aucun calcul ou mouvement décidé par un template."""

from django.db import transaction
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.dateparse import parse_datetime
from django.utils.translation import gettext, gettext_lazy

from apps.accounts.views import endpoint
from apps.cases import reading
from apps.cases.models import Case
from apps.catalog.models import Supplier
from apps.core.api import actor_context
from apps.core.errors import BusinessError
from apps.core.payloads import parse_uuid
from apps.core.policies import check_role, lock_actor
from apps.inventory.selectors import page_offset
from apps.purchasing.models import PurchaseOrder

from . import corrections, locks, quarantine, selectors
from .models import ArrivalReport
from .state import CASE_KINDS, NONCONFORMITIES, effective, item_state

TYPES = {
    "SURPLUS": gettext_lazy("Surplus fournisseur"),
    "NONCONFORMITY": gettext_lazy("Produit non conforme"),
    "UNKNOWN_ORDER": gettext_lazy("Commande introuvable"),
    "LATE_CANCELLED_DELIVERY": gettext_lazy("Livraison d’un reliquat annulé"),
    "RECEIPT_ERROR": gettext_lazy("Erreur de réception"),
}
STATUSES = [
    ("OPEN", gettext_lazy("Ouvert")),
    ("UNDER_REVIEW", gettext_lazy("En vérification")),
    ("RESOLVED", gettext_lazy("Résolu")),
]


def actor(request, roles=("ADMIN", "REPLENISHER")):
    user = lock_actor(actor_context(request))
    check_role(user, set(roles))
    return user


def display_case(case):
    data = reading.data(case)
    labels = {
        "REVIEWED": gettext_lazy("Mis en vérification"),
        "QUARANTINE_ACCEPTED": gettext_lazy("Surplus accepté"),
        "QUARANTINE_REFUSED": gettext_lazy("Surplus refusé"),
        "EXTERNAL_VERIFIED": gettext_lazy("Traitement externe vérifié"),
        "UNKNOWN_LINKED": gettext_lazy("Commande identifiée"),
        "REMAINDER_REINSTATED": gettext_lazy("Reliquat rétabli"),
        "ISOLATION_CONSUMED": gettext_lazy("Marchandises réceptionnées"),
        "CORRECTION_BLOCKED": gettext_lazy("Correction bloquée par le stock"),
        "ISOLATION_CORRECTED": gettext_lazy("Quantités isolées corrigées"),
        "RECEIPT_CORRECTED": gettext_lazy("Réception corrigée"),
    }
    for event in data["events"]:
        event["label"] = labels.get(event["kind"], gettext_lazy("Décision historisée"))
        event["display_at"] = parse_datetime(event["at"])
    data["author"] = data["reporter"]
    data.update(
        type_label=TYPES.get(case.kind, case.kind),
        status_label=dict(STATUSES)[case.status],
        display_at=parse_datetime(data["at"]),
    )
    return data


@endpoint(access="authenticated", methods=["GET"])
def listing(request):
    with transaction.atomic():
        user = actor(request)
        offset = page_offset(request.GET)
        rows = list(selectors.orders(user, request.GET)[offset : offset + 51])
        return render(
            request,
            "receiving/orders.html",
            {
                "orders": [selectors.order_data(row) for row in rows[:50]],
                "statuses": PurchaseOrder.Status.choices,
                "filters": request.GET,
                "next_offset": offset + 50 if len(rows) > 50 else None,
            },
        )


@endpoint(access="authenticated", methods=["GET"])
def order_detail(request, order_id):
    with transaction.atomic():
        user = actor(request)
        order = selectors.get_order(user, order_id)
        data = selectors.order_data(order)
        records = selectors.receipts(user).filter(event__order=order)[:50]
        return render(
            request,
            "receiving/order.html",
            {
                "order": data,
                "sources": selectors.available_sources(order),
                "records": [selectors.receipt_data(row) for row in records],
                "can_receive": user.role == "REPLENISHER"
                and order.status in {"WAITING", "PARTIAL"},
                "can_report_extra": user.role == "REPLENISHER"
                and any(not line.waiting and not line.cancelled for line in order.lines.all()),
                "can_report_late": user.role == "REPLENISHER"
                and any(line.cancelled for line in order.lines.all()),
            },
        )


@endpoint(access="authenticated", methods=["GET"])
def records(request):
    with transaction.atomic():
        user = actor(request)
        offset = page_offset(request.GET)
        rows = list(selectors.receipts(user)[offset : offset + 51])
        return render(
            request,
            "receiving/receipts.html",
            {
                "receipts": [selectors.receipt_data(row) for row in rows[:50]],
                "next_offset": offset + 50 if len(rows) > 50 else None,
            },
        )


@endpoint(access="authenticated", methods=["GET"])
def receipt_detail(request, receipt_id):
    with transaction.atomic():
        user = actor(request)
        receipt = selectors.get_receipt(user, receipt_id)
        error = Case.objects.filter(receipt=receipt, kind="RECEIPT_ERROR").first()
        cases = reading.query(user).filter(receipt=receipt)
        return render(
            request,
            "receiving/receipt.html",
            {
                "receipt": selectors.receipt_data(receipt),
                "error_case": error,
                "cases": [display_case(c) for c in cases],
            },
        )


@endpoint(access="authenticated", methods=["GET"])
def cases(request):
    with transaction.atomic():
        user = actor(request)
        rows = reading.query(user).filter(kind__in=CASE_KINDS)
        if request.GET.get("status"):
            rows = rows.filter(status=request.GET["status"])
        if request.GET.get("kind"):
            rows = rows.filter(kind=request.GET["kind"])
        offset = page_offset(request.GET)
        rows = list(rows[offset : offset + 51])
        return render(
            request,
            "receiving/cases.html",
            {
                "cases": [display_case(c) for c in rows[:50]],
                "statuses": STATUSES,
                "types": TYPES.items(),
                "filters": request.GET,
                "next_offset": offset + 50 if len(rows) > 50 else None,
            },
        )


@endpoint(access="authenticated", methods=["GET"])
def case_detail(request, case_id):
    with transaction.atomic():
        user = actor(request)
        case = reading.get(user, case_id)
        if case.kind not in CASE_KINDS:
            return redirect("case-detail", case_id=case.pk)
        report = getattr(case, "arrival_report", None)
        order_id = (
            case.receipt.event.order_id
            if case.receipt_id
            else (locks.report_order(report) if report else None)
        )
        order = selectors.get_order(user, order_id) if order_id else None
        items = [item_state(i.pk) for i in case.quarantine_items.order_by("pk")]
        decisions = [
            d
            for item in case.quarantine_items.all()
            for d in item.decisions.select_related("operation__actor").order_by(
                "operation__confirmed_at"
            )
        ]
        report_resolution = getattr(report, "external_resolution", None) if report else None
        return render(
            request,
            "receiving/case.html",
            {
                "case": display_case(case),
                "report": report,
                "observations": report.lines.all() if report else [],
                "order": selectors.order_data(order) if order else None,
                "items": items,
                "decisions": decisions,
                "report_resolution": report_resolution,
                "receipt": selectors.receipt_data(case.receipt) if case.receipt_id else None,
                "can_link": user.role == "ADMIN"
                and case.kind == "UNKNOWN_ORDER"
                and not getattr(report, "order_link", None)
                and case.status != "RESOLVED",
                "can_reinstate": user.role == "ADMIN"
                and report is not None
                and any(row["remaining"] > row["grant_remaining"] for row in items)
                and order is not None
                and any(line.cancelled for line in order.lines.all()),
                "can_resolve": user.role == "ADMIN"
                and case.kind != "RECEIPT_ERROR"
                and case.status != "RESOLVED"
                and (not items or any(row["remaining"] for row in items)),
                "can_review": user.role == "ADMIN" and case.status != "RESOLVED",
            },
        )


@endpoint(access="authenticated", methods=["GET"])
def editor(request, mode, order_id=None, case_id=None, receipt_id=None, item_id=None):
    with transaction.atomic():
        user = actor(
            request,
            {"REPLENISHER"}
            if mode in {"receive", "unknown", "late", "extra", "report_error", "reminder"}
            else {"ADMIN"},
        )
        cfg = {"actor_id": str(user.pk), "mode": mode, "resource_id": None}
        context = {
            "mode": mode,
            "config": cfg,
            "nonconformities": NONCONFORMITIES.items(),
            "back_url": reverse("receiving-orders"),
        }
        if mode in {"receive", "late", "extra", "reminder"}:
            order = selectors.get_order(user, order_id)
            data = selectors.order_data(order)
            cfg.update(
                resource_id=str(order.pk),
                order=data,
                order_url=f"/api/v1/receiving/orders/{order.pk}",
            )
            context.update(order=data, back_url=reverse("receiving-order", args=[order.pk]))
            if mode == "receive":
                if order.status not in {"WAITING", "PARTIAL"}:
                    raise BusinessError(
                        "ORDER_CLOSED",
                        gettext(
                            "Cette commande est close. Signalez l’arrivée supplémentaire ou le reliquat annulé."
                        ),
                    )
                cfg.update(
                    kind="receipts.create",
                    url=f"/api/v1/orders/{order.pk}/receipts",
                    sources=selectors.available_sources(order),
                )
                context.update(
                    title=gettext("Recevoir cette arrivée"),
                    rows=data["lines"],
                    sources=cfg["sources"],
                )
            elif mode == "reminder":
                if order.status != "PARTIAL":
                    raise BusinessError(
                        "NOT_PARTIAL",
                        gettext("Le rappel concerne les commandes partiellement reçues."),
                    )
                cfg.update(kind="orders.remind", url=f"/api/v1/orders/{order.pk}/reminders")
                context.update(
                    title=gettext("Rappeler la responsable"), last_reminder=data["last_reminder"]
                )
            else:
                rows = (
                    [line for line in data["lines"] if line["cancelled"]]
                    if mode == "late"
                    else [
                        line
                        for line in data["lines"]
                        if not line["waiting"] and not line["cancelled"]
                    ]
                )
                if not rows:
                    raise BusinessError(
                        "NO_ELIGIBLE_LINES",
                        gettext(
                            "Aucune ligne ne correspond à ce signalement. Consultez la commande actualisée."
                        ),
                    )
                cfg.update(
                    kind="receiving.late" if mode == "late" else "receiving.extra",
                    url=f"/api/v1/orders/{order.pk}/{'late-delivery-reports' if mode == 'late' else 'surplus-reports'}",
                )
                context.update(
                    title=(
                        gettext("Signaler le reliquat annulé arrivé")
                        if mode == "late"
                        else gettext("Signaler une arrivée supplémentaire")
                    ),
                    rows=rows,
                )
        elif mode == "unknown":
            cfg.update(kind="receiving.unknown", url="/api/v1/cases/unknown-order")
            context.update(
                title=gettext("Commande introuvable"),
                suppliers=Supplier.objects.order_by("name"),
            )
        elif mode in {"report_error", "open_receipt_case"}:
            receipt = selectors.get_receipt(user, receipt_id)
            existing = Case.objects.filter(receipt=receipt, kind="RECEIPT_ERROR").first()
            if existing:
                return redirect("receiving-case", case_id=existing.pk)
            cfg.update(
                kind="receipts.report_error" if mode == "report_error" else "cases.open",
                resource_id=str(receipt.pk) if mode == "report_error" else None,
                url=f"/api/v1/receipts/{receipt.pk}/reports"
                if mode == "report_error"
                else "/api/v1/cases",
                receipt_id=str(receipt.pk),
            )
            context.update(
                title=gettext("Signaler une erreur de réception")
                if mode == "report_error"
                else gettext("Ouvrir une vérification du reçu"),
                receipt=selectors.receipt_data(receipt),
                back_url=reverse("receiving-receipt", args=[receipt.pk]),
            )
        elif mode in {"accept", "refuse"}:
            scope, item, case = locks.item_scope(user, item_id)
            state = item_state(item.pk)
            if state["remaining"] <= 0 or state["refused"] or item.kind != "SURPLUS":
                raise BusinessError(
                    "ALREADY_DECIDED",
                    gettext(
                        "Ce surplus ne permet plus cette décision. Consultez le dossier actualisé."
                    ),
                )
            cfg.update(
                kind="quarantine.accept_surplus" if mode == "accept" else "quarantine.refuse",
                resource_id=str(item.pk),
                url=f"/api/v1/quarantine/{item.pk}/{'accept-surplus' if mode == 'accept' else 'refuse'}",
                item=state,
                expected_stock_version=scope.stocks[
                    parse_uuid(state["product_id"])
                ].balance.version,
            )
            context.update(
                title=gettext("Accepter tout le surplus restant")
                if mode == "accept"
                else gettext("Refuser le surplus"),
                item=state,
                back_url=reverse("receiving-case", args=[case.pk]),
            )
        else:
            case = reading.get(user, case_id)
            if case.kind not in CASE_KINDS:
                raise BusinessError(
                    "NOT_FOUND", gettext("Dossier fournisseur introuvable."), status=404
                )
            cfg["case_id"] = str(case.pk)
            context.update(
                case=display_case(case), back_url=reverse("receiving-case", args=[case.pk])
            )
            if mode == "link":
                report = ArrivalReport.objects.get(case=case, kind="UNKNOWN_ORDER")
                if getattr(report, "order_link", None) or case.status == "RESOLVED":
                    raise BusinessError(
                        "PARCEL_ALREADY_LINKED", gettext("Ce colis est déjà rattaché ou traité.")
                    )
                if not request.GET.get("order"):
                    offset = page_offset(request.GET)
                    orders = list(selectors.orders(user, request.GET)[offset : offset + 51])
                    return render(
                        request,
                        "receiving/link_order.html",
                        {
                            "case": display_case(case),
                            "orders": [selectors.order_data(o) for o in orders[:50]],
                            "filters": request.GET,
                            "next_offset": offset + 50 if len(orders) > 50 else None,
                        },
                    )
                order = selectors.get_order(user, parse_uuid(request.GET["order"]))
                data = selectors.order_data(order)
                observed = {
                    line.product_id: line.quantity
                    for line in report.lines.all()
                    if line.product_id and line.quantity
                }
                rows = [
                    {**line, "initial_quantity": observed.get(parse_uuid(line["product_id"]), 0)}
                    for line in data["lines"]
                ]
                cfg.update(
                    kind="cases.link_order",
                    resource_id=str(case.pk),
                    url=f"/api/v1/cases/{case.pk}/link-order",
                    order_id=str(order.pk),
                    expected_version=case.version,
                )
                context.update(
                    title=gettext("Identifier et rattacher le colis"),
                    rows=rows,
                    order=data,
                    observations=report.lines.all(),
                )
            elif mode == "resolve":
                _, report, items, locked_case = quarantine.case_resources(user, case.pk)
                cfg.update(
                    kind="receiving.case_resolve",
                    resource_id=str(case.pk),
                    url=f"/api/v1/cases/{case.pk}/resolve",
                    expected_version=locked_case.version,
                    item_versions={str(i.pk): item_state(i.pk)["version"] for i in items},
                )
                context.update(
                    title=gettext("Vérifier le traitement externe"),
                    items=[item_state(i.pk) for i in items],
                )
            elif mode == "reinstate":
                report = ArrivalReport.objects.select_related("order_link").get(case=case)
                order = selectors.get_order(user, locks.report_order(report))
                data = selectors.order_data(order)
                items = {
                    item_state(i.pk)["order_line_id"]: item_state(i.pk)
                    for i in case.quarantine_items.all()
                }
                rows = [
                    {
                        **line,
                        "maximum": min(
                            line["cancelled"],
                            max(
                                0,
                                items[line["id"]]["remaining"]
                                - items[line["id"]]["grant_remaining"],
                            ),
                        ),
                    }
                    for line in data["lines"]
                    if line["id"] in items
                ]
                cfg.update(
                    kind="orders.reinstate_remainder",
                    resource_id=str(order.pk),
                    url=f"/api/v1/orders/{order.pk}/reinstate-remainder",
                    expected_version=order.version,
                    report_id=str(report.pk),
                    expected_report_version=case.version,
                )
                context.update(
                    title=gettext("Rétablir le reliquat après vérification"), rows=rows, order=data
                )
            elif mode == "correction":
                if not case.receipt_id or case.kind != "RECEIPT_ERROR":
                    raise BusinessError(
                        "INVALID_SOURCE",
                        gettext("Le dossier ne concerne pas une erreur de réception."),
                    )
                data = corrections.resources(user, case.receipt_id, {"case_id": str(case.pk)})
                rows = []
                for line in data.lines.values():
                    rows.append(
                        {
                            "id": str(line.pk),
                            "name": line.order_line.product_name,
                            "unit": line.order_line.unit,
                            "initial": {
                                key: getattr(line, key)
                                for key in ["conforming", "nonconforming", "regular", "surplus"]
                            },
                            "effective": effective(line),
                        }
                    )
                cfg.update(
                    kind="corrections.confirm",
                    resource_id=str(case.receipt_id),
                    url="/api/v1/corrections/confirm",
                    preview_url="/api/v1/corrections/preview",
                    source_id=str(case.receipt_id),
                    expected_versions={
                        "order": data.order_scope.order.version,
                        "receipt": data.receipt.version,
                        "case": case.version,
                        "stocks": {
                            str(line.order_line.product_id): data.stocks[
                                line.order_line.product_id
                            ].balance.version
                            for line in data.lines.values()
                        },
                        "quarantine": {str(iid): item_state(iid)["version"] for iid in data.items},
                    },
                )
                context.update(
                    title=gettext("Vérifier et corriger le reçu"),
                    rows=rows,
                    receipt=selectors.receipt_data(data.receipt),
                )
            elif mode == "review":
                cfg.update(
                    kind="cases.review",
                    resource_id=str(case.pk),
                    url=f"/api/v1/cases/{case.pk}/review",
                    expected_version=case.version,
                )
                context.update(title=gettext("Mettre le dossier en vérification"))
            else:
                raise BusinessError("NOT_FOUND", gettext("Action indisponible."), status=404)
        return render(request, "receiving/editor.html", context)
