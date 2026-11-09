"""M8 : faits d'arrivée, quantités isolées et décisions ; jamais de second stock."""

import uuid

from django.db import models
from django.utils.translation import gettext_lazy

Q = models.Q
F = models.F


class OrderEvent(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    order = models.ForeignKey(
        "purchasing.PurchaseOrder", on_delete=models.PROTECT, related_name="receiving_events"
    )
    operation = models.OneToOneField("core.Operation", on_delete=models.PROTECT)
    kind = models.CharField(
        max_length=16,
        choices=[
            ("RECEIPT", gettext_lazy("Réception")),
            ("REINSTATE", gettext_lazy("Rétablissement")),
            ("CORRECTION", gettext_lazy("Correction")),
        ],
    )
    revision = models.PositiveBigIntegerField()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["order", "revision"], name="receiving_order_revision"),
            models.CheckConstraint(
                condition=Q(revision__gte=1, kind__in=["RECEIPT", "REINSTATE", "CORRECTION"]),
                name="receiving_order_event_valid",
            ),
        ]


class ArrivalReport(models.Model):
    class Kind(models.TextChoices):
        UNKNOWN = "UNKNOWN_ORDER", gettext_lazy("Commande introuvable")
        LATE = "LATE_CANCELLED_DELIVERY", gettext_lazy("Livraison d’un reliquat annulé")
        SURPLUS = "SURPLUS", gettext_lazy("Arrivée supplémentaire")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    reference = models.CharField(max_length=32, unique=True)
    operation = models.OneToOneField("core.Operation", on_delete=models.PROTECT)
    case = models.OneToOneField(
        "cases.Case", on_delete=models.PROTECT, related_name="arrival_report"
    )
    kind = models.CharField(max_length=24, choices=Kind)
    order = models.ForeignKey("purchasing.PurchaseOrder", null=True, on_delete=models.PROTECT)
    order_version = models.PositiveBigIntegerField(null=True)
    supplier = models.ForeignKey("catalog.Supplier", null=True, on_delete=models.PROTECT)
    supplier_name = models.CharField(max_length=150, blank=True)
    message = models.CharField(max_length=2000, blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(kind="UNKNOWN_ORDER", order__isnull=True)
                | Q(kind__in=["LATE_CANCELLED_DELIVERY", "SURPLUS"], order__isnull=False),
                name="arrival_report_origin",
            )
        ]


class ReportLine(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    report = models.ForeignKey(ArrivalReport, on_delete=models.PROTECT, related_name="lines")
    position = models.PositiveIntegerField()
    order_line = models.ForeignKey("purchasing.OrderLine", null=True, on_delete=models.PROTECT)
    product = models.ForeignKey("catalog.Product", null=True, on_delete=models.PROTECT)
    product_name = models.CharField(max_length=150, blank=True)
    unit = models.CharField(max_length=40, blank=True)
    quantity = models.PositiveBigIntegerField(null=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["report", "position"], name="report_line_position"),
            models.CheckConstraint(
                condition=Q(quantity__isnull=True) | Q(quantity__gte=1, quantity__lte=1000000000),
                name="report_observation_quantity",
            ),
        ]


class UnknownLink(models.Model):
    report = models.OneToOneField(
        ArrivalReport, primary_key=True, on_delete=models.PROTECT, related_name="order_link"
    )
    order = models.ForeignKey("purchasing.PurchaseOrder", on_delete=models.PROTECT)
    operation = models.OneToOneField("core.Operation", on_delete=models.PROTECT)
    verification = models.BooleanField()
    message = models.CharField(max_length=2000, blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(verification=True), name="unknown_link_verified")
        ]


class UnknownLinkLine(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    link = models.ForeignKey(UnknownLink, on_delete=models.PROTECT, related_name="lines")
    order_line = models.ForeignKey("purchasing.OrderLine", on_delete=models.PROTECT)
    quantity = models.PositiveBigIntegerField()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["link", "order_line"], name="unknown_link_order_line"),
            models.CheckConstraint(
                condition=Q(quantity__gte=1, quantity__lte=1000000000), name="unknown_link_quantity"
            ),
        ]


