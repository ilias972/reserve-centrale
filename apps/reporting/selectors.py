"""Datasets explicites : chaque ligne conserve référence, produit, unité et origine."""

from datetime import timedelta

from django.db.models import BigIntegerField, Count, F, OuterRef, Q, QuerySet, Subquery, Sum, Value
from django.db.models.functions import Coalesce
from django.utils.translation import gettext, gettext_lazy, gettext_noop

from apps.cases import classification, reading
from apps.core.errors import BusinessError
from apps.core.models import Operation
from apps.core.policies import check_role
from apps.inventory.models import StockBalance, StockEntry
from apps.purchasing.models import OrderLine, PurchaseOrder
from apps.receiving.models import ManualReminder, QuarantineDecision, ReceiptCorrectionLine
from apps.receiving.selectors import effective_receipts
from apps.requests.models import RequestEvent, RequestLine
from apps.requests.selectors import effective_deliveries

from .filters import limit, refused
from .iteration import stream

ROLES = {
    "ADMIN": gettext_lazy("Gestionnaire"),
    "ASSISTANT": gettext_lazy("Demandeur — profil A"),
    "DOCTOR": gettext_lazy("Demandeur — profil B"),
    "REPLENISHER": gettext_lazy("Préparateur"),
}
KINDS = {
    "SURPLUS": gettext_lazy("Surplus fournisseur"),
    "DELIVERY_ERROR": gettext_lazy("Erreur de remise"),
    "RECEIPT_ERROR": gettext_lazy("Erreur de réception"),
    "STOCK_CONFLICT": gettext_lazy("Conflit de stock"),
    "SHORTAGE": gettext_lazy("Manque physique"),
    "NONCONFORMITY": gettext_lazy("Non-conformité"),
    "UNKNOWN_ORDER": gettext_lazy("Commande introuvable"),
    "LATE_CANCELLED_DELIVERY": gettext_lazy("Livraison tardive"),
}
STATES = {
    "OPEN": gettext_lazy("Ouvert"),
    "UNDER_REVIEW": gettext_lazy("En vérification"),
    "RESOLVED": gettext_lazy("Résolu"),
}
# Colonnes explicitement autorisées : jamais de payload JSON, secret ou session.
SCHEMAS = {
    "stocks": (
        gettext_noop("État actuel des stocks"),
        [
            gettext_noop("Référence produit"),
            gettext_noop("Produit"),
            gettext_noop("Unité"),
            gettext_noop("État"),
            gettext_noop("Stock en réserve"),
            gettext_noop("Réservé"),
            gettext_noop("Disponible"),
            gettext_noop("Stock bas à"),
            gettext_noop("Quantité souhaitée"),
            gettext_noop("Vérification"),
        ],
    ),
    "movements": (
        gettext_noop("Mouvements de réserve"),
        [
            gettext_noop("Date"),
            gettext_noop("Produit"),
            gettext_noop("Référence produit"),
            gettext_noop("Unité"),
            gettext_noop("Action"),
            gettext_noop("Stock avant"),
            gettext_noop("Stock après"),
            gettext_noop("Variation en réserve"),
            gettext_noop("Réservé avant"),
            gettext_noop("Réservé après"),
            gettext_noop("Variation réservée"),
            gettext_noop("Auteur"),
            gettext_noop("Rôle"),
        ],
    ),
    "requests": (
        gettext_noop("Demandes enregistrées"),
        [
            gettext_noop("Date"),
            gettext_noop("Demande"),
            gettext_noop("Demandeur"),
            gettext_noop("Rôle à la demande"),
            gettext_noop("Unité d’origine"),
            gettext_noop("Produit"),
            gettext_noop("Référence produit"),
            gettext_noop("Unité"),
            gettext_noop("Demandé"),
            gettext_noop("Remis effectif"),
            gettext_noop("Clôturé"),
            gettext_noop("Encore réservé"),
            gettext_noop("État"),
        ],
    ),
    "deliveries": (
        gettext_noop("Quantités effectivement remises"),
        [
            gettext_noop("Date d’origine"),
            gettext_noop("Demande"),
            gettext_noop("Unité d’origine"),
            gettext_noop("Rôle demandeur"),
            gettext_noop("Produit"),
            gettext_noop("Référence produit"),
            gettext_noop("Unité"),
            gettext_noop("Déclaré initialement"),
            gettext_noop("Remis effectif"),
            gettext_noop("Opérateur"),
        ],
    ),
    "purchases": (
        gettext_noop("Achats réellement enregistrés"),
        [
            gettext_noop("Date d’achat"),
            gettext_noop("Commande"),
            gettext_noop("Fournisseur"),
            gettext_noop("Numéro fournisseur"),
            gettext_noop("Montant"),
            gettext_noop("Devise"),
            gettext_noop("État"),
            gettext_noop("Auteur"),
            gettext_noop("Dernier rappel enregistré"),
        ],
    ),
    "orderlines": (
        gettext_noop("Lignes des achats fournisseurs"),
        [
            gettext_noop("Date d’achat"),
            gettext_noop("Commande"),
            gettext_noop("Fournisseur"),
            gettext_noop("Produit"),
            gettext_noop("Référence produit"),
            gettext_noop("Unité"),
            gettext_noop("Commandé"),
            gettext_noop("Reçu régulier"),
            gettext_noop("Annulé"),
            gettext_noop("Encore attendu"),
            gettext_noop("État"),
        ],
    ),
    "receipts": (
        gettext_noop("Réceptions régulières effectives"),
        [
            gettext_noop("Date d’origine"),
            gettext_noop("Réception"),
            gettext_noop("Commande"),
            gettext_noop("Fournisseur"),
            gettext_noop("Produit"),
            gettext_noop("Référence produit"),
            gettext_noop("Unité"),
            gettext_noop("Régulier initial"),
            gettext_noop("Régulier effectif"),
            gettext_noop("Auteur"),
            gettext_noop("Non conformes effectifs — hors stock"),
            gettext_noop("Surplus de l’arrivée — distinct du régulier"),
        ],
    ),
    "surplus": (
        gettext_noop("Surplus acceptés séparément"),
        [
            gettext_noop("Date de décision"),
            gettext_noop("Dossier"),
            gettext_noop("Produit"),
            gettext_noop("Unité"),
            gettext_noop("Surplus accepté"),
            gettext_noop("Auteur"),
        ],
    ),
    "cases": (
        gettext_noop("Dossiers et signalements"),
        [
            gettext_noop("Date du signalement"),
            gettext_noop("Dossier"),
            gettext_noop("Nature"),
            gettext_noop("Catégorie principale"),
            gettext_noop("État"),
            gettext_noop("Auteur"),
        ],
    ),
    "history": (
        gettext_noop("Historique des opérations"),
        [
            gettext_noop("Date réelle"),
            gettext_noop("Auteur"),
            gettext_noop("Rôle au moment de l’action"),
            gettext_noop("Action"),
            gettext_noop("Objet"),
            gettext_noop("Variations vérifiables"),
            gettext_noop("Correction de"),
        ],
    ),
}


