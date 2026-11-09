"""Adaptateurs : capture par défaut, garde explicite avant tout transport extérieur."""

import json
from datetime import date
from smtplib import SMTPRecipientsRefused, SMTPResponseException

import requests
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.mail import EmailMessage
from django.core.validators import validate_email
from django.db import transaction
from django.utils import timezone, translation
from django.utils.translation import gettext

from apps.accounts.models import UserSession
from apps.accounts.sessions import checked_session
from apps.core.errors import BusinessError
from apps.core.policies import ActorContext, lock_actor

from .models import PushSubscription, WorkItem
from .worker import DeliveryFailure, result

APPLE_WEB_PUSH_403_REASONS = {
    "VapidPkHashMismatch": "PUSH_HTTP_403_VAPID_KEY_MISMATCH",
    "BadJwtToken": "PUSH_HTTP_403_BAD_JWT",
    "BadAuthorizationHeader": "PUSH_HTTP_403_BAD_AUTH_HEADER",
    "BadVapidPublicKey": "PUSH_HTTP_403_BAD_VAPID_KEY",
}


def _push_failure_code(exc):
    """Retourne un code fermé, sans stocker le corps ou le texte de l'exception."""
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if status == 403:
        try:
            payload = response.json()
        except (AttributeError, TypeError, ValueError):
            payload = None
        reason = payload.get("reason") if isinstance(payload, dict) else None
        if not isinstance(reason, str) or not reason or len(reason) > 80:
            return "PUSH_HTTP_403_REASON_UNAVAILABLE"
        return APPLE_WEB_PUSH_403_REASONS.get(reason, "PUSH_HTTP_403_REASON_UNRECOGNIZED")
    if status in {404, 410}:
        return f"SUBSCRIPTION_GONE_{status}"
    if isinstance(status, int) and 100 <= status <= 599:
        return f"PUSH_HTTP_{status}"
    return "PUSH_TEMPORARY"


def purchase_message(work):
    order = work.outbox.event.payload["order"]
    lines = [
        gettext("Order: %(reference)s") % {"reference": order["reference"]},
        gettext("Supplier: %(supplier)s") % {"supplier": order["supplier_name"]},
        gettext("Purchase date: %(date)s")
        % {"date": date.fromisoformat(order["purchased_on"]).isoformat()},
        "",
        gettext("Purchased products:"),
    ]
    lines += [
        gettext("• %(name)s: %(quantity)s %(unit)s")
        % {"name": row["name"], "quantity": row["quantity"], "unit": row["unit"]}
        for row in order["lines"]
    ]
    if order.get("external_reference"):
        lines.append(
            gettext("Supplier order number: %(reference)s")
            % {"reference": order["external_reference"]}
        )
    if order.get("amount") is not None:
        lines.append(
            gettext("Amount: %(amount)s %(currency)s")
            % {"amount": order["amount"], "currency": order["currency"]}
        )
    lines += [
        "",
        gettext("This message records an order declared as placed. It does not place an order."),
        gettext("Local demonstration only — all data is fictitious."),
    ]
    return gettext("Purchase recorded — %(reference)s") % {
        "reference": order["reference"]
    }, "\n".join(lines)


def send_email(work, *, allow_external):
    address = work.email_to
    if address != settings.NOTIFICATION_EMAIL_RECIPIENTS.get(work.destination, ""):
        return "RECIPIENT_CONFIGURATION_CHANGED"
    if not address:
        return "RECIPIENT_NOT_CONFIGURED"
    try:
        validate_email(address)
    except ValidationError as exc:
        raise DeliveryFailure("INVALID_EMAIL_CONFIGURATION", permanent=True) from exc
    subject, body = purchase_message(work)
    identifier = f"<reserve-{work.pk}@prototype.invalid>"
    if work.mode == "CAPTURE":
        from .email_backend import CaptureBackend

        mail = EmailMessage(
            subject,
            body,
            settings.NOTIFICATION_EMAIL_FROM,
            [address],
            connection=CaptureBackend(),
            headers={"Message-ID": identifier, "X-Reserve-Work": str(work.pk)},
        )
        mail.send(fail_silently=False)
        return None
    if not (
        allow_external
        and settings.NOTIFICATION_EXTERNAL_ALLOWED
        and settings.NOTIFICATION_EMAIL_MODE == "SMTP"
    ):
        return "EXTERNAL_DISABLED"
    from django.core.mail.backends.smtp import EmailBackend

    backend = EmailBackend(
        host=settings.NOTIFICATION_SMTP_HOST,
        port=settings.NOTIFICATION_SMTP_PORT,
        username=settings.NOTIFICATION_SMTP_USER,
        password=settings.NOTIFICATION_SMTP_PASSWORD,
        use_tls=True,
        timeout=8,
    )
    if not settings.NOTIFICATION_SMTP_HOST:
        return "SMTP_NOT_CONFIGURED"
    mail = EmailMessage(
        subject,
        body,
        settings.NOTIFICATION_EMAIL_FROM,
        [address],
        connection=backend,
        headers={"Message-ID": identifier},
    )
    try:
        if mail.send(fail_silently=False) != 1:
            raise DeliveryFailure("EMAIL_NOT_ACCEPTED")
    except SMTPRecipientsRefused as exc:
        permanent = bool(exc.recipients) and all(
            500 <= code < 600 for code, _ in exc.recipients.values()
        )
        raise DeliveryFailure("SMTP_RECIPIENT_REJECTED", permanent=permanent) from exc
    except SMTPResponseException as exc:
        raise DeliveryFailure("SMTP_REJECTED", permanent=500 <= exc.smtp_code < 600) from exc
    return None


