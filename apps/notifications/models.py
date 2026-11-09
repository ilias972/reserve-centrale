import uuid

from django.db import models
from django.utils import timezone


class BusinessEvent(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    operation = models.ForeignKey("core.Operation", on_delete=models.PROTECT)
    event_type = models.CharField(max_length=100)
    aggregate_id = models.UUIDField()
    revision = models.PositiveBigIntegerField(default=1)
    payload = models.JSONField(default=dict)

    class Meta:
        indexes = [
            models.Index(
                fields=["aggregate_id", "revision"],
                condition=models.Q(event_type="recommendation.stock_reactivated"),
                name="notif_stock_reactivation_idx",
            )
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["operation", "event_type", "aggregate_id", "revision"],
                name="event_operation_unique",
            ),
            models.CheckConstraint(condition=models.Q(revision__gte=1), name="event_revision"),
        ]


class Outbox(models.Model):
    """Boîte transactionnelle uniquement. Aucun émetteur externe dans M0."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    event = models.ForeignKey(BusinessEvent, on_delete=models.PROTECT)
    channel = models.CharField(max_length=20, default="INTERNAL")
    destination = models.CharField(max_length=150, default="internal")
    dedupe_key = models.CharField(max_length=200, unique=True)
    state = models.CharField(max_length=20, default="PENDING")
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["event", "channel", "destination"], name="outbox_delivery_unique"
            ),
            models.CheckConstraint(condition=models.Q(state="PENDING"), name="outbox_m0_pending"),
            models.CheckConstraint(
                condition=models.Q(channel="INTERNAL"), name="outbox_m0_internal"
            ),
        ]


class Activation(models.Model):
    id = models.PositiveSmallIntegerField(primary_key=True, default=1)
    started_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=models.Q(id=1), name="notification_activation_one")
        ]


class Notification(models.Model):
    operation = models.ForeignKey("core.Operation", on_delete=models.PROTECT)
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    event = models.ForeignKey(BusinessEvent, on_delete=models.PROTECT)
    recipient = models.ForeignKey("accounts.User", on_delete=models.PROTECT)
    scope = models.CharField(max_length=20)
    title = models.CharField(max_length=240)
    message = models.CharField(max_length=500)
    target_kind = models.CharField(max_length=20)
    target_id = models.UUIDField(null=True)
    fact_key = models.CharField(max_length=100)
    created_at = models.DateTimeField(default=timezone.now)
    read_at = models.DateTimeField(null=True)

    class Meta:
        indexes = [models.Index(fields=["recipient", "read_at", "created_at"])]
        constraints = [
            models.UniqueConstraint(
                fields=["event", "recipient", "fact_key"], name="notification_event_recipient_fact"
            ),
            models.UniqueConstraint(
                fields=["operation", "recipient", "fact_key"],
                name="notification_operation_recipient",
            ),
            models.CheckConstraint(
                condition=models.Q(scope__in=["ADMIN", "REPLENISHER", "REQUESTER"]),
                name="notification_scope",
            ),
        ]


class PushSubscription(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey("accounts.User", on_delete=models.PROTECT)
    session = models.ForeignKey("accounts.UserSession", on_delete=models.PROTECT)
    endpoint = models.CharField(max_length=2048)
    endpoint_digest = models.CharField(max_length=64)
    p256dh = models.CharField(max_length=100)
    auth = models.CharField(max_length=32)
    language = models.CharField(
        max_length=2,
        choices=[("en", "English"), ("fr", "Français"), ("ar", "العربية")],
        default="en",
    )
    created_at = models.DateTimeField(default=timezone.now)
    revoked_at = models.DateTimeField(null=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["endpoint_digest"],
                condition=models.Q(revoked_at__isnull=True),
                name="push_one_active_endpoint",
            )
        ]


class WorkItem(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    kind = models.CharField(max_length=8)
    outbox = models.ForeignKey(Outbox, on_delete=models.PROTECT)
    notification = models.ForeignKey(Notification, null=True, on_delete=models.PROTECT)
    subscription = models.ForeignKey(PushSubscription, null=True, on_delete=models.PROTECT)
    destination = models.CharField(max_length=30, default="")
    email_to = models.EmailField(blank=True, default="")
    mode = models.CharField(max_length=12, default="")
    dedupe_key = models.CharField(max_length=200, unique=True)
    state = models.CharField(max_length=12, default="PENDING")
    due_at = models.DateTimeField(default=timezone.now)
    lease_until = models.DateTimeField(null=True)
    lease_token = models.UUIDField(null=True)
    attempts = models.PositiveIntegerField(default=0)
    result_code = models.CharField(max_length=40, default="")
    created_at = models.DateTimeField(default=timezone.now)
    completed_at = models.DateTimeField(null=True)

    class Meta:
        indexes = [models.Index(fields=["state", "due_at"])]
        constraints = [
            models.UniqueConstraint(
                fields=["outbox"],
                condition=models.Q(kind="EVENT"),
                name="work_one_event_projection",
            ),
            models.UniqueConstraint(
                fields=["outbox", "destination"],
                condition=models.Q(kind="EMAIL"),
                name="work_one_purchase_email_alias",
            ),
            models.UniqueConstraint(
                fields=["notification", "subscription"],
                condition=models.Q(kind="PUSH"),
                name="work_one_push_subscription",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    state__in=["PENDING", "CLAIMED", "RETRY", "DONE", "DEAD", "SKIPPED"]
                ),
                name="notification_work_state",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        kind="EVENT",
                        notification__isnull=True,
                        subscription__isnull=True,
                        destination="",
                        mode="",
                    )
                    | models.Q(
                        kind="EMAIL",
                        notification__isnull=True,
                        subscription__isnull=True,
                        destination__in=["RESPONSIBLE", "ACCOUNTANT"],
                        mode__in=["CAPTURE", "SMTP"],
                    )
                    | models.Q(
                        kind="PUSH",
                        notification__isnull=False,
                        subscription__isnull=False,
                        destination="",
                        mode="PUSH",
                    )
                ),
                name="notification_work_type",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(state="CLAIMED", lease_token__isnull=False, lease_until__isnull=False)
                    | (
                        ~models.Q(state="CLAIMED")
                        & models.Q(lease_token__isnull=True, lease_until__isnull=True)
                    )
                ),
                name="notification_work_lease",
            ),
        ]


class Attempt(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    work = models.ForeignKey(WorkItem, on_delete=models.PROTECT, related_name="history")
    number = models.PositiveIntegerField()
    token = models.UUIDField(unique=True)
    started_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["work", "number"], name="notification_attempt_number")
        ]


class AttemptResult(models.Model):
    attempt = models.OneToOneField(Attempt, primary_key=True, on_delete=models.PROTECT)
    finished_at = models.DateTimeField(default=timezone.now)
    outcome = models.CharField(max_length=12)
    code = models.CharField(max_length=40)


class CapturedEmail(models.Model):
    work = models.OneToOneField(WorkItem, primary_key=True, on_delete=models.PROTECT)
    message_id = models.CharField(max_length=150, unique=True)
    recipient = models.EmailField()
    subject = models.CharField(max_length=250)
    body = models.TextField()
    captured_at = models.DateTimeField(default=timezone.now)


class ProductAlertState(models.Model):
    product = models.OneToOneField("catalog.Product", primary_key=True, on_delete=models.PROTECT)
    need = models.BooleanField(default=False)
    shortage = models.BooleanField(default=False)
    updated_at = models.DateTimeField(default=timezone.now)
