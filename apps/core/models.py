import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone


class OperationIntent(models.Model):
    class State(models.TextChoices):
        READY = "READY"
        CONFIRMED = "CONFIRMED"
        REJECTED = "REJECTED"
        EXPIRED = "EXPIRED"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    command_type = models.CharField(max_length=80)
    resource_id = models.UUIDField(null=True, blank=True)
    idempotency_key = models.CharField(max_length=128)
    payload_hash = models.CharField(max_length=64)
    generation_id = models.CharField(max_length=64)
    state = models.CharField(max_length=12, choices=State, default=State.READY)
    created_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField()
    completed_at = models.DateTimeField(null=True)
    response = models.JSONField(null=True)
    http_status = models.PositiveSmallIntegerField(null=True)

    class Meta:
        indexes = [models.Index(fields=["actor", "created_at"])]
        constraints = [
            models.UniqueConstraint(fields=["actor", "idempotency_key"], name="intent_actor_key"),
            models.CheckConstraint(
                condition=models.Q(payload_hash__regex=r"^[0-9a-f]{64}$"),
                name="intent_payload_hash",
            ),
            models.CheckConstraint(
                condition=models.Q(idempotency_key__regex=r"^[A-Za-z0-9_-]{16,128}$"),
                name="intent_key_format",
            ),
            models.CheckConstraint(
                condition=models.Q(expires_at__gt=models.F("created_at")), name="intent_deadline"
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        state="READY",
                        response__isnull=True,
                        http_status__isnull=True,
                        completed_at__isnull=True,
                    )
                    | models.Q(
                        state="CONFIRMED",
                        response__isnull=False,
                        http_status__isnull=False,
                        http_status__gte=200,
                        http_status__lt=300,
                        completed_at__isnull=False,
                    )
                    | models.Q(
                        state="REJECTED",
                        response__isnull=False,
                        http_status__isnull=False,
                        http_status__gte=400,
                        http_status__lt=500,
                        completed_at__isnull=False,
                    )
                    | models.Q(
                        state="EXPIRED",
                        response__isnull=False,
                        http_status__isnull=False,
                        http_status=409,
                        completed_at__isnull=False,
                    )
                ),
                name="intent_state_result",
            ),
        ]


class Operation(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    intent = models.OneToOneField(OperationIntent, on_delete=models.PROTECT)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    actor_role = models.CharField(max_length=20)
    command_type = models.CharField(max_length=80)
    resource_id = models.UUIDField(null=True)
    confirmed_at = models.DateTimeField(default=timezone.now)
    correlation_id = models.UUIDField()
    corrects_operation = models.ForeignKey("self", on_delete=models.PROTECT, null=True)
