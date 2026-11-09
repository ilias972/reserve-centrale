from dataclasses import dataclass
from datetime import timedelta
from uuid import uuid4

from django.conf import settings
from django.db import IntegrityError, OperationalError, transaction
from django.utils.translation import gettext

from apps.audit.models import AuditEvent
from apps.notifications.models import BusinessEvent, Outbox

from .commands import Event, get_command, projected_events
from .errors import BusinessError
from .models import Operation, OperationIntent
from .payloads import parse_uuid, payload_hash, safe_record, validate_binding
from .policies import check_role, lock_actor
from .transactions import configure_transaction, database_now


@dataclass(frozen=True)
class Outcome:
    body: dict
    status: int = 200
    replayed: bool = False
    once: dict | None = None


@dataclass(frozen=True)
class Execution:
    actor: object
    intent: OperationIntent
    operation_id: object
    correlation_id: object
    context: object = None


def authorize(actor, intent):
    if intent.actor_id != actor.pk:
        raise BusinessError("NOT_FOUND", gettext("Opération introuvable."), status=404)
    if intent.generation_id != settings.OPERATION_GENERATION:
        raise BusinessError(
            "RECOVERY_RECONCILIATION_REQUIRED",
            gettext("Cette opération nécessite une vérification de reprise."),
        )
    command = get_command(intent.command_type)
    check_role(actor, command.roles)
    command.authorize(actor, intent.resource_id)
    return command


def register_intent(context, *, intent_id, key, fingerprint, command_type, resource_id=None):
    intent_id = parse_uuid(intent_id)
    resource_id = parse_uuid(resource_id) if resource_id is not None else None
    validate_binding(key, fingerprint)
    command = get_command(command_type)
    with transaction.atomic(durable=True):
        configure_transaction()
        actor = lock_actor(context)
        check_role(actor, command.roles)
        command.authorize(actor, resource_id)
        now = database_now()
        try:
            with transaction.atomic():
                intent, _ = OperationIntent.objects.get_or_create(
                    actor=actor,
                    idempotency_key=key,
                    defaults={
                        "id": intent_id,
                        "command_type": command_type,
                        "resource_id": resource_id,
                        "payload_hash": fingerprint,
                        "generation_id": settings.OPERATION_GENERATION,
                        "created_at": now,
                        "expires_at": now
                        + timedelta(seconds=settings.OPERATION_INTENT_TTL_SECONDS),
                    },
                )
        except IntegrityError as exc:
            raise BusinessError(
                "IDEMPOTENCY_MISMATCH", gettext("Identifiant d'opération déjà utilisé.")
            ) from exc
        if (
            intent.pk != intent_id
            or intent.payload_hash != fingerprint
            or intent.command_type != command_type
            or intent.resource_id != resource_id
            or intent.generation_id != settings.OPERATION_GENERATION
        ):
            raise BusinessError(
                "IDEMPOTENCY_MISMATCH", gettext("La clé correspond à une autre opération.")
            )
        return Outcome(describe(intent))


def find_intent(actor, intent_id):
    try:
        return OperationIntent.objects.get(actor=actor, pk=parse_uuid(intent_id))
    except OperationIntent.DoesNotExist as exc:
        raise BusinessError("NOT_FOUND", gettext("Opération introuvable."), status=404) from exc


def describe(intent):
    return {
        "intent_id": str(intent.pk),
        "state": intent.state,
        "expires_at": intent.expires_at.isoformat(),
        "http_status": intent.http_status,
        "response": intent.response,
    }


def read_intent(context, intent_id):
    with transaction.atomic(durable=True):
        configure_transaction()
        actor = lock_actor(context)
        intent = find_intent(actor, intent_id)
        authorize(actor, intent)
        # READY n'est pas une preuve d'absence d'une transaction concurrente en cours.
        return Outcome(describe(intent))


def finish(intent, state, body, status):
    intent.state = state
    intent.response = body
    intent.http_status = status
    intent.completed_at = database_now()
    intent.save(update_fields=["state", "response", "http_status", "completed_at"])


def expired(intent):
    body = BusinessError(
        "INTENT_EXPIRED", gettext("Cette confirmation n'est plus utilisable.")
    ).response()
    finish(intent, OperationIntent.State.EXPIRED, body, 409)
    return Outcome(body, 409)


def replay(intent):
    return Outcome(intent.response, intent.http_status, replayed=True)


