"""Demandes : faits de création/annulation et projections contrôlées, aucun stock d'unité."""

import uuid

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy


class InternalRequest(models.Model):
    class Status(models.TextChoices):
        NEW = "NEW", gettext_lazy("Nouvelle")
        PREPARING = "PREPARING", gettext_lazy("En préparation")
        PARTIAL_OPEN = "PARTIAL_OPEN", gettext_lazy("Partiellement livrée — reste à livrer")
        DELIVERED = "DELIVERED", gettext_lazy("Livrée")
        CANCELLED = "CANCELLED", gettext_lazy("Annulée")
        REFUSED = "REFUSED", gettext_lazy("Refusée")
        PARTIAL_CLOSED = "PARTIAL_CLOSED", gettext_lazy("Partiellement livrée — clôturée")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    reference = models.CharField(max_length=32, unique=True)
    assistant = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    assistant_name = models.CharField(max_length=150)
    floor = models.ForeignKey("catalog.Floor", on_delete=models.PROTECT)
    floor_code = models.CharField(max_length=32)
    floor_name = models.CharField(max_length=120)
    creation_operation = models.OneToOneField(
        "core.Operation", on_delete=models.PROTECT, related_name="created_request"
    )
    status = models.CharField(max_length=24, choices=Status, default=Status.NEW)
    version = models.PositiveBigIntegerField(default=0)
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        on_delete=models.PROTECT,
        related_name="assigned_requests",
    )
    closure_reason = models.CharField(max_length=24, default="")
    # A01 : le premier fait confirmé reste ancré, même lors de futures corrections.
    first_delivery_operation = models.ForeignKey(
        "core.Operation", null=True, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        indexes = [models.Index(fields=["assistant", "status", "id"])]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(
                    status__in=[
                        "NEW",
                        "PREPARING",
                        "PARTIAL_OPEN",
                        "DELIVERED",
                        "CANCELLED",
                        "REFUSED",
                        "PARTIAL_CLOSED",
                    ]
                ),
                name="m5_request_states",
            )
        ]


class RequestLine(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    request = models.ForeignKey(InternalRequest, on_delete=models.PROTECT, related_name="lines")
    product = models.ForeignKey("catalog.Product", on_delete=models.PROTECT)
    quantity = models.PositiveBigIntegerField()
    delivered = models.PositiveBigIntegerField(default=0)
    closed = models.PositiveBigIntegerField(default=0)
    product_code = models.CharField(max_length=64)
    product_name = models.CharField(max_length=150)
    unit = models.CharField(max_length=40)
    maximum = models.PositiveBigIntegerField()
    permission_version = models.PositiveBigIntegerField()
    product_version = models.PositiveBigIntegerField()
    photo = models.ForeignKey("catalog.ProductPhoto", null=True, on_delete=models.PROTECT)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["request", "product"], name="request_product_unique"),
            models.CheckConstraint(
                condition=models.Q(
                    quantity__gt=0,
                    quantity__lte=1000000000,
                    maximum__gte=models.F("quantity"),
                    delivered__lte=models.F("quantity"),
                    closed__lte=models.F("quantity"),
                ),
                name="m5_line_quantities",
            ),
        ]


class Reservation(models.Model):
    line = models.OneToOneField(
        RequestLine, primary_key=True, on_delete=models.PROTECT, related_name="reservation"
    )
    product = models.ForeignKey("catalog.Product", on_delete=models.PROTECT)
    remaining = models.PositiveBigIntegerField()
    version = models.PositiveBigIntegerField(default=0)


class RequestEvent(models.Model):
    class Kind(models.TextChoices):
        CREATED = "CREATED", gettext_lazy("Demande envoyée")
        CANCELLED = "CANCELLED", gettext_lazy("Demande annulée")
        REFUSED = "REFUSED", gettext_lazy("Demande refusée")
        CORRECTED = "CORRECTED", gettext_lazy("Livraison corrigée par la gestionnaire")
        CLOSED = "CLOSED", gettext_lazy("Reliquat abandonné par la gestionnaire")
        REOPENED = "REOPENED", gettext_lazy("Quantités rouvertes par la gestionnaire")
        REASSIGNED = "REASSIGNED", gettext_lazy("Demande réattribuée par la gestionnaire")
        RECONCILED = "RECONCILED", gettext_lazy("Engagement réduit après comptage vérifié")
        CLAIMED = "CLAIMED", gettext_lazy("Demande prise en charge")
        DELIVERED = "DELIVERED", gettext_lazy("Remise confirmée")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    request = models.ForeignKey(InternalRequest, on_delete=models.PROTECT, related_name="events")
    operation = models.ForeignKey("core.Operation", on_delete=models.PROTECT)
    kind = models.CharField(max_length=16, choices=Kind)
    revision = models.PositiveBigIntegerField(default=0)
    message = models.CharField(max_length=2000, default="", blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["request", "revision"], name="request_event_revision"),
            models.UniqueConstraint(
                fields=["request", "operation"], name="request_operation_unique"
            ),
            models.CheckConstraint(
                condition=models.Q(
                    kind__in=[
                        "CREATED",
                        "CANCELLED",
                        "CLAIMED",
                        "DELIVERED",
                        "REFUSED",
                        "CORRECTED",
                        "CLOSED",
                        "REOPENED",
                        "REASSIGNED",
                        "RECONCILED",
                    ]
                ),
                name="m5_event_kind",
            ),
            models.CheckConstraint(
                condition=models.Q(message="") | models.Q(kind="REFUSED"),
                name="request_refusal_message",
            ),
        ]


