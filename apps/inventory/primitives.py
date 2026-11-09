"""Unique voie interne P/R. M4 devra appeler ces primitives sous les mêmes verrous."""

from dataclasses import dataclass

from django.db import connection
from django.utils.translation import gettext

from apps.catalog.models import Product
from apps.core.errors import BusinessError

from .models import StockBalance, StockEntry


@dataclass
class LockedStock:
    product: Product
    balance: StockBalance


def lock_stocks(identifiers):
    if not connection.in_atomic_block:
        raise RuntimeError("Une transaction est requise avant le verrouillage des stocks.")
    products = list(Product.objects.select_for_update().filter(pk__in=identifiers).order_by("pk"))
    if len(products) != len(identifiers):
        raise BusinessError(
            "NOT_FOUND", gettext("Un produit n'existe plus dans ce lot."), status=404
        )
    balances = {
        row.product_id: row
        for row in StockBalance.objects.select_for_update()
        .filter(product_id__in=identifiers)
        .order_by("product_id")
    }
    if len(balances) != len(products):
        raise RuntimeError("Projection de stock absente : contrôle technique nécessaire.")
    return {product.pk: LockedStock(product, balances[product.pk]) for product in products}


def append_change(execution, stock, *, kind, physical, delta_r=0, sequence):
    """Les appelants valident permissions/version et verrouillent TOUT le lot avant appel.
    Le SQL vérifie indépendamment chaînage, sommes, provenance, audit et immuabilité.
    """
    if not connection.in_atomic_block or not isinstance(stock, LockedStock):
        raise RuntimeError("Stock verrouillé dans une transaction obligatoire.")
    product, balance = stock.product, stock.balance
    if type(physical) is not int or not 0 <= physical <= 1000000000 or type(delta_r) is not int:
        raise BusinessError("INVALID_QUANTITY", gettext("Quantité entière invalide."), status=400)
    from apps.cases.models import StockHold

    held = StockHold.objects.filter(product=product).exists()
    reconciliation = (
        kind == "ADMIN_RECONCILIATION"
        and execution.actor.role == "ADMIN"
        and execution.intent.command_type == "stock_reconciliations.confirm"
    )
    if (
        held
        and not reconciliation
        and (delta_r > 0 or (balance.physical is not None and physical < balance.physical))
    ):
        raise BusinessError(
            "PRODUCT_ON_HOLD",
            gettext("Produit en vérification : nouvelle réservation ou diminution bloquée."),
        )
    reserved = balance.reserved + delta_r
    if not 0 <= reserved <= physical:
        raise BusinessError(
            "STOCK_BELOW_RESERVED",
            gettext("La quantité physique ne peut pas être inférieure aux réservations."),
        )
    if kind == StockEntry.Kind.RESERVE and (
        not product.counted_at or product.status != Product.Status.ACTIVE
    ):
        raise BusinessError(
            "PRODUCT_UNAVAILABLE",
            gettext("Le produit doit être inventorié et actif pour une nouvelle réservation."),
        )
    entry = StockEntry.objects.create(
        operation_id=execution.operation_id,
        product=product,
        sequence=sequence,
        kind=kind,
        physical_before=balance.physical,
        reserved_before=balance.reserved,
        physical_after=physical,
        reserved_after=reserved,
        delta_p=physical - (balance.physical or 0),
        delta_r=delta_r,
        version_before=balance.version,
        version_after=balance.version + 1,
        unit=product.unit,
        product_code=product.code,
        product_name=product.name,
        photo_id=product.current_photo_id,
    )
    balance.physical = physical
    balance.reserved = reserved
    balance.version += 1
    balance.latest_entry = entry
    balance.save(update_fields=["physical", "reserved", "version", "latest_entry"])
    return entry
