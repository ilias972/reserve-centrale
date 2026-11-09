"""Même verrou de demande pour prise en charge, annulation et remise."""

from django.utils.translation import gettext

from apps.core.errors import BusinessError

from .models import InternalRequest


def operational_scope(actor, identifier):
    if not InternalRequest.objects.filter(pk=identifier).exists():
        raise BusinessError("NOT_FOUND", gettext("Demande introuvable."), status=404)


def lock_request(actor, identifier):
    return InternalRequest.objects.select_for_update().get(pk=identifier)
