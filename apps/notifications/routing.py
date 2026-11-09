"""Projection de faits connus uniquement ; aucun appel à un service de mutation métier."""

from django.db.models import Q
from django.urls import reverse
from django.utils.translation import gettext_noop

from apps.accounts.models import REQUESTER_ROLES, User
from apps.cases.models import Case
from apps.requests.models import InternalRequest

from .models import Notification

REQUEST_LABELS = {
    "request.created": gettext_noop("Nouvelle demande à préparer"),
    "request.cancelled": gettext_noop("Demande annulée"),
    "request.claimed": gettext_noop("Demande prise en charge"),
    "request.delivered": gettext_noop("Remise enregistrée"),
    "request.refused": gettext_noop("Demande refusée"),
    "request.corrected": gettext_noop("Quantités remises corrigées"),
    "request.remainder_closed": gettext_noop("Reste de la demande clôturé"),
    "request.reopened": gettext_noop("Demande rouverte"),
    "request.reassigned": gettext_noop("Attribution de la demande modifiée"),
    "request.reconciled": gettext_noop("Quantités réservées modifiées après vérification"),
}
CASE_LABELS = {
    "STOCK_CONFLICT": gettext_noop("Stock à vérifier"),
    "DELIVERY_ERROR": gettext_noop("Quantités remises à vérifier"),
    "SHORTAGE": gettext_noop("Manque signalé lors d'une remise"),
    "SURPLUS": gettext_noop("Surplus fournisseur à examiner"),
    "NONCONFORMITY": gettext_noop("Produits non conformes à examiner"),
    "UNKNOWN_ORDER": gettext_noop("Commande fournisseur introuvable"),
    "LATE_CANCELLED_DELIVERY": gettext_noop("Arrivée d’unités annulées à vérifier"),
    "RECEIPT_ERROR": gettext_noop("Réception à vérifier"),
}
SUPPLIER_CASES = {
    "SURPLUS",
    "NONCONFORMITY",
    "UNKNOWN_ORDER",
    "LATE_CANCELLED_DELIVERY",
    "RECEIPT_ERROR",
}


def active_users(**filters):
    return User.objects.filter(is_active=True, **filters)