def resolve_intent(context, intent_id):
    with transaction.atomic(durable=True):
        configure_transaction()
        actor = lock_actor(context)
        metadata = find_intent(actor, intent_id)
        authorize(actor, metadata)
        try:
            with transaction.atomic():
                intent = OperationIntent.objects.select_for_update(nowait=True).get(pk=metadata.pk)
        except OperationalError as exc:
            if getattr(exc.__cause__, "sqlstate", None) != "55P03":
                raise
            return Outcome({"intent_id": str(metadata.pk), "state": "CHECKING"}, 202)
        if intent.state != OperationIntent.State.READY:
            return replay(intent)
        return expired(intent)


def execute_intent(context, *, intent_id, key, payload, correlation_id=None):
    fingerprint = payload_hash(payload)
    validate_binding(key, fingerprint)
    correlation_id = correlation_id or uuid4()
    with transaction.atomic(durable=True):
        configure_transaction()
        # M1 : les changements d'accès verrouillent d'abord les comptes ordonnés,
        # avant le verrou partagé d'acteur et avant l'intention. Les autres
        # commandes conservent la concurrence du noyau M0.
        metadata = OperationIntent.objects.filter(
            actor_id=context.actor_id, pk=parse_uuid(intent_id)
        ).first()
        if metadata is not None:
            registered = get_command(metadata.command_type)
            if registered.lock_payload_accounts:
                registered.lock_payload_accounts(context, metadata.resource_id, payload)
            if registered.lock_accounts:
                registered.lock_accounts(context, metadata.resource_id)
        actor = lock_actor(context)
        metadata = find_intent(actor, intent_id)
        command = authorize(actor, metadata)
        if command.lock_references:
            command.lock_references(actor, metadata.resource_id)
        if command.lock_payload_references:
            command.lock_payload_references(actor, metadata.resource_id, payload)
        intent = OperationIntent.objects.select_for_update().get(pk=metadata.pk)
        if intent.idempotency_key != key or intent.payload_hash != fingerprint:
            raise BusinessError(
                "IDEMPOTENCY_MISMATCH", gettext("La confirmation reçue a été modifiée.")
            )
        if intent.state != OperationIntent.State.READY:
            return replay(intent)
        if intent.expires_at <= database_now():
            return expired(intent)
        resources = (
            command.lock_payload_resources(actor, intent.resource_id, payload)
            if command.lock_payload_resources
            else command.lock_resources(actor, intent.resource_id)
        )
        execution = Execution(actor, intent, uuid4(), correlation_id, context)
        try:
            with transaction.atomic():
                operation = Operation.objects.create(
                    id=execution.operation_id,
                    intent=intent,
                    actor=actor,
                    actor_role=actor.role,
                    command_type=intent.command_type,
                    resource_id=intent.resource_id,
                    confirmed_at=database_now(),
                    correlation_id=correlation_id,
                )
                effect = command.apply(execution, resources, payload)
                record_facts(operation, effect)
                body = {
                    "operation_id": str(operation.pk),
                    "intent_id": str(intent.pk),
                    "result": "CONFIRMED",
                    "resource": effect.resource,
                    "notifications": "QUEUED",
                }
        except BusinessError as exc:
            extra = {}
            if command.record_rejection:
                extra = command.record_rejection(execution, resources, exc)
                safe_record(extra)
            body = {**exc.response(), **extra}
            finish(intent, OperationIntent.State.REJECTED, body, exc.status)
            return Outcome(body, exc.status)
        finish(intent, OperationIntent.State.CONFIRMED, body, effect.http_status)
        return Outcome(body, effect.http_status, once=effect.once)


def record_facts(operation, effect):
    for item in (effect.resource, effect.before, effect.after, effect.details):
        safe_record(item)
    if not 200 <= effect.http_status < 300:
        raise ValueError("Une confirmation exige un résultat de succès.")
    AuditEvent.objects.create(
        operation=operation,
        before=effect.before,
        after=effect.after,
        details=effect.details,
    )
    events = (
        Event("operation.confirmed", operation.pk, 1, {"kind": operation.command_type}),
        *effect.events,
        *projected_events(operation),
    )
    for item in events:
        safe_record(item.payload)
        event = BusinessEvent.objects.create(
            operation=operation,
            event_type=item.event_type,
            aggregate_id=item.aggregate_id,
            revision=item.revision,
            payload=item.payload,
        )
        Outbox.objects.create(event=event, dedupe_key=f"{event.pk}:internal")
