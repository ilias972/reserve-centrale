"""Soldes reconstruisibles et faits immuables ; aucune quantité d'unité."""

import uuid

from django.db import models
from django.utils.translation import gettext_lazy


class StockBalance(models.Model):
    product = models.OneToOneField(
        "catalog.Product", primary_key=True, on_delete=models.PROTECT, related_name="stock"
    )
    physical = models.BigIntegerField(null=True)
    reserved = models.BigIntegerField(default=0)
    version = models.PositiveBigIntegerField(default=0)
    latest_entry = models.OneToOneField(
        "StockEntry", null=True, on_delete=models.PROTECT, related_name="+"
    )

    @property
    def available(self):
        return None if self.physical is None else self.physical - self.reserved

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(
                        physical__isnull=True, reserved=0, version=0, latest_entry__isnull=True
                    )
                    | models.Q(
                        physical__isnull=False,
                        physical__gte=0,
                        physical__lte=1000000000,
                        reserved__gte=0,
                        reserved__lte=models.F("physical"),
                        version__gte=1,
                        latest_entry__isnull=False,
                    )
                ),
                name="stock_balance_invariants",
            )
        ]


class StockEntry(models.Model):
    class Kind(models.TextChoices):
        INITIAL_COUNT = "INITIAL_COUNT", gettext_lazy("Inventaire initial")
        MANUAL_ADJUSTMENT = "MANUAL_ADJUSTMENT", gettext_lazy("Ajustement ordinaire")
        RESERVE = "RESERVE", gettext_lazy("Réservation")
        RELEASE = "RELEASE", gettext_lazy("Libération")
        DELIVER = "DELIVER", gettext_lazy("Remise interne")
        DELIVERY_CORRECTION = (
            "DELIVERY_CORRECTION",
            gettext_lazy("Correction de remise par la gestionnaire"),
        )
        ADMIN_RECONCILIATION = (
            "ADMIN_RECONCILIATION",
            gettext_lazy("Comptage régularisé par la gestionnaire"),
        )
        SHORTAGE = "SHORTAGE", gettext_lazy("Écart de stock lié au manque")
        RECEIPT_REGULAR = "RECEIPT_REGULAR", gettext_lazy("Réception fournisseur conforme")
        SURPLUS_ACCEPT = "SURPLUS_ACCEPT", gettext_lazy("Surplus accepté par la responsable")
        RECEIPT_CORRECTION = "RECEIPT_CORRECTION", gettext_lazy("Correction de réception")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    operation = models.ForeignKey(
        "core.Operation", on_delete=models.PROTECT, related_name="stock_entries"
    )
    product = models.ForeignKey(
        "catalog.Product", on_delete=models.PROTECT, related_name="stock_entries"
    )
    sequence = models.PositiveIntegerField()
    kind = models.CharField(max_length=24, choices=Kind)
    physical_before = models.BigIntegerField(null=True)
    reserved_before = models.BigIntegerField()
    physical_after = models.BigIntegerField()
    reserved_after = models.BigIntegerField()
    delta_p = models.BigIntegerField()
    delta_r = models.BigIntegerField()
    version_before = models.PositiveBigIntegerField()
    version_after = models.PositiveBigIntegerField()
    unit = models.CharField(max_length=40)
    product_code = models.CharField(max_length=64)
    product_name = models.CharField(max_length=150)
    photo = models.ForeignKey("catalog.ProductPhoto", on_delete=models.PROTECT, null=True)

    class Meta:
        indexes = [models.Index(fields=["product", "version_after"])]
        constraints = [
            models.UniqueConstraint(
                fields=["operation", "sequence"], name="stock_operation_sequence"
            ),
            models.UniqueConstraint(
                fields=["product", "version_after"], name="stock_product_version"
            ),
            models.CheckConstraint(
                condition=models.Q(
                    physical_after__gte=0,
                    physical_after__lte=1000000000,
                    reserved_after__gte=0,
                    reserved_after__lte=models.F("physical_after"),
                    reserved_before__gte=0,
                ),
                name="stock_entry_quantities",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    version_after=models.F("version_before") + 1,
                    delta_r=models.F("reserved_after") - models.F("reserved_before"),
                ),
                name="stock_entry_version_delta_r",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        kind="INITIAL_COUNT",
                        physical_before__isnull=True,
                        reserved_before=0,
                        reserved_after=0,
                        delta_r=0,
                        delta_p=models.F("physical_after"),
                        version_before=0,
                    )
                    | models.Q(
                        kind__in=[
                            "MANUAL_ADJUSTMENT",
                            "RESERVE",
                            "RELEASE",
                            "DELIVER",
                            "SHORTAGE",
                            "DELIVERY_CORRECTION",
                            "ADMIN_RECONCILIATION",
                            "RECEIPT_REGULAR",
                            "SURPLUS_ACCEPT",
                            "RECEIPT_CORRECTION",
                        ],
                        physical_before__isnull=False,
                        physical_before__gte=models.F("reserved_before"),
                        delta_p=models.F("physical_after") - models.F("physical_before"),
                    )
                ),
                name="stock_entry_delta_p",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(kind="INITIAL_COUNT")
                    | (models.Q(kind="MANUAL_ADJUSTMENT", delta_r=0) & ~models.Q(delta_p=0))
                    | models.Q(kind="RESERVE", delta_p=0, delta_r__gt=0)
                    | models.Q(kind="RELEASE", delta_p=0, delta_r__lt=0)
                    | models.Q(kind="DELIVER", delta_p=models.F("delta_r"), delta_r__lt=0)
                    | models.Q(kind="SHORTAGE", delta_r=0, delta_p__lt=0)
                    | models.Q(kind="DELIVERY_CORRECTION")
                    | (models.Q(kind="ADMIN_RECONCILIATION", delta_r=0) & ~models.Q(delta_p=0))
                    | models.Q(
                        kind__in=["RECEIPT_REGULAR", "SURPLUS_ACCEPT"], delta_p__gt=0, delta_r=0
                    )
                    | (models.Q(kind="RECEIPT_CORRECTION", delta_r=0) & ~models.Q(delta_p=0))
                ),
                name="stock_entry_kind",
            ),
        ]


class InventoryBatch(models.Model):
    class Kind(models.TextChoices):
        INITIAL = "INITIAL", gettext_lazy("Inventaire initial")
        DAILY = "DAILY", gettext_lazy("Ajustement ordinaire")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    operation = models.OneToOneField("core.Operation", on_delete=models.PROTECT)
    kind = models.CharField(max_length=12, choices=Kind)


class InventoryLine(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    batch = models.ForeignKey(InventoryBatch, on_delete=models.PROTECT, related_name="lines")
    product = models.ForeignKey("catalog.Product", on_delete=models.PROTECT)
    base_version = models.PositiveBigIntegerField()
    target_physical = models.PositiveBigIntegerField()
    entry = models.OneToOneField(StockEntry, on_delete=models.PROTECT)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["batch", "product"], name="inventory_batch_product")
        ]
