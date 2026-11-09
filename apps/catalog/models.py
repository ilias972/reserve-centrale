"""Référentiels M2. Aucune quantité physique ni réservation n'est éditable ici."""

import uuid

from django.conf import settings
from django.db import models
from django.db.models.functions import Lower, Trim
from django.utils import timezone
from django.utils.translation import gettext_lazy


class Reference(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    version = models.PositiveBigIntegerField(default=0)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(default=timezone.now)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, related_name="+"
    )
    last_operation = models.ForeignKey(
        "core.Operation", on_delete=models.PROTECT, null=True, related_name="+"
    )

    class Meta:
        abstract = True


class Floor(Reference):
    code = models.CharField(max_length=32, unique=True)
    name = models.CharField(max_length=120)
    active = models.BooleanField(default=True)

    class Meta:
        constraints = [models.UniqueConstraint(Lower(Trim("code")), name="floor_code_normalized")]

    def __str__(self):
        return f"{self.code} — {self.name}"


class Category(Reference):
    name = models.CharField(max_length=120)
    active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(Lower(Trim("name")), name="category_name_normalized")
        ]

    def __str__(self):
        return self.name


class Supplier(Reference):
    code = models.CharField(max_length=32, unique=True)
    name = models.CharField(max_length=150)
    website = models.URLField(max_length=500, blank=True)
    active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(Lower(Trim("code")), name="supplier_code_normalized")
        ]

    def __str__(self):
        return f"{self.code} — {self.name}"


class Product(Reference):
    class Status(models.TextChoices):
        NOT_COUNTED = "NOT_COUNTED", gettext_lazy("Non inventorié")
        COUNTED_PENDING_ACTIVATION = (
            "COUNTED_PENDING_ACTIVATION",
            gettext_lazy("En attente d'activation"),
        )
        ACTIVE = "ACTIVE", gettext_lazy("Actif")
        ARCHIVED = "ARCHIVED", gettext_lazy("Archivé")

    code = models.CharField(max_length=64, unique=True)
    name = models.CharField(max_length=150)
    category = models.ForeignKey(Category, on_delete=models.PROTECT, null=True, blank=True)
    unit = models.CharField(max_length=40)
    minimum = models.PositiveBigIntegerField()
    target = models.PositiveBigIntegerField()
    status = models.CharField(max_length=32, choices=Status, default=Status.NOT_COUNTED)
    current_photo = models.ForeignKey(
        "ProductPhoto", on_delete=models.PROTECT, null=True, related_name="+"
    )
    # Contrat de raccordement M3 : métadonnées seules, sans compteur P/R.
    # Le runtime M2 ne peut modifier ces colonnes. Aucun endpoint d'inventaire M2.
    counted_at = models.DateTimeField(null=True)
    initial_inventory_operation = models.ForeignKey(
        "core.Operation", on_delete=models.PROTECT, null=True, related_name="+"
    )
    activation_operation = models.ForeignKey(
        "core.Operation", on_delete=models.PROTECT, null=True, related_name="+"
    )

    class Meta:
        indexes = [models.Index(fields=["status", "name", "id"])]
        constraints = [
            models.UniqueConstraint(Lower(Trim("code")), name="product_code_normalized"),
            models.CheckConstraint(
                condition=models.Q(
                    target__gte=models.F("minimum"), minimum__lte=1000000000, target__lte=1000000000
                ),
                name="product_thresholds",
            ),
            models.CheckConstraint(condition=~models.Q(unit=""), name="product_unit_not_empty"),
            models.CheckConstraint(
                condition=(
                    models.Q(counted_at__isnull=True, initial_inventory_operation__isnull=True)
                    | models.Q(counted_at__isnull=False, initial_inventory_operation__isnull=False)
                ),
                name="product_inventory_anchor",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(status__in=["NOT_COUNTED", "ARCHIVED"])
                    | models.Q(
                        status__in=["COUNTED_PENDING_ACTIVATION", "ACTIVE"],
                        counted_at__isnull=False,
                    )
                ),
                name="product_status_inventory",
            ),
            models.CheckConstraint(
                condition=(
                    ~models.Q(status="ACTIVE")
                    | models.Q(
                        current_photo__isnull=False,
                        activation_operation__isnull=False,
                        counted_at__isnull=False,
                    )
                ),
                name="product_active_requirements",
            ),
            models.CheckConstraint(
                condition=(~models.Q(status="NOT_COUNTED") | models.Q(counted_at__isnull=True)),
                name="product_not_counted_state",
            ),
        ]

    def __str__(self):
        return f"{self.code} — {self.name}"


class FloorProduct(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    floor = models.ForeignKey(Floor, on_delete=models.PROTECT)
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="floor_permissions")
    visible = models.BooleanField(default=False)
    demandable = models.BooleanField(default=False)
    maximum = models.PositiveBigIntegerField(null=True)

    class Meta:
        indexes = [models.Index(fields=["floor", "visible", "product"])]
        constraints = [
            models.UniqueConstraint(fields=["floor", "product"], name="floor_product_pair"),
            models.CheckConstraint(
                condition=(
                    models.Q(demandable=False)
                    | models.Q(visible=True, maximum__isnull=False, maximum__gt=0)
                ),
                name="floor_product_requires_ceiling",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(maximum__isnull=True)
                    | models.Q(maximum__gt=0, maximum__lte=1000000000)
                ),
                name="floor_product_ceiling_range",
            ),
        ]


class ProductSupplier(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="supplier_links")
    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT)
    active = models.BooleanField(default=True)
    preferred = models.BooleanField(default=False)
    reference = models.CharField(max_length=150, blank=True)
    website = models.URLField(max_length=500, blank=True)
    indicative_price = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    currency = models.CharField(max_length=3, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["product", "supplier"], name="product_supplier_pair"),
            models.UniqueConstraint(
                fields=["product"],
                condition=models.Q(preferred=True),
                name="one_preferred_supplier",
            ),
            models.CheckConstraint(
                condition=(models.Q(preferred=False) | models.Q(active=True)),
                name="preferred_link_active",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(indicative_price__isnull=True, currency="")
                    | (
                        models.Q(indicative_price__isnull=False, indicative_price__gte=0)
                        & ~models.Q(currency="")
                    )
                ),
                name="supplier_price_currency",
            ),
        ]


class PhotoAsset(models.Model):
    """Téléversement technique privé ; jamais visible sans ProductPhoto confirmé."""

    id = models.UUIDField(primary_key=True, editable=False)
    intent = models.OneToOneField("core.OperationIntent", on_delete=models.PROTECT)
    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    uploader = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(default=timezone.now)
    source_digest = models.CharField(max_length=64)
    digest = models.CharField(max_length=64)
    object_key = models.CharField(max_length=200, unique=True)
    thumbnail_key = models.CharField(max_length=200, unique=True)
    width = models.PositiveIntegerField()
    height = models.PositiveIntegerField()
    size = models.PositiveIntegerField()
    thumbnail_size = models.PositiveIntegerField()


class ProductPhoto(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="photos")
    asset = models.OneToOneField(PhotoAsset, on_delete=models.PROTECT)
    replaces = models.ForeignKey("self", on_delete=models.PROTECT, null=True)
    operation = models.OneToOneField("core.Operation", on_delete=models.PROTECT)
    created_at = models.DateTimeField(default=timezone.now)