def dataset(name):
    if name not in SCHEMAS:
        raise BusinessError("NOT_FOUND", gettext("Analyse inconnue."), status=404)
    title, columns = SCHEMAS[name]
    return gettext(title), [gettext(column) for column in columns]


def period(query, field, f):
    return query.filter(**{field + "__gte": f.lower, field + "__lt": f.upper})


def operation_filter(query, prefix, f):
    if f.actor:
        query = query.filter(**{prefix + "actor_id": f.actor})
    if f.role:
        query = query.filter(**{prefix + "actor_role": f.role})
    if f.action:
        query = query.filter(**{prefix + "command_type": f.action})
    return query


def history_query(actor, f, personal=False):
    q = (
        Operation.objects.select_related(
            "actor",
            "auditevent",
            "purchaseorder",
            "orderevent__order",
            "orderevent__receipt",
            "orderevent__receipt_correction__receipt",
        )
        .defer("actor__password", "auditevent__before", "auditevent__details")
        .prefetch_related("stock_entries", "requestevent_set__request")
    )
    if personal and actor.role == "ADMIN":
        q = q.filter(actor=actor)
    if actor.role != "ADMIN":
        if actor.is_requester:
            owned = RequestEvent.objects.filter(request__assistant=actor).values("operation_id")
            q = q.filter(Q(actor=actor) | Q(pk__in=owned))
        else:
            check_role(actor, {"REPLENISHER"})
            q = q.filter(actor=actor)
    q = operation_filter(period(q, "confirmed_at", f), "", f)
    if f.product:
        q = q.filter(stock_entries__product_id=f.product).distinct()
    if f.floor:
        q = q.filter(
            pk__in=RequestEvent.objects.filter(request__floor_id=f.floor).values("operation_id")
        )
    if f.q:
        related = RequestEvent.objects.filter(request__reference__icontains=f.q)
        if actor.is_requester:
            related = related.filter(request__assistant=actor)
        match = (
            Q(pk__in=related.values("operation_id"))
            | Q(command_type__icontains=f.q)
            | Q(actor__display_name__icontains=f.q)
        )
        if not actor.is_requester:
            from apps.receiving.models import OrderEvent

            match |= Q(
                pk__in=PurchaseOrder.objects.filter(reference__icontains=f.q).values(
                    "creation_operation_id"
                )
            )
            match |= Q(
                pk__in=OrderEvent.objects.filter(order__reference__icontains=f.q).values(
                    "operation_id"
                )
            )
            match |= Q(stock_entries__product_name__icontains=f.q) | Q(
                stock_entries__product_code__icontains=f.q
            )
            from apps.cases.models import Case, CaseEvent
            from apps.purchasing.models import RemainderEvent
            from apps.receiving.models import ArrivalReport, UnknownLink

            for model, field in [
                (ManualReminder, "order__reference"),
                (RemainderEvent, "order__reference"),
                (ArrivalReport, "order__reference"),
                (UnknownLink, "order__reference"),
            ]:
                match |= Q(
                    pk__in=model.objects.filter(**{field + "__icontains": f.q}).values(
                        "operation_id"
                    )
                )
            linked_cases = Case.objects.filter(
                Q(delivery__request__reference__icontains=f.q)
                | Q(receipt__event__order__reference__icontains=f.q)
                | Q(arrival_report__order__reference__icontains=f.q)
                | Q(arrival_report__order_link__order__reference__icontains=f.q)
                | Q(stock_case__products__product_name__icontains=f.q)
            )
            match |= Q(pk__in=linked_cases.values("origin_operation_id"))
            match |= Q(
                pk__in=CaseEvent.objects.filter(case__in=linked_cases).values("operation_id")
            )
            decisions = QuarantineDecision.objects.filter(
                Q(item__case__in=linked_cases) | Q(report__case__in=linked_cases)
            )
            match |= Q(pk__in=decisions.values("operation_id"))
        q = q.filter(match).distinct()
    return q.order_by("-confirmed_at", "-id")


