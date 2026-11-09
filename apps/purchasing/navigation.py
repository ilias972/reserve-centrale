"""Compteur de besoins à traiter, sans notification externe ni modification A11."""

from apps.core.policies import check_role

from . import recommendations
from .models import OrderLine, PurchaseOrder


def pending_count(actor):
    check_role(actor, {"ADMIN"})
    return recommendations.query().filter(_rec_state="ACTIVE", _need__gt=0).count()


def incomplete_count(actor):
    """Une commande avec W>0, quel que soit le nombre de lignes attendues."""
    from django.db.models import F

    check_role(actor, {"ADMIN"})
    waiting = OrderLine.objects.filter(quantity__gt=F("received") + F("cancelled"))
    return PurchaseOrder.objects.filter(pk__in=waiting.values("order_id")).count()


def navigation_counts(request):
    actor = request.user
    if not actor.is_authenticated or actor.role != "ADMIN" or actor.must_change_password:
        return {}
    return {"pending_purchase_need_count": pending_count(actor)}
