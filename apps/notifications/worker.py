"""Worker PostgreSQL borné ; aucun service métier appelé lors d'un envoi."""

import logging
import uuid
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from . import dispatcher
from .models import Attempt, AttemptResult, WorkItem

logger = logging.getLogger("reserve")


def _provider_http_status(code):
    """Extrait un statut déjà encodé dans un code local fermé, jamais un texte externe."""
    if not isinstance(code, str):
        return None
    for prefix in ("PUSH_HTTP_", "SUBSCRIPTION_GONE_"):
        if code.startswith(prefix):
            value = code[len(prefix) :].split("_", 1)[0]
            if value.isdigit() and 100 <= int(value) <= 599:
                return int(value)
    return None


class DeliveryFailure(Exception):
    def __init__(self, code, *, permanent=False, delay=None):
        self.code, self.permanent, self.delay = code, permanent, delay


def result(work, attempt, state, code, *, delay=0):
    # À appeler sous verrou ; l'ancien worker ne peut acquitter le nouveau lease.
    if work.state != "CLAIMED" or work.lease_token != attempt.token:
        return False
    AttemptResult.objects.create(attempt=attempt, outcome=state, code=code)
    work.state, work.result_code = state, code
    work.due_at = timezone.now() + timedelta(seconds=delay)
    work.lease_until = work.lease_token = None
    work.completed_at = timezone.now() if state in {"DONE", "DEAD", "SKIPPED"} else None
    work.save(
        update_fields=[
            "state",
            "result_code",
            "due_at",
            "lease_until",
            "lease_token",
            "completed_at",
        ]
    )
    extra = {
        "event": "notification.attempt.finished",
        "correlation_id": str(work.outbox.event.operation.correlation_id),
        "job_id": str(work.pk),
        "attempt_id": str(attempt.pk),
        "channel": work.kind,
        "attempt": attempt.number,
        "recipient_category": "replenisher" if work.kind == "PUSH" else work.destination,
        "status": state,
        "error_type": code,
    }
    provider_http_status = _provider_http_status(code)
    if provider_http_status is not None:
        extra["provider_http_status"] = provider_http_status
    logger.info(
        "notification.attempt.finished",
        extra=extra,
    )
    return True


@transaction.atomic
def claim(work_item_id=None):
    now = timezone.now()
    work = WorkItem.objects.select_for_update(skip_locked=True).filter(
        Q(state__in=["PENDING", "RETRY"], due_at__lte=now)
        | Q(state="CLAIMED", lease_until__lte=now)
    )
    if work_item_id is not None:
        work = work.filter(pk=work_item_id)
    work = work.order_by("due_at", "pk").first()
    if work is None:
        return None
    if work.lease_token:
        previous = Attempt.objects.get(token=work.lease_token)
        AttemptResult.objects.get_or_create(
            attempt=previous, defaults={"outcome": "UNKNOWN", "code": "LEASE_EXPIRED"}
        )
    if work.attempts >= settings.NOTIFICATION_MAX_ATTEMPTS:
        work.state, work.result_code = "DEAD", "RETRIES_EXHAUSTED"
        work.lease_token = work.lease_until = None
        work.completed_at = now
        work.save()
        return None
    work.state, work.lease_token = "CLAIMED", uuid.uuid4()
    work.lease_until = now + timedelta(seconds=settings.NOTIFICATION_LEASE_SECONDS)
    work.attempts += 1
    work.save()
    return Attempt.objects.create(work=work, number=work.attempts, token=work.lease_token)


def perform(attempt, *, allow_external=False):
    from .senders import send_email, send_push

    try:
        # Pas de verrou de stock. Le verrou de job clôture le lease pour toute la
        # transaction (y compris l'I/O bornée) ; aucune reprise concurrente de ce job.
        # Push prend les verrous compte/session avant le job pour respecter M1.
        if attempt.work.kind == "PUSH":
            return send_push(attempt, allow_external=allow_external)
        with transaction.atomic():
            work = (
                WorkItem.objects.select_for_update(of=("self",))
                .select_related("outbox__event__operation__intent")
                .get(pk=attempt.work_id)
            )
            if work.state != "CLAIMED" or work.lease_token != attempt.token:
                return False
            if work.outbox.event.operation.intent.generation_id != settings.OPERATION_GENERATION:
                return result(work, attempt, "SKIPPED", "STALE_GENERATION")
            if work.kind == "EVENT":
                dispatcher.process(work)
            elif work.kind == "EMAIL":
                code = send_email(work, allow_external=allow_external)
                return result(
                    work,
                    attempt,
                    "SKIPPED" if code else "DONE",
                    code or ("CAPTURED" if work.mode == "CAPTURE" else "SENT"),
                )
            return result(work, attempt, "DONE", "PROJECTED")
    except Exception as exc:
        # L'exception n'est jamais sérialisée : les bibliothèques transport y mettent
        # parfois l'endpoint, la clef ou le message. Le bail et la trace sont conservés.
        code = exc.code if isinstance(exc, DeliveryFailure) else "TECHNICAL_FAILURE"
        permanent = isinstance(exc, DeliveryFailure) and exc.permanent
        with transaction.atomic():
            work = (
                WorkItem.objects.select_for_update(of=("self",))
                .select_related("outbox__event__operation")
                .get(pk=attempt.work_id)
            )
            delay = (
                exc.delay
                if isinstance(exc, DeliveryFailure) and exc.delay is not None
                else min(300, 2**work.attempts)
            )
            return result(
                work,
                attempt,
                "DEAD"
                if permanent or work.attempts >= settings.NOTIFICATION_MAX_ATTEMPTS
                else "RETRY",
                code,
                delay=max(1, min(300, delay)),
            )


def drain(*, limit=100, allow_external=False, on_progress=None):
    dispatcher.enqueue_events(limit)
    count = 0
    for _ in range(limit):
        if on_progress:
            on_progress()
        attempt = claim()
        if attempt is None:
            break
        perform(attempt, allow_external=allow_external)
        count += 1
    return count