class Receipt(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    reference = models.CharField(max_length=32, unique=True)
    event = models.OneToOneField(OrderEvent, on_delete=models.PROTECT, related_name="receipt")
    source_report = models.ForeignKey(
        ArrivalReport, null=True, on_delete=models.PROTECT, related_name="receipts"
    )
    version = models.PositiveBigIntegerField(default=0)


class ReceiptLine(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    receipt = models.ForeignKey(Receipt, on_delete=models.PROTECT, related_name="lines")
    order_line = models.ForeignKey(
        "purchasing.OrderLine", on_delete=models.PROTECT, related_name="receipt_lines"
    )
    conforming = models.PositiveBigIntegerField()
    nonconforming = models.PositiveBigIntegerField()
    regular = models.PositiveBigIntegerField()
    surplus = models.PositiveBigIntegerField()
    nonconformity = models.CharField(max_length=16, blank=True)
    waiting_before = models.PositiveBigIntegerField()
    entry = models.OneToOneField("inventory.StockEntry", null=True, on_delete=models.PROTECT)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["receipt", "order_line"], name="receipt_order_line"),
            models.CheckConstraint(
                condition=Q(
                    conforming=F("regular") + F("surplus"),
                    regular__lte=F("waiting_before"),
                    conforming__lte=1000000000,
                    nonconforming__lte=1000000000,
                )
                & (
                    Q(nonconforming=0, nonconformity="")
                    | Q(nonconforming__gt=0, nonconformity__in=["DAMAGED", "INCORRECT", "UNUSABLE"])
                ),
                name="receipt_line_quantities",
            ),
        ]


class QuarantineItem(models.Model):
    class Kind(models.TextChoices):
        SURPLUS = "SURPLUS", gettext_lazy("Surplus à part")
        NONCONFORMING = "NONCONFORMING", gettext_lazy("Non conforme")
        UNKNOWN = "UNKNOWN_ORDER", gettext_lazy("Commande régularisée")
        LATE = "LATE_CANCELLED", gettext_lazy("Reliquat annulé livré")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    case = models.ForeignKey(
        "cases.Case", on_delete=models.PROTECT, related_name="quarantine_items"
    )
    creation_operation = models.ForeignKey("core.Operation", on_delete=models.PROTECT)
    kind = models.CharField(max_length=20, choices=Kind)
    receipt_line = models.ForeignKey(
        ReceiptLine, null=True, on_delete=models.PROTECT, related_name="quarantine_items"
    )
    report_line = models.OneToOneField(
        ReportLine, null=True, on_delete=models.PROTECT, related_name="quarantine_item"
    )
    link_line = models.OneToOneField(
        UnknownLinkLine, null=True, on_delete=models.PROTECT, related_name="quarantine_item"
    )
    quantity = models.PositiveBigIntegerField()

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(
                    receipt_line__isnull=False,
                    report_line__isnull=True,
                    link_line__isnull=True,
                    kind__in=["SURPLUS", "NONCONFORMING"],
                )
                | Q(
                    receipt_line__isnull=True,
                    report_line__isnull=False,
                    link_line__isnull=True,
                    kind__in=["SURPLUS", "LATE_CANCELLED"],
                )
                | Q(
                    receipt_line__isnull=True,
                    report_line__isnull=True,
                    link_line__isnull=False,
                    kind="UNKNOWN_ORDER",
                ),
                name="quarantine_typed_origin",
            ),
            models.UniqueConstraint(
                fields=["receipt_line", "kind"], name="quarantine_receipt_kind"
            ),
        ]


class SourceConsumption(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    line = models.OneToOneField(
        ReceiptLine, on_delete=models.PROTECT, related_name="source_consumption"
    )
    item = models.ForeignKey(QuarantineItem, on_delete=models.PROTECT, related_name="consumptions")
    quantity = models.PositiveBigIntegerField()
    reinstated_quantity = models.PositiveBigIntegerField(default=0)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(quantity__gte=1, quantity__lte=1000000000),
                name="source_consumption_quantity",
            )
        ]


class QuarantineDecision(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    operation = models.ForeignKey("core.Operation", on_delete=models.PROTECT)
    item = models.ForeignKey(
        QuarantineItem, null=True, on_delete=models.PROTECT, related_name="decisions"
    )
    report = models.OneToOneField(
        ArrivalReport, null=True, on_delete=models.PROTECT, related_name="external_resolution"
    )
    kind = models.CharField(
        max_length=16,
        choices=[
            ("ACCEPT", gettext_lazy("Accepter")),
            ("REFUSE", gettext_lazy("Refuser")),
            ("EXTERNAL", gettext_lazy("Traitement externe vérifié")),
        ],
    )
    quantity = models.PositiveBigIntegerField(null=True)
    remaining_before = models.PositiveBigIntegerField(null=True)
    version_before = models.PositiveBigIntegerField()
    grant_released = models.PositiveBigIntegerField(default=0)
    verification = models.BooleanField()
    message = models.CharField(max_length=2000, blank=True)
    entry = models.OneToOneField("inventory.StockEntry", null=True, on_delete=models.PROTECT)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["operation", "item"], name="quarantine_operation_item"),
            models.CheckConstraint(
                condition=Q(verification=True)
                & (
                    Q(
                        item__isnull=False,
                        report__isnull=True,
                        quantity__gte=1,
                        quantity=F("remaining_before"),
                        kind__in=["ACCEPT", "REFUSE", "EXTERNAL"],
                    )
                    | Q(
                        item__isnull=True,
                        report__isnull=False,
                        quantity__isnull=True,
                        remaining_before__isnull=True,
                        kind="EXTERNAL",
                        entry__isnull=True,
                    )
                ),
                name="quarantine_full_balance_decision",
            ),
        ]


