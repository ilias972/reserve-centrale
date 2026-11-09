"""Déclarations A03 immuables et projections de traitement M6."""

import uuid

from django.conf import settings
from django.db import models


class StockCase(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    failed_intent = models.OneToOneField("core.OperationIntent", on_delete=models.PROTECT)
    operation = models.OneToOneField("core.Operation", on_delete=models.PROTECT)
    reporter = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    kind = models.CharField(max_length=24, default="STOCK_CONFLICT")
    status = models.CharField(max_length=16, default="OPEN")

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(kind="STOCK_CONFLICT", status="OPEN"), name="m3_stock_case_scope"
            )
        ]

    @property
    def reference(self):
        return "SIG-" + str(self.pk).upper()


class StockCaseProduct(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    case = models.ForeignKey(StockCase, on_delete=models.PROTECT, related_name="products")
    product = models.ForeignKey("catalog.Product", on_delete=models.PROTECT)
    product_code = models.CharField(max_length=64)
    product_name = models.CharField(max_length=150)
    unit = models.CharField(max_length=40)
    physical = models.PositiveBigIntegerField()
    reserved = models.PositiveBigIntegerField()
    attempted_physical = models.BigIntegerField()
    is_calculated = models.BooleanField(default=False)
    stock_version = models.PositiveBigIntegerField()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["case", "product"], name="case_product_pair"),
            models.CheckConstraint(
                condition=models.Q(attempted_physical__lt=models.F("reserved"))
                & (models.Q(is_calculated=True) | models.Q(attempted_physical__gte=0)),
                name="stock_case_below_reserved",
            ),
        ]


class StockHold(models.Model):
    product = models.OneToOneField(
        "catalog.Product", primary_key=True, on_delete=models.PROTECT, related_name="stock_hold"
    )
    case = models.ForeignKey(StockCase, on_delete=models.PROTECT)
    # Résolution/levée en M6 seulement. Aucun UPDATE/DELETE accordé en M3.


class Case(models.Model):
    """Projection de traitement ; les déclarations StockCase/DeliveryIssue restent intactes."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    stock_case = models.OneToOneField(StockCase, null=True, on_delete=models.PROTECT)
    delivery = models.ForeignKey("requests.Delivery", null=True, on_delete=models.PROTECT)
    delivery_issue = models.OneToOneField(
        "requests.DeliveryIssue", null=True, on_delete=models.PROTECT
    )
    receipt = models.ForeignKey("receiving.Receipt", null=True, on_delete=models.PROTECT)
    origin_operation = models.ForeignKey(
        "core.Operation", on_delete=models.PROTECT, related_name="+"
    )
    kind = models.CharField(max_length=24)
    status = models.CharField(max_length=16, default="OPEN")
    version = models.PositiveBigIntegerField(default=0)

    class Meta:
        indexes = [models.Index(fields=["status", "kind"])]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(status__in=["OPEN", "UNDER_REVIEW", "RESOLVED"]),
                name="case_status",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    kind="STOCK_CONFLICT",
                    stock_case__isnull=False,
                    delivery__isnull=True,
                    receipt__isnull=True,
                )
                | models.Q(
                    kind__in=["DELIVERY_ERROR", "SHORTAGE"],
                    stock_case__isnull=True,
                    delivery__isnull=False,
                    receipt__isnull=True,
                )
                | models.Q(
                    kind__in=[
                        "SURPLUS",
                        "NONCONFORMITY",
                        "UNKNOWN_ORDER",
                        "LATE_CANCELLED_DELIVERY",
                        "RECEIPT_ERROR",
                    ],
                    stock_case__isnull=True,
                    delivery__isnull=True,
                    delivery_issue__isnull=True,
                ),
                name="case_typed_origin",
            ),
            models.UniqueConstraint(fields=["delivery", "kind"], name="case_delivery_kind"),
            models.UniqueConstraint(fields=["receipt", "kind"], name="case_receipt_kind"),
        ]

    @property
    def reference(self):
        return "SIG-" + str(self.pk).upper()


class CaseEvent(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    case = models.ForeignKey(Case, on_delete=models.PROTECT, related_name="events")
    operation = models.ForeignKey("core.Operation", on_delete=models.PROTECT)
    revision = models.PositiveBigIntegerField()
    kind = models.CharField(max_length=24)
    status = models.CharField(max_length=16)
    details = models.JSONField(default=dict)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["case", "revision"], name="case_event_revision"),
            models.UniqueConstraint(fields=["case", "operation"], name="case_event_operation"),
        ]


class StockReconciliation(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    case = models.ForeignKey(Case, on_delete=models.PROTECT)
    operation = models.OneToOneField("core.Operation", on_delete=models.PROTECT)
    verification = models.BooleanField()
    message = models.CharField(max_length=2000)


class ReconciledStock(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    reconciliation = models.ForeignKey(
        StockReconciliation, on_delete=models.PROTECT, related_name="lines"
    )
    product = models.ForeignKey("catalog.Product", on_delete=models.PROTECT)
    physical_before = models.PositiveBigIntegerField()
    reserved_before = models.PositiveBigIntegerField()
    counted_quantity = models.PositiveBigIntegerField()
    reserved_after = models.PositiveBigIntegerField()
    version_before = models.PositiveBigIntegerField()
    version_after = models.PositiveBigIntegerField()
    entry = models.OneToOneField("inventory.StockEntry", null=True, on_delete=models.PROTECT)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["reconciliation", "product"], name="reconciled_product"
            ),
            models.CheckConstraint(
                condition=models.Q(counted_quantity__gte=models.F("reserved_after")),
                name="reconciliation_balanced",
            ),
        ]
