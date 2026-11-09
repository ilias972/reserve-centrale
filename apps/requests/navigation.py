"""Compteur de travail M5, sans notification externe ni modification métier."""

from django.db.models import Q

from apps.core.policies import check_role

from .models import InternalRequest


def pending_count(actor):
    check_role(actor, {"REPLENISHER"})
    return InternalRequest.objects.filter(
        Q(status="NEW", assigned_to__isnull=True)
        | Q(status__in=["PREPARING", "PARTIAL_OPEN"], assigned_to=actor)
    ).count()


def navigation_counts(request):
    actor = request.user
    if not actor.is_authenticated or actor.role != "REPLENISHER" or actor.must_change_password:
        return {}
    return {"pending_request_count": pending_count(actor)}