def history_page(actor, f, personal=False):
    """Le compte est global au filtre ; seuls 50 objets et leurs liens sont projetés."""
    from .history import rows as history_rows

    if not personal:
        check_role(actor, {"ADMIN"})
    validate_filters(actor, "history", f, personal)
    query = history_query(actor, f, personal)
    count = query.count()
    start = (f.page - 1) * 50
    return history_rows(actor, query[start : start + 50]), count


def request_filters(q, prefix, f):
    if f.floor:
        q = q.filter(**{prefix + "floor_id": f.floor})
    if f.role:
        q = q.filter(**{prefix + "creation_operation__actor_role": f.role})
    if f.q:
        q = q.filter(
            Q(**{prefix + "reference__icontains": f.q})
            | Q(**{prefix + "floor_name__icontains": f.q})
        )
    return q


def stock_query(f):
    q = StockBalance.objects.select_related("product", "product__stock_hold").order_by(
        "product__name", "product_id"
    )
    if f.product:
        q = q.filter(product_id=f.product)
    if f.q:
        q = q.filter(Q(product__name__icontains=f.q) | Q(product__code__icontains=f.q))
    if f.state == "shortage":
        q = q.filter(physical=F("reserved"), physical__isnull=False, product__status="ACTIVE")
    elif f.state:
        q = q.filter(product__status=f.state)
    return q


