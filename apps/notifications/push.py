"""Abonnements volontaires, liés à une session révocable ; endpoints non arbitraires."""

import base64
import hashlib
from urllib.parse import urlsplit

from cryptography.hazmat.primitives.asymmetric import ec
from django.conf import settings
from django.db import connection, transaction
from django.utils import timezone
from django.utils.translation import gettext

from apps.accounts.models import UserSession
from apps.accounts.sessions import checked_session
from apps.core.errors import BusinessError
from apps.core.policies import check_role, lock_actor

from .models import PushSubscription


def enabled():
    return bool(
        settings.NOTIFICATION_PUSH_ENABLED
        and settings.WEBPUSH_PUBLIC_KEY
        and settings.WEBPUSH_PRIVATE_KEY_FILE
        and settings.WEBPUSH_CONTACT
    )


def valid_endpoint(endpoint):
    try:
        url = urlsplit(endpoint)
        host = url.hostname or ""
        allowed = host in {
            "fcm.googleapis.com",
            "updates.push.services.mozilla.com",
            "web.push.apple.com",
        } or host.endswith(".push.apple.com")
        if settings.PUSH_SINGLE_TARGET_TEST_MODE:
            allowed = host == "web.push.apple.com" or host.endswith(".push.apple.com")
        if not (
            isinstance(endpoint, str)
            and len(endpoint) <= 2048
            and url.scheme == "https"
            and allowed
            and url.port in {None, 443}
            and not url.username
            and not url.password
            and not url.fragment
            and url.path not in {"", "/"}
        ):
            raise ValueError
    except (ValueError, TypeError, AttributeError) as exc:
        raise BusinessError(
            "INVALID_SUBSCRIPTION",
            gettext("Service de notifications non reconnu ou adresse invalide."),
            status=400,
        ) from exc
    return endpoint


def decode_key(value, length):
    try:
        if not isinstance(value, str) or len(value) > 100:
            raise ValueError
        decoded = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
        if len(decoded) != length:
            raise ValueError
        return decoded
    except (ValueError, TypeError) as exc:
        raise BusinessError(
            "INVALID_SUBSCRIPTION",
            gettext("Abonnement navigateur invalide. Réessayez depuis cet appareil."),
            status=400,
        ) from exc


@transaction.atomic
def subscribe(context, payload):
    actor = lock_actor(context)
    check_role(actor, {"REPLENISHER"})
    if not enabled():
        raise BusinessError(
            "PUSH_DISABLED",
            gettext("Les notifications de cet appareil ne sont pas encore configurées."),
            status=409,
        )
    if (
        set(payload) != {"endpoint", "keys", "language"}
        or not isinstance(payload["keys"], dict)
        or set(payload["keys"]) != {"p256dh", "auth"}
    ):
        raise BusinessError(
            "INVALID_SUBSCRIPTION", gettext("Abonnement navigateur invalide."), status=400
        )
    language = payload["language"]
    if language not in {code for code, _label in settings.LANGUAGES}:
        raise BusinessError(
            "INVALID_SUBSCRIPTION", gettext("Langue de notification invalide."), status=400
        )
    endpoint = valid_endpoint(payload["endpoint"])
    key = decode_key(payload["keys"]["p256dh"], 65)
    try:
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), key)
    except ValueError as exc:
        raise BusinessError(
            "INVALID_SUBSCRIPTION", gettext("Clef navigateur invalide."), status=400
        ) from exc
    decode_key(payload["keys"]["auth"], 16)
    meta = UserSession.objects.select_for_update().get(pk=context.session_id, user=actor)
    checked_session(actor, meta.pk)
    digest = hashlib.sha256(endpoint.encode()).hexdigest()
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", [int(digest[:15], 16)])
    current = (
        PushSubscription.objects.select_for_update()
        .filter(endpoint_digest=digest, revoked_at__isnull=True)
        .first()
    )
    if (
        current
        and current.session_id == meta.pk
        and current.p256dh == payload["keys"]["p256dh"]
        and current.auth == payload["keys"]["auth"]
    ):
        # Subscription facts are append-only apart from revocation. Its push language
        # remains the language explicitly selected when this subscription was created.
        return current
    if (
        settings.PUSH_SINGLE_TARGET_TEST_MODE
        and PushSubscription.objects.filter(revoked_at__isnull=True).exists()
    ):
        raise BusinessError(
            "PUSH_TEST_SUBSCRIPTION_LIMIT",
            gettext("Une seule inscription d'appareil est autorisée pour cet essai."),
            status=409,
        )
    if current:
        # Nouveau fait : ne pas rattacher l'ancien abonnement ni ses jobs au nouveau compte.
        current.revoked_at = timezone.now()
        current.save(update_fields=["revoked_at"])
    return PushSubscription.objects.create(
        user=actor,
        session=meta,
        endpoint=endpoint,
        endpoint_digest=digest,
        language=language,
        **payload["keys"],
    )


@transaction.atomic
def revoke(context, identifier):
    actor = lock_actor(context)
    checked_session(actor, context.session_id)
    return PushSubscription.objects.filter(
        pk=identifier, user=actor, session_id=context.session_id, revoked_at__isnull=True
    ).update(revoked_at=timezone.now())
