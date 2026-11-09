"""M7 : achats déclarés et annulations ; aucune réception, aucun stock parallèle."""

import uuid

from django.db import models
from django.utils.translation import gettext_lazy


class Recommendation(models.Model):
    product = models.OneToOneField(
        "catalog.Product",
        on_delete=models.PROTECT,
        primary_key=True,
        related_name="purchase_recommendation",
    )
    version = models.PositiveBigIntegerField(default=0)
    latest_event = models.OneToOneField(
        "RecommendationEvent", on_delete=models.PROTECT, null=True, related_name="+"
    )


class RecommendationEvent(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    recommendation = models.ForeignKey(
        Recommendation, on_delete=models.PROTECT, related_name="events"
    )
    operation = models.OneToOneField("core.Operation", on_delete=models.PROTECT)
    action = models.CharField(
        max_length=12,
        choices=[("DEFER", gettext_lazy("Reporter")), ("REACTIVATE", gettext_lazy("Réactiver"))],
    )
    revision = models.PositiveBigIntegerField()
    available = models.PositiveBigIntegerField()
    stock_version = models.PositiveBigIntegerField()
    product_version = models.PositiveBigIntegerField()
    before = models.JSONField()
    after = models.JSONField()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["recommendation", "revision"], name="recommendation_revision"
            ),
            models.CheckConstraint(
                condition=models.Q(revision__gte=1, action__in=["DEFER", "REACTIVATE"]),
                name="recommendation_event_valid",
            ),
        ]


class PurchaseOrder(models.Model):
    class Status(models.TextChoices):
        WAITING = "WAITING", gettext_lazy("En attente")
        PARTIAL = "PARTIAL", gettext_lazy("Partiellement reçue")
        RECEIVED = "RECEIVED", gettext_lazy("Reçue")
        CLOSED_REMAINDER_CANCELLED = (
            "CLOSED_REMAINDER_CANCELLED",
            gettext_lazy("Clôturée avec reliquat annulé"),
        )
        CANCELLED_NO_RECEIPT = "CANCELLED_NO_RECEIPT", gettext_lazy("Annulée sans réception")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    reference = models.CharField(max_length=32, unique=True)
    supplier = models.ForeignKey("catalog.Supplier", on_delete=models.PROTECT)
    author_name = models.CharField(max_length=150)
    supplier_code = models.CharField(max_length=32)
    supplier_name = models.CharField(max_length=150)
    supplier_website = models.CharField(max_length=500, blank=True)
    purchased_on = models.DateField()
    creation_operation = models.OneToOneField("core.Operation", on_delete=models.PROTECT)
    external_reference = models.CharField(max_length=160, blank=True)
    reference_key = models.CharField(max_length=160, blank=True)
    lines_key = models.JSONField()
    amount = models.DecimalField(max_digits=14, decimal_places=2, null=True)
    currency = models.CharField(max_length=3, blank=True)
    status = models.CharField(max_length=32, choices=Status, default=Status.WAITING)
    version = models.PositiveBigIntegerField(default=0)

    class Meta:
        indexes = [
            models.Index(fields=["supplier", "reference_key"]),
            models.Index(fields=["supplier", "purchased_on"]),
            models.Index(fields=["status", "purchased_on", "id"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(
                    status__in=[
                        "WAITING",
                        "PARTIAL",
                        "RECEIVED",
                        "CLOSED_REMAINDER_CANCELLED",
                        "CANCELLED_NO_RECEIPT",
                    ]
                ),
                name="order_status_valid",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(amount__isnull=True, currency="")
                    | (models.Q(amount__isnull=False, amount__gte=0, currency__regex=r"^[A-Z]{3}$"))
                ),
                name="order_amount_currency",
            ),
        ]


class OrderLine(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    order = models.ForeignKey(PurchaseOrder, on_delete=models.PROTECT, related_name="lines")
    product = models.ForeignKey("catalog.Product", on_delete=models.PROTECT)
    product_code = models.CharField(max_length=64)
    product_name = models.CharField(max_length=150)
    unit = models.CharField(max_length=40)
    photo = models.ForeignKey("catalog.ProductPhoto", on_delete=models.PROTECT, null=True)
    supplier_reference = models.CharField(max_length=150, blank=True)
    quantity = models.PositiveBigIntegerField()
    product_version = models.PositiveBigIntegerField()
    received = models.PositiveBigIntegerField(default=0)
    cancelled = models.PositiveBigIntegerField(default=0)

    @property
    def waiting(self):
        return self.quantity - self.received - self.cancelled

    class Meta:
        indexes = [models.Index(fields=["product", "order"])]
        constraints = [
            models.UniqueConstraint(fields=["order", "product"], name="order_product_unique"),
            models.CheckConstraint(
                condition=models.Q(
                    quantity__gte=1,
                    quantity__lte=1000000000,
                    received__gte=0,
                    cancelled__gte=0,
                )
                & models.Q(quantity__gte=models.F("received") + models.F("cancelled")),
                name="order_line_equation",
            ),
        ]


class DuplicateAcknowledgement(models.Model):
    order = models.OneToOneField(PurchaseOrder, primary_key=True, on_delete=models.PROTECT)
    candidate_ids = models.JSONField()
    candidate_fingerprint = models.CharField(max_length=64)
    verified_distinct = models.BooleanField()

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(verified_distinct=True), name="purchase_distinct_verified"
            )
        ]


class RemainderEvent(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    order = models.ForeignKey(PurchaseOrder, on_delete=models.PROTECT, related_name="events")
    operation = models.OneToOneField("core.Operation", on_delete=models.PROTECT)
    revision = models.PositiveBigIntegerField()
    external_verification = models.BooleanField()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["order", "revision"], name="order_remainder_revision"),
            models.CheckConstraint(
                condition=models.Q(revision__gte=1, external_verification=True),
                name="order_remainder_verified",
            ),
        ]


class RemainderLine(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    event = models.ForeignKey(RemainderEvent, on_delete=models.PROTECT, related_name="lines")
    line = models.ForeignKey(OrderLine, on_delete=models.PROTECT, related_name="cancellations")
    quantity = models.PositiveBigIntegerField()
    cancelled_before = models.PositiveBigIntegerField()
    cancelled_after = models.PositiveBigIntegerField()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["event", "line"], name="remainder_line_unique"),
            models.CheckConstraint(
                condition=models.Q(
                    quantity__gte=1,
                    cancelled_after=models.F("cancelled_before") + models.F("quantity"),
                ),
                name="remainder_line_delta",
            ),
        ]