def stock_rows(query):
    for s in stream(query):
        p = s.product
        yield {
            "cells": [
                p.code,
                p.name,
                p.unit,
                p.get_status_display(),
                s.physical,
                s.reserved,
                s.available,
                p.minimum,
                p.target,
                "Bloqué" if hasattr(p, "stock_hold") else "—",
            ],
            "url": f"/catalogue/{p.pk}",
            "product_id": str(p.pk),
        }


def stock_page(actor, f):
    check_role(actor, {"ADMIN"})
    validate_filters(actor, "stocks", f)
    query = stock_query(f)
    known = Q(physical__isnull=False)
    counts = query.aggregate(
        total=Count("pk"),
        known=Count("pk", filter=known),
        shortage=Count("pk", filter=known & Q(physical=F("reserved"))),
        below=Count("pk", filter=known & Q(physical__lte=F("reserved") + F("product__minimum"))),
        above=Count("pk", filter=known & Q(physical__gt=F("reserved") + F("product__minimum"))),
    )
    counts["unknown"] = counts["total"] - counts["known"]
    start = (f.page - 1) * 50
    return stock_rows(query[start : start + 50]), counts["total"], counts


def rows(actor, name, f, personal=False):
    dataset(name)
    if not personal:
        check_role(actor, {"ADMIN"})
    elif name != "history":
        raise BusinessError(
            "PERMISSION_DENIED", gettext("Historique personnel uniquement."), status=403
        )
    validate_filters(actor, name, f, personal)
    if name == "stocks":
        yield from stock_rows(stock_query(f))
    elif name == "movements":
        q = operation_filter(
            period(
                StockEntry.objects.select_related("operation__actor"), "operation__confirmed_at", f
            ),
            "operation__",
            f,
        )
        if f.product:
            q = q.filter(product_id=f.product)
        if f.state:
            q = q.filter(kind=f.state)
        if f.q:
            q = q.filter(product_name__icontains=f.q)
        # Les ajustements de réserve n'ont aucun unité ; filtre unité exclut ceux-ci.
        if f.floor:
            q = q.filter(
                operation_id__in=RequestEvent.objects.filter(request__floor_id=f.floor).values(
                    "operation_id"
                )
            )
        for e in stream(q.order_by("-operation__confirmed_at", "-id")):
            yield {
                "cells": [
                    e.operation.confirmed_at,
                    e.product_name,
                    e.product_code,
                    e.unit,
                    e.get_kind_display(),
                    e.physical_before,
                    e.physical_after,
                    e.delta_p,
                    e.reserved_before,
                    e.reserved_after,
                    e.delta_r,
                    e.operation.actor.display_name,
                    ROLES[e.operation.actor_role],
                ],
                "url": "/stocks/mouvements",
                "product_id": str(e.product_id),
            }
    elif name == "requests":
        q = RequestLine.objects.select_related("request__creation_operation", "reservation")
        q = request_filters(
            period(q, "request__creation_operation__confirmed_at", f), "request__", f
        )
        if f.product:
            q = q.filter(product_id=f.product)
        if f.actor:
            q = q.filter(request__assistant_id=f.actor)
        if f.state:
            q = q.filter(request__status=f.state)
        for x in stream(q.order_by("-request__creation_operation__confirmed_at", "id")):
            r = x.request
            yield {
                "cells": [
                    r.creation_operation.confirmed_at,
                    r.reference,
                    r.assistant_name,
                    ROLES[r.creation_operation.actor_role],
                    r.floor_name,
                    x.product_name,
                    x.product_code,
                    x.unit,
                    x.quantity,
                    x.delivered,
                    x.closed,
                    x.reservation.remaining,
                    r.get_status_display(),
                ],
                "url": f"/demandes/{r.pk}",
                "product_id": str(x.product_id),
                "request_id": str(r.pk),
                "floor_id": str(r.floor_id),
                "floor_code": r.floor_code,
            }
    elif name == "deliveries":
        q = effective_deliveries(actor, f.start, f.end - timedelta(days=1)).select_related(
            "delivery__request__creation_operation"
        )
        q = request_filters(q, "delivery__request__", f)
        if f.product:
            q = q.filter(line__product_id=f.product)
        if f.actor:
            q = q.filter(delivery__operator_id=f.actor)
        if f.state:
            q = q.filter(delivery__request__status=f.state)
        for x in stream(q.order_by("delivery__operation__confirmed_at", "pk")):
            r = x.delivery.request
            line = x.line
            yield {
                "cells": [
                    x.delivery.operation.confirmed_at,
                    r.reference,
                    r.floor_name,
                    ROLES[r.creation_operation.actor_role],
                    line.product_name,
                    line.product_code,
                    line.unit,
                    x.handed,
                    x.effective_quantity,
                    x.delivery.operator_name,
                ],
                "url": f"/demandes/{r.pk}",
                "product_id": str(line.product_id),
            }
    elif name in {"purchases", "orderlines"}:
        orders = PurchaseOrder.objects.select_related("creation_operation").annotate(
            last_reminder=Subquery(
                ManualReminder.objects.filter(order_id=OuterRef("pk"))
                .order_by("-operation__confirmed_at")
                .values("operation__confirmed_at")[:1]
            )
        )
        if f.state != "incomplete":
            orders = orders.filter(purchased_on__gte=f.start, purchased_on__lt=f.end)
        if f.state == "incomplete":
            orders = orders.filter(
                pk__in=OrderLine.objects.filter(quantity__gt=F("received") + F("cancelled")).values(
                    "order_id"
                )
            )
        elif f.state:
            orders = orders.filter(status=f.state)
        if f.q:
            orders = orders.filter(
                Q(reference__icontains=f.q)
                | Q(supplier_name__icontains=f.q)
                | Q(external_reference__icontains=f.q)
            )
        if f.product:
            orders = orders.filter(
                pk__in=OrderLine.objects.filter(product_id=f.product).values("order_id")
            )
        orders = operation_filter(orders, "creation_operation__", f)
        if name == "purchases":
            for o in stream(orders.order_by("-purchased_on", "pk")):
                yield {
                    "cells": [
                        o.purchased_on,
                        o.reference,
                        o.supplier_name,
                        o.external_reference,
                        o.amount,
                        o.currency,
                        o.get_status_display(),
                        o.author_name,
                        o.last_reminder,
                    ],
                    "url": f"/achats/{o.pk}",
                }
        else:
            q = OrderLine.objects.filter(order__in=orders).select_related("order")
            if f.product:
                q = q.filter(product_id=f.product)
            for line in stream(q.order_by("-order__purchased_on", "pk")):
                o = line.order
                yield {
                    "cells": [
                        o.purchased_on,
                        o.reference,
                        o.supplier_name,
                        line.product_name,
                        line.product_code,
                        line.unit,
                        line.quantity,
                        line.received,
                        line.cancelled,
                        line.waiting,
                        o.get_status_display(),
                    ],
                    "url": f"/achats/{o.pk}",
                    "product_id": str(line.product_id),
                }
    elif name == "receipts":
        q = effective_receipts(actor, f.start, f.end - timedelta(days=1)).select_related(
            "receipt__event__order", "receipt__event__operation__actor"
        )
        for field in ["surplus", "nonconforming"]:
            changes = (
                ReceiptCorrectionLine.objects.filter(line_id=OuterRef("pk"))
                .values("line_id")
                .annotate(total=Sum(F(field + "_after") - F(field + "_before")))
                .values("total")
            )
            q = q.annotate(
                **{
                    "effective_" + field: F(field)
                    + Coalesce(Subquery(changes), Value(0), output_field=BigIntegerField())
                }
            )
        if f.product:
            q = q.filter(order_line__product_id=f.product)
        q = operation_filter(q, "receipt__event__operation__", f)
        if f.q:
            q = q.filter(receipt__event__order__reference__icontains=f.q)
        for x in stream(q.order_by("receipt__event__operation__confirmed_at", "pk")):
            o = x.receipt.event.order
            line = x.order_line
            yield {
                "cells": [
                    x.receipt.event.operation.confirmed_at,
                    x.receipt.reference,
                    o.reference,
                    o.supplier_name,
                    line.product_name,
                    line.product_code,
                    line.unit,
                    x.regular,
                    x.effective_quantity,
                    x.receipt.event.operation.actor.display_name,
                    x.effective_nonconforming,
                    x.effective_surplus,
                ],
                "url": f"/receptions/recus/{x.receipt_id}",
                "product_id": str(line.product_id),
            }
    elif name == "surplus":
        q = QuarantineDecision.objects.filter(kind="ACCEPT").select_related(
            "operation__actor", "item__receipt_line__order_line", "item__report_line", "item__case"
        )
        q = operation_filter(period(q, "operation__confirmed_at", f), "operation__", f)
        if f.product:
            q = q.filter(
                Q(item__receipt_line__order_line__product_id=f.product)
                | Q(item__report_line__product_id=f.product)
            )
        for x in stream(q.order_by("-operation__confirmed_at", "pk"), post_filter=bool(f.q)):
            line = x.item.receipt_line.order_line if x.item.receipt_line_id else x.item.report_line
            if f.q and f.q.casefold() not in line.product_name.casefold():
                continue
            yield {
                "cells": [
                    x.operation.confirmed_at,
                    x.item.case.reference,
                    line.product_name,
                    line.unit,
                    x.quantity,
                    x.operation.actor.display_name,
                ],
                "url": f"/receptions/dossiers/{x.item.case_id}",
                "product_id": str(line.product_id),
            }
    elif name == "cases":
        q = (
            reading.query(actor)
            .select_related("arrival_report")
            .prefetch_related("stock_case__products")
        )
        if f.state != "pending":
            q = period(q, "origin_operation__confirmed_at", f)
        q = operation_filter(q, "origin_operation__", f)
        if f.category:
            q = q.filter(kind__in=[k for k, v in classification.BY_KIND.items() if v == f.category])
        if f.state == "pending":
            q = q.exclude(status="RESOLVED")
        elif f.state:
            q = q.filter(status=f.state)
        if f.product:
            q = q.filter(
                Q(stock_case__products__product_id=f.product)
                | Q(delivery__lines__line__product_id=f.product)
                | Q(receipt__lines__order_line__product_id=f.product)
                | Q(quarantine_items__report_line__product_id=f.product)
                | Q(arrival_report__lines__product_id=f.product)
            ).distinct()
        if f.q:
            q = q.filter(
                Q(kind__icontains=f.q)
                | Q(origin_operation__actor__display_name__icontains=f.q)
                | Q(delivery__request__reference__icontains=f.q)
                | Q(receipt__reference__icontains=f.q)
                | Q(arrival_report__reference__icontains=f.q)
            )
        for c in stream(q):
            supplier = c.kind in {
                "SURPLUS",
                "RECEIPT_ERROR",
                "NONCONFORMITY",
                "UNKNOWN_ORDER",
                "LATE_CANCELLED_DELIVERY",
            }
            yield {
                "cells": [
                    c.origin_operation.confirmed_at,
                    case_title(c),
                    KINDS[c.kind],
                    classification.CATEGORIES[classification.primary_category(c.kind)],
                    STATES[c.status],
                    c.origin_operation.actor.display_name,
                ],
                "url": f"/receptions/dossiers/{c.pk}" if supplier else f"/dossiers/{c.pk}",
            }
    else:
        from .history import rows as history_rows

        yield from history_rows(actor, history_query(actor, f, personal))