class SafePushSession(requests.Session):
    """Pas de proxy ambiant, redirection ou fuite de VAPID vers un hôte choisi par le client."""

    def __init__(self):
        super().__init__()
        self.trust_env = False

    def request(self, method, url, **kwargs):
        from .push import valid_endpoint

        valid_endpoint(url)
        kwargs["allow_redirects"] = False
        kwargs["timeout"] = 8
        kwargs["verify"] = True
        return super().request(method, url, **kwargs)


def push_transport(subscription, notification):
    from pywebpush import WebPushException, webpush

    from .push import valid_endpoint

    valid_endpoint(subscription.endpoint)
    with SafePushSession() as session:
        try:
            response = webpush(
                subscription_info={
                    "endpoint": subscription.endpoint,
                    "keys": {"p256dh": subscription.p256dh, "auth": subscription.auth},
                },
                data=json.dumps(
                    localized_push_payload(notification, subscription.language),
                    ensure_ascii=False,
                ),
                vapid_private_key=settings.WEBPUSH_PRIVATE_KEY_FILE,
                vapid_claims={"sub": settings.WEBPUSH_CONTACT},
                ttl=300,
                timeout=8,
                requests_session=session,
            )
        except WebPushException as exc:
            code = _push_failure_code(exc)
            if code.startswith("SUBSCRIPTION_GONE_"):
                return code
            status = getattr(getattr(exc, "response", None), "status_code", None)
            raise DeliveryFailure(
                code,
                permanent=isinstance(status, int) and status < 500 and status != 429,
            ) from exc
        if not 200 <= response.status_code < 300:
            raise DeliveryFailure("PUSH_REDIRECT_REFUSED", permanent=True)
    return None


def localized_push_payload(notification, language):
    with translation.override(language):
        return {
            "title": gettext("Réserve centrale"),
            "body": gettext("Une information vous attend dans l’application."),
            "notification_id": str(notification.pk),
        }


def send_push(attempt, *, allow_external):
    from .push import enabled
    from .routing import destination, scope

    # Même ordre que les services de compte : compte partagé puis session, abonnement,
    # job. La désactivation attend la fin de l'I/O déjà en cours, jamais l'inverse.
    sub = PushSubscription.objects.select_related("user", "session").get(
        pk=attempt.work.subscription_id
    )
    with transaction.atomic():
        try:
            actor = lock_actor(ActorContext(sub.user_id, sub.session.auth_version, sub.session_id))
        except BusinessError:
            actor = None
        meta = UserSession.objects.select_for_update().get(pk=sub.session_id)
        sub = PushSubscription.objects.select_for_update().get(pk=sub.pk)
        work = (
            WorkItem.objects.select_for_update(of=("self",))
            .select_related("notification", "outbox__event__operation__intent")
            .get(pk=attempt.work_id)
        )
        if work.state != "CLAIMED" or work.lease_token != attempt.token:
            return False
        try:
            if actor:
                checked_session(actor, meta.pk)
        except BusinessError:
            actor = None
        if (
            actor is None
            or actor.role != "REPLENISHER"
            or sub.revoked_at
            or work.notification.scope != scope(actor)
            or work.notification.recipient_id != actor.pk
        ):
            if sub.revoked_at is None:
                sub.revoked_at = timezone.now()
                sub.save(update_fields=["revoked_at"])
            return result(work, attempt, "SKIPPED", "SESSION_OR_ACCOUNT_REVOKED")
        if work.outbox.event.operation.intent.generation_id != settings.OPERATION_GENERATION:
            return result(work, attempt, "SKIPPED", "STALE_GENERATION")
        if destination(work.notification, actor)[0] is None:
            return result(work, attempt, "SKIPPED", "TARGET_FORBIDDEN")
        if settings.PUSH_SINGLE_TARGET_TEST_MODE:
            active_subscriptions = list(
                PushSubscription.objects.filter(revoked_at__isnull=True).values_list(
                    "pk", "user_id"
                )
            )
            if (
                len(active_subscriptions) != 1
                or str(sub.pk) != settings.PUSH_TEST_ALLOWED_SUBSCRIPTION_ID
                or str(sub.user_id) != settings.PUSH_TEST_ALLOWED_USER_ID
                or tuple(map(str, active_subscriptions[0]))
                != (
                    settings.PUSH_TEST_ALLOWED_SUBSCRIPTION_ID,
                    settings.PUSH_TEST_ALLOWED_USER_ID,
                )
                or not settings.PUSH_TEST_ALLOWED_SUBSCRIPTION_ID
                or not settings.PUSH_TEST_ALLOWED_USER_ID
            ):
                return result(work, attempt, "SKIPPED", "PUSH_TEST_TARGET_BLOCKED")
        if not (enabled() and allow_external and settings.NOTIFICATION_EXTERNAL_ALLOWED):
            return result(work, attempt, "SKIPPED", "EXTERNAL_DISABLED")
        code = push_transport(sub, work.notification)
        if code == "SUBSCRIPTION_GONE" or (
            isinstance(code, str) and code.startswith("SUBSCRIPTION_GONE_")
        ):
            sub.revoked_at = timezone.now()
            sub.save(update_fields=["revoked_at"])
            return result(work, attempt, "SKIPPED", code)
        return result(work, attempt, "DONE", "SENT")