class Reinstatement(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    event = models.OneToOneField(OrderEvent, on_delete=models.PROTECT, related_name="reinstatement")
    report = models.ForeignKey(
        ArrivalReport, on_delete=models.PROTECT, related_name="reinstatements"
    )
    external_verification = models.BooleanField()

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(external_verification=True), name="reinstatement_verified"
            )
        ]


class ReinstatementLine(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    reinstatement = models.ForeignKey(Reinstatement, on_delete=models.PROTECT, related_name="lines")
    line = models.ForeignKey(
        "purchasing.OrderLine", on_delete=models.PROTECT, related_name="reinstatements"
    )
    item = models.ForeignKey(
        QuarantineItem, on_delete=models.PROTECT, related_name="reinstated_lines"
    )
    quantity = models.PositiveBigIntegerField()
    cancelled_before = models.PositiveBigIntegerField()
    cancelled_after = models.PositiveBigIntegerField()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["reinstatement", "line"], name="reinstatement_order_line"
            ),
            models.CheckConstraint(
                condition=Q(quantity__gte=1, cancelled_before=F("cancelled_after") + F("quantity")),
                name="reinstatement_line_delta",
            ),
        ]


class ReceiptCorrection(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    event = models.OneToOneField(
        OrderEvent, on_delete=models.PROTECT, related_name="receipt_correction"
    )
    receipt = models.ForeignKey(Receipt, on_delete=models.PROTECT, related_name="corrections")
    case = models.ForeignKey("cases.Case", on_delete=models.PROTECT)
    previous = models.OneToOneField(
        "self", null=True, on_delete=models.PROTECT, related_name="next_correction"
    )
    revision = models.PositiveBigIntegerField()
    verification = models.BooleanField()
    message = models.CharField(max_length=2000)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["receipt", "revision"], name="receipt_correction_revision"
            ),
            models.CheckConstraint(
                condition=Q(revision__gte=1, verification=True), name="receipt_correction_verified"
            ),
        ]


class ReceiptCorrectionLine(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    correction = models.ForeignKey(
        ReceiptCorrection, on_delete=models.PROTECT, related_name="lines"
    )
    line = models.ForeignKey(ReceiptLine, on_delete=models.PROTECT, related_name="corrections")
    conforming_before = models.PositiveBigIntegerField()
    conforming_after = models.PositiveBigIntegerField()
    nonconforming_before = models.PositiveBigIntegerField()
    nonconforming_after = models.PositiveBigIntegerField()
    regular_before = models.PositiveBigIntegerField()
    regular_after = models.PositiveBigIntegerField()
    surplus_before = models.PositiveBigIntegerField()
    surplus_after = models.PositiveBigIntegerField()
    nonconformity_before = models.CharField(max_length=16, blank=True)
    nonconformity_after = models.CharField(max_length=16, blank=True)
    source_grant_before = models.PositiveBigIntegerField(default=0)
    source_grant_after = models.PositiveBigIntegerField(default=0)
    entry = models.OneToOneField("inventory.StockEntry", null=True, on_delete=models.PROTECT)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["correction", "line"], name="receipt_correction_line"),
            models.CheckConstraint(
                condition=Q(
                    conforming_before=F("regular_before") + F("surplus_before"),
                    conforming_after=F("regular_after") + F("surplus_after"),
                    conforming_after__lte=1000000000,
                    nonconforming_after__lte=1000000000,
                )
                & (
                    Q(nonconforming_after=0, nonconformity_after="")
                    | Q(
                        nonconforming_after__gt=0,
                        nonconformity_after__in=["DAMAGED", "INCORRECT", "UNUSABLE"],
                    )
                ),
                name="receipt_correction_line_quantities",
            ),
        ]


class ManualReminder(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    operation = models.OneToOneField("core.Operation", on_delete=models.PROTECT)
    order = models.ForeignKey(
        "purchasing.PurchaseOrder", on_delete=models.PROTECT, related_name="reminders"
    )
    previous = models.OneToOneField(
        "self", null=True, on_delete=models.PROTECT, related_name="next_reminder"
    )