def operation_label(kind):
    labels = {
        "requests.create": gettext_noop("Demande envoyée"),
        "requests.cancel": gettext_noop("Demande annulée"),
        "requests.deliver": gettext_noop("Remise confirmée"),
        "requests.claim": gettext_noop("Demande prise en charge"),
        "corrections.confirm": gettext_noop("Correction confirmée"),
        "reporting.export": gettext_noop("Export Excel généré"),
        "inventory.adjust": gettext_noop("Ajustement de réserve"),
        "inventory.initial": gettext_noop("Inventaire initial"),
        "orders.create": gettext_noop("Achat enregistré"),
    }
    labels.update(
        {
            "requests.refuse": gettext_noop("Demande refusée"),
            "requests.close_remainder": gettext_noop("Quantités restantes clôturées"),
            "requests.reopen": gettext_noop("Demande rouverte"),
            "requests.reassign": gettext_noop("Demande réattribuée"),
            "deliveries.report_error": gettext_noop("Erreur de remise signalée"),
            "cases.review": gettext_noop("Vérification commencée"),
            "cases.open": gettext_noop("Dossier ouvert"),
            "cases.resolve": gettext_noop("Manque vérifié et dossier résolu"),
            "stock_reconciliations.confirm": gettext_noop("Stock et réservations régularisés"),
            "orders.cancel_remainder": gettext_noop("Unités fournisseur restantes annulées"),
            "recommendations.defer": gettext_noop("Besoin mis en attente"),
            "recommendations.reactivate": gettext_noop("Besoin réactivé"),
            "receipts.create": gettext_noop("Réception confirmée"),
            "orders.remind": gettext_noop("Rappel manuel enregistré"),
            "receiving.unknown": gettext_noop("Commande introuvable signalée"),
            "receiving.late": gettext_noop("Livraison tardive signalée"),
            "receiving.extra": gettext_noop("Arrivée supplémentaire signalée"),
            "receipts.report_error": gettext_noop("Erreur de réception signalée"),
            "quarantine.accept_surplus": gettext_noop("Surplus accepté"),
            "quarantine.refuse": gettext_noop("Marchandises isolées refusées"),
            "receiving.case_resolve": gettext_noop("Traitement externe vérifié"),
            "cases.link_order": gettext_noop("Colis rattaché à une commande"),
            "orders.reinstate_remainder": gettext_noop("Reliquat fournisseur rétabli"),
            "accounts.create": gettext_noop("Compte créé"),
            "accounts.update": gettext_noop("Compte modifié"),
            "accounts.reset": gettext_noop("Accès réinitialisé"),
            "accounts.revoke_session": gettext_noop("Session révoquée"),
            "catalog.product.create": gettext_noop("Produit créé"),
            "catalog.product.update": gettext_noop("Fiche produit modifiée"),
            "catalog.product.activate": gettext_noop("Produit activé"),
            "catalog.product.archive": gettext_noop("Produit archivé"),
            "catalog.product.permissions": gettext_noop("Autorisations et plafonds modifiés"),
            "catalog.product.suppliers": gettext_noop("Fournisseurs du produit modifiés"),
            "catalog.product.photo": gettext_noop("Photographie remplacée"),
            "catalog.floor.create": gettext_noop("Unité créé"),
            "catalog.floor.update": gettext_noop("Unité modifié"),
            "catalog.category.create": gettext_noop("Catégorie créée"),
            "catalog.category.update": gettext_noop("Catégorie modifiée"),
            "catalog.supplier.create": gettext_noop("Fournisseur créé"),
            "catalog.supplier.update": gettext_noop("Fournisseur modifié"),
        }
    )
    return gettext(labels.get(kind, gettext_noop("Action administrative historisée")))


