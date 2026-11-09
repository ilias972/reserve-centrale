"""Sessions serveur. Les durées configurées demeurent provisoires (A10)."""

import hashlib
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.signals import user_logged_in
from django.db import transaction
from django.dispatch import receiver
from django.utils import timezone
from django.utils.translation import gettext

from apps.core.errors import BusinessError

from .models import User, UserSession


def digest_key(key):
    return hashlib.sha256(key.encode()).hexdigest()


def durations(user):
    if user.role == User.Role.ADMIN:
        return settings.ADMIN_SESSION_IDLE_SECONDS, settings.ADMIN_SESSION_ABSOLUTE_SECONDS
    return settings.SESSION_IDLE_SECONDS, settings.SESSION_ABSOLUTE_SECONDS


def checked_session(user, identifier, *, key=None):
    now = timezone.now()
    meta = UserSession.objects.filter(pk=identifier, user=user).first()
    if meta and meta.generation_id != settings.OPERATION_GENERATION:
        raise BusinessError(
            "RECOVERY_RECONCILIATION_REQUIRED",
            gettext("Une reprise a invalidé cet accès. Reconnectez-vous après vérification."),
        )
    if (
        meta is None
        or meta.revoked_at is not None
        or meta.auth_version != user.auth_version
        or meta.generation_id != settings.OPERATION_GENERATION
        or meta.idle_expires_at <= now
        or meta.absolute_expires_at <= now
        or (key is not None and meta.key_digest != digest_key(key))
        or not user.is_active
    ):
        raise BusinessError(
            "SESSION_REVOKED",
            gettext("Votre session a expiré ou a été révoquée. Reconnectez-vous."),
            status=401,
        )
    return meta


def revoke_all(user):
    UserSession.objects.filter(user=user, revoked_at__isnull=True).update(revoked_at=timezone.now())


@receiver(user_logged_in, dispatch_uid="reserve.register_session")
def register_session(sender, request, user, **kwargs):
    # Le signal couvre également force_login des tests M0. Les vraies connexions
    # passent par services.sign_in, qui conserve le verrou jusqu'à son audit.
    with transaction.atomic():
        current = User.objects.select_for_update().get(pk=user.pk)
        if not current.is_active or current.auth_version != user.auth_version:
            raise BusinessError("SESSION_REVOKED", gettext("Accès révoqué."), status=401)
        now = timezone.now()
        idle, absolute = durations(current)
        persistent = bool(getattr(request, "persistent_login", True))
        request.session.save()
        meta = UserSession.objects.create(
            user=current,
            key_digest=digest_key(request.session.session_key),
            auth_version=current.auth_version,
            generation_id=settings.OPERATION_GENERATION,
            created_at=now,
            authenticated_at=now,
            last_seen_at=now,
            idle_expires_at=now + timedelta(seconds=min(idle, absolute)),
            absolute_expires_at=now + timedelta(seconds=absolute),
            persistent=persistent,
        )
        request.session["reserve_session_id"] = str(meta.pk)
        request.user_session_id = meta.pk
        request.session.set_expiry(min(idle, absolute) if persistent else 0)
        request.session.save()