class ReservationChange(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    event = models.ForeignKey(RequestEvent, on_delete=models.PROTECT, related_name="changes")
    line = models.ForeignKey(RequestLine, on_delete=models.PROTECT, related_name="changes")
    entry = models.OneToOneField("inventory.StockEntry", on_delete=models.PROTECT)


class Delivery(models.Model):
    class Mode(models.TextChoices):
        COMPLETE = "COMPLETE", gettext_lazy("Remise complète")
        LATER = "LATER", gettext_lazy("À apporter plus tard")
        SHORTAGE = "SHORTAGE", gettext_lazy("Manquant dans la réserve")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    request = models.ForeignKey(
        InternalRequest, on_delete=models.PROTECT, related_name="deliveries"
    )
    operation = models.OneToOneField("core.Operation", on_delete=models.PROTECT)
    event = models.OneToOneField(RequestEvent, on_delete=models.PROTECT)
    operator = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    operator_name = models.CharField(max_length=150)
    mode = models.CharField(max_length=16, choices=Mode)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(mode__in=["COMPLETE", "LATER", "SHORTAGE"]), name="delivery_mode"
            )
        ]


class DeliveryLine(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    delivery = models.ForeignKey(Delivery, on_delete=models.PROTECT, related_name="lines")
    line = models.ForeignKey(RequestLine, on_delete=models.PROTECT, related_name="delivery_lines")
    remaining_before = models.PositiveBigIntegerField()
    handed = models.PositiveBigIntegerField()
    missing = models.PositiveBigIntegerField()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["delivery", "line"], name="delivery_line_unique"),
            models.CheckConstraint(
                condition=models.Q(
                    handed__lte=models.F("remaining_before"),
                    missing__lte=models.F("remaining_before") - models.F("handed"),
                ),
                name="delivery_quantities",
            ),
        ]


class DeliveryIssue(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    delivery = models.ForeignKey(Delivery, on_delete=models.PROTECT, related_name="issues")
    operation = models.ForeignKey("core.Operation", on_delete=models.PROTECT)
    kind = models.CharField(max_length=16)
    message = models.CharField(max_length=2000, default="")
    status = models.CharField(max_length=8, default="OPEN")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["operation", "kind"], name="delivery_issue_operation_kind"
            ),
            models.UniqueConstraint(
                fields=["delivery"],
                condition=models.Q(kind="DELIVERY_ERROR"),
                name="one_open_error_per_delivery",
            ),
            models.CheckConstraint(
                condition=models.Q(kind__in=["SHORTAGE", "DELIVERY_ERROR"], status="OPEN"),
                name="m5_issue_scope",
            ),
        ]


class RequestDecision(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    event = models.OneToOneField(RequestEvent, on_delete=models.PROTECT, related_name="decision")
    case = models.ForeignKey("cases.Case", null=True, on_delete=models.PROTECT)
    cause = models.CharField(max_length=24)
    verification = models.BooleanField()
    message = models.CharField(max_length=2000)
    disposition = models.CharField(max_length=16, default="")
    previous_operator = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT, related_name="+"
    )
    next_operator = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT, related_name="+"
    )


class DecisionLine(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    decision = models.ForeignKey(RequestDecision, on_delete=models.PROTECT, related_name="lines")
    line = models.ForeignKey(RequestLine, on_delete=models.PROTECT)
    delivered_before = models.PositiveBigIntegerField()
    delivered_after = models.PositiveBigIntegerField()
    closed_before = models.PositiveBigIntegerField()
    closed_after = models.PositiveBigIntegerField()
    reserved_before = models.PositiveBigIntegerField()
    reserved_after = models.PositiveBigIntegerField()
    entry = models.OneToOneField("inventory.StockEntry", null=True, on_delete=models.PROTECT)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["decision", "line"], name="decision_line_unique")
        ]


class DeliveryCorrection(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    decision = models.OneToOneField(
        RequestDecision, on_delete=models.PROTECT, related_name="correction"
    )
    delivery = models.ForeignKey(Delivery, on_delete=models.PROTECT, related_name="corrections")
    previous = models.OneToOneField(
        "self", null=True, on_delete=models.PROTECT, related_name="next_correction"
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["delivery"],
                condition=models.Q(previous__isnull=True),
                name="first_delivery_correction",
            )
        ]


class DeliveryCorrectionLine(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    correction = models.ForeignKey(
        DeliveryCorrection, on_delete=models.PROTECT, related_name="lines"
    )
    delivery_line = models.ForeignKey(
        DeliveryLine, on_delete=models.PROTECT, related_name="corrections"
    )
    effective_before = models.PositiveBigIntegerField()
    effective_after = models.PositiveBigIntegerField()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["correction", "delivery_line"], name="correction_delivery_line"
            )
        ]