SUPPORTED = {
    "stocks": {"product", "q", "state"},
    "movements": {"product", "q", "state", "actor", "role", "action", "floor"},
    "requests": {"product", "q", "state", "actor", "role", "floor"},
    "deliveries": {"product", "q", "state", "actor", "role", "floor"},
    "purchases": {"product", "q", "state", "actor", "role", "action"},
    "orderlines": {"product", "q", "state", "actor", "role", "action"},
    "receipts": {"product", "q", "actor", "role", "action"},
    "surplus": {"product", "q", "actor", "role", "action"},
    "cases": {"product", "q", "actor", "role", "action", "state", "category"},
    "history": {"product", "q", "actor", "role", "action", "floor"},
}


def filter_fields(actor, name, f):
    from apps.accounts.models import User
    from apps.catalog.models import Floor, Product
    from apps.requests.models import InternalRequest

    result = []
    choices = {
        "product": (
            gettext_noop("Produit"),
            Product.objects.order_by("name").values_list("pk", "name"),
        ),
        "floor": (
            gettext_noop("Unité d’origine"),
            Floor.objects.order_by("name").values_list("pk", "name"),
        ),
        "actor": (
            gettext_noop("Auteur"),
            User.objects.order_by("display_name").values_list("pk", "display_name"),
        ),
        "role": (
            gettext_noop("Profil du demandeur à la création")
            if name in {"requests", "deliveries"}
            else gettext_noop("Rôle au moment de l’action"),
            ROLES.items(),
        ),
        "category": (gettext_noop("Catégorie principale"), classification.CATEGORIES.items()),
    }
    if name == "stocks":
        states = [
            ("shortage", gettext_noop("Rupture")),
            ("ACTIVE", gettext_noop("Actif")),
            ("NOT_INVENTORIED", gettext_noop("Non inventorié")),
            ("COUNTED_PENDING_ACTIVATION", gettext_noop("À activer")),
            ("ARCHIVED", gettext_noop("Archivé")),
        ]
    elif name == "movements":
        states = StockEntry.Kind.choices
    elif name in {"requests", "deliveries"}:
        states = InternalRequest.Status.choices
    elif name in {"purchases", "orderlines"}:
        states = [
            ("incomplete", gettext_noop("Commandes incomplètes")),
            *PurchaseOrder.Status.choices,
        ]
    elif name == "cases":
        states = [("pending", "À traiter"), *STATES.items()]
    else:
        states = []
    choices["action"] = (
        (
            gettext_noop("Type d’action"),
            [
                (key, operation_label(key))
                for key in Operation.objects.order_by("command_type")
                .values_list("command_type", flat=True)
                .distinct()[: limit("MAX_CHOICES", 10000) + 1]
            ],
        )
        if "action" in SUPPORTED[name] and actor.role == "ADMIN"
        else (gettext_noop("Type d’action"), [])
    )
    if states:
        choices["state"] = (gettext_noop("État"), states)
    for key, (label, options) in choices.items():
        if key in SUPPORTED[name]:
            # Historique personnel : pas de liste des autres comptes/unités/produits.
            if actor.role != "ADMIN" and key not in {"role", "state"}:
                continue
            maximum_choices = limit("MAX_CHOICES", 10000)
            bounded = (
                list(options[: maximum_choices + 1])
                if isinstance(options, QuerySet)
                else list(options)
            )
            if len(bounded) > maximum_choices:
                raise refused(
                    gettext_noop(
                        "La liste des choix dépasse la limite technique. Réduisez les filtres."
                    )
                )
            result.append(
                {
                    "name": key,
                    "label": gettext(label),
                    "choices": [
                        (
                            str(k),
                            gettext(v) if key in {"role", "category", "action", "state"} else v,
                        )
                        for k, v in bounded
                    ],
                    "selected": getattr(f, key),
                }
            )
    return result