def project(event):
    """Renvoie des destinataires calculés à partir des relations, pas de rôles arbitraires JSON."""
    kind = event.event_type
    if kind == "recommendation.stock_reactivated":
        return [
            (
                user,
                gettext_noop("Produit indisponible")
                if event.payload["available_after"] == 0
                else gettext_noop("Besoin d’achat réactivé"),
                gettext_noop(
                    "Un besoin d’achat a changé. Consultez les quantités actuelles dans son dossier."
                ),
                "PRODUCT",
                event.aggregate_id,
                "need:" + str(event.aggregate_id),
            )
            for user in active_users(role="ADMIN")
        ]
    if kind in REQUEST_LABELS:
        req = InternalRequest.objects.filter(pk=event.aggregate_id).first()
        if req is None:
            return []
        if kind == "request.created":
            users = active_users(role="REPLENISHER")
        elif kind == "request.cancelled":
            # Attribution au moment de l'événement, et non une nouvelle attribution éventuelle.
            named = event.payload.get("recipient_id")
            users = active_users(role="REPLENISHER")
            if named:
                users = users.filter(pk=named)
        else:
            known = {req.assistant_id, req.assigned_to_id, event.operation.actor_id}
            from apps.requests.models import RequestDecision

            for previous, following in RequestDecision.objects.filter(
                event__operation=event.operation, event__request=req
            ).values_list("previous_operator_id", "next_operator_id"):
                known.update((previous, following))
            if kind in {
                "request.corrected",
                "request.reconciled",
                "request.remainder_closed",
                "request.reopened",
                "request.reassigned",
            }:
                related = known | set(req.deliveries.values_list("operator_id", flat=True))
                declared = set(event.payload.get("recipient_ids", []))
                known = {
                    identifier
                    for identifier in related
                    if identifier is not None and str(identifier) in declared
                }
            if kind in {"request.delivered", "request.refused"}:
                known = {req.assistant_id}
            users = active_users(pk__in=known).filter(
                Q(pk=req.assistant_id, role__in=REQUESTER_ROLES) | Q(role="REPLENISHER")
            )
        return [
            (
                u,
                REQUEST_LABELS[kind],
                gettext_noop(
                    "Une demande a changé. Consultez son suivi pour voir son état et ses quantités."
                ),
                "REQUEST",
                req.pk,
                "request:" + str(req.pk),
            )
            for u in users
        ]
    case = None
    if kind in {"case.opened", "case.updated"}:
        case = Case.objects.filter(pk=event.aggregate_id).first()
    elif kind == "stock.conflict.opened":
        case = Case.objects.filter(stock_case_id=event.aggregate_id).first()
    elif kind in {"delivery.error.reported", "delivery.shortage.reported"}:
        case = Case.objects.filter(delivery_issue_id=event.aggregate_id).first()
    if case:
        if kind == "case.updated":
            users = active_users(pk=case.origin_operation.actor_id, role="REPLENISHER")
            title = gettext_noop("Votre signalement a été mis à jour")
        else:
            users = active_users(role="ADMIN")
            title = CASE_LABELS.get(case.kind, gettext_noop("Signalement à examiner"))
        message = gettext_noop(
            "Un dossier nécessite votre attention. Consultez son état et les actions possibles."
        )
        return [(u, title, message, "CASE", case.pk, "case:" + str(case.pk)) for u in users]
    if kind in {"purchase.manual_reminder", "purchase.received"}:
        from apps.purchasing.models import PurchaseOrder
        from apps.receiving.models import Receipt

        order = PurchaseOrder.objects.filter(pk=event.aggregate_id).first()
        if order is None:
            return []
        if kind == "purchase.received":
            receipt = (
                Receipt.objects.filter(pk=event.payload.get("receipt_id"))
                .select_related("event")
                .first()
            )
            # Snapshot de cette arrivée : pas l'état actuel susceptible d'avoir évolué.
            after = event.operation.auditevent.after
            if receipt is None or after.get("order_status", order.status) != "PARTIAL":
                return []
        title = (
            gettext_noop("Rappel concernant une commande")
            if kind.endswith("reminder")
            else gettext_noop("Réception partielle à suivre")
        )
        return [
            (
                u,
                title,
                gettext_noop(
                    "Une commande fournisseur nécessite un suivi. Consultez les quantités encore attendues."
                ),
                "ORDER",
                order.pk,
                "order:" + str(order.pk),
            )
            for u in active_users(role="ADMIN")
        ]
    return []


def scope(user):
    return "REQUESTER" if user.is_requester else user.role


def visible(user):
    return (
        Notification.objects.filter(recipient=user, scope=scope(user))
        .select_related("event__operation")
        .order_by("-created_at", "-id")
    )


def destination(notification, user):
    """Contrôle actuel de l'objet avant de proposer un lien ; jamais d'URL provenant du payload."""
    if notification.target_kind == "REQUEST":
        from apps.requests.selectors import request_query

        if request_query(user).filter(pk=notification.target_id).exists():
            return reverse("request-detail", args=[notification.target_id]), gettext_noop(
                "Consulter la demande"
            )
        if user.role == "REPLENISHER":
            return reverse("requests"), gettext_noop("Voir les demandes à préparer")
    if notification.target_kind == "CASE" and user.role in {"ADMIN", "REPLENISHER"}:
        from apps.cases.reading import query

        case = query(user).filter(pk=notification.target_id).first()
        if case:
            name = "receiving-case" if case.kind in SUPPLIER_CASES else "case-detail"
            return reverse(name, args=[case.pk]), gettext_noop("Examiner le signalement")
    if notification.target_kind == "ORDER" and user.role == "ADMIN":
        from apps.purchasing.models import PurchaseOrder

        if PurchaseOrder.objects.filter(pk=notification.target_id).exists():
            return reverse("receiving-order", args=[notification.target_id]), gettext_noop(
                "Consulter la commande"
            )
    if notification.target_kind == "PRODUCT" and user.role == "ADMIN":
        from apps.catalog.models import Product

        if Product.objects.filter(pk=notification.target_id).exists():
            return reverse("purchase-need", args=[notification.target_id]), gettext_noop(
                "Consulter le besoin"
            )
    return None, gettext_noop("This item is no longer available with your current access.")