def validate_filters(actor, name, f, personal=False):
    dataset(name)
    for key in {
        "product",
        "floor",
        "actor",
        "role",
        "action",
        "q",
        "state",
        "category",
    } - SUPPORTED[name]:
        if getattr(f, key):
            raise BusinessError(
                "INVALID_INPUT", gettext("Ce filtre ne s’applique pas à cette analyse."), status=400
            )
    if personal and any([f.actor, f.product, f.floor, f.action]):
        raise BusinessError(
            "INVALID_INPUT",
            gettext("Ce filtre n’est pas disponible dans votre historique personnel."),
            status=400,
        )
    fields = filter_fields(actor, name, f)
    if personal:
        fields = [
            field
            for field in fields
            if field["name"] not in {"actor", "floor", "product", "action"}
        ]
    for field in fields:
        if field["selected"] and field["selected"] not in {k for k, v in field["choices"]}:
            raise BusinessError("INVALID_INPUT", gettext("Choix de filtre invalide."), status=400)
    return fields


def case_title(case):
    if case.delivery_id:
        return case.delivery.request.reference
    if case.receipt_id:
        return case.receipt.reference
    if case.stock_case_id:
        return gettext("Écart de stock — %(items)s") % {
            "items": ", ".join(product.product_name for product in case.stock_case.products.all())
        }
    if hasattr(case, "arrival_report"):
        return case.arrival_report.reference
    return KINDS[case.kind]
