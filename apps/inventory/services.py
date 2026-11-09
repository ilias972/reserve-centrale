from django.utils import timezone
from django.utils.translation import gettext

from apps.accounts.models import User
from apps.catalog.models import Product
from apps.core import commands
from apps.core.commands import Command, Effect
from apps.core.errors import BusinessError
from apps.core.models import Operation
from apps.core.payloads import invalid_input, parse_uuid

from .models import InventoryBatch, InventoryLine
from .primitives import append_change, lock_stocks

MAX_BATCH = 100


def parsed_lines(payload, *, initial=False):
    if not isinstance(payload, dict) or set(payload) != {"lines"}:
        raise invalid_input()
    rows = payload["lines"]
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_BATCH:
        raise BusinessError(
            "INVALID_BATCH", gettext("Un lot doit contenir de 1 à 100 produits."), status=400
        )
    seen = set()
    result = []
    target = "counted_quantity" if initial else "target_physical"
    allowed = {"product_id", "expected_stock_version", target}
    if initial:
        allowed.add("expected_product_version")
    for row in rows:
        if not isinstance(row, dict) or set(row) != allowed:
            raise invalid_input()
        identifier = parse_uuid(row["product_id"])
        if identifier in seen:
            raise BusinessError(
                "DUPLICATE_PRODUCT",
                gettext("Un produit ne peut apparaître deux fois dans un lot."),
                status=400,
            )
        seen.add(identifier)
        for key in allowed - {"product_id"}:
            if (
                type(row[key]) is not int
                or row[key] < 0
                or (key == target and row[key] > 1000000000)
            ):
                raise BusinessError(
                    "INVALID_QUANTITY", gettext("Quantité ou version entière invalide."), status=400
                )
        result.append({**row, "product_id": identifier})
    return result


def batch_scope(actor, identifier):
    if identifier is not None:
        raise invalid_input()


def lock_batch(initial):
    def lock(actor, identifier, payload):
        rows = parsed_lines(payload, initial=initial)
        return rows, lock_stocks({row["product_id"] for row in rows})

    return lock


def stock_snapshot(stock):
    product, balance = stock.product, stock.balance
    return {
        "product_id": str(product.pk),
        "code": product.code,
        "name": product.name,
        "unit": product.unit,
        "physical": balance.physical,
        "reserved": balance.reserved,
        "available": balance.available,
        "stock_version": balance.version,
        "product_version": product.version,
        "status": product.status,
    }


def apply_batch(initial):
    def apply(execution, resources, payload):
        rows, stocks = resources
        before = [stock_snapshot(stocks[row["product_id"]]) for row in rows]
        shortages = []
        held_decreases = []
        # Toutes les validations décisives du lot avant la première écriture.
        for row in rows:
            stock = stocks[row["product_id"]]
            if initial:
                if stock.product.counted_at or stock.balance.physical is not None:
                    raise BusinessError(
                        "ALREADY_COUNTED",
                        gettext("Un inventaire initial existe déjà pour ce produit."),
                    )
                if stock.product.version != row["expected_product_version"]:
                    raise BusinessError(
                        "STALE_PRODUCT_VERSION",
                        gettext(
                            "La fiche ou l'unité a changé. Vérifiez le comptage avant confirmation."
                        ),
                    )
            if stock.balance.version != row["expected_stock_version"]:
                raise BusinessError(
                    "STALE_VERSION",
                    gettext(
                        "Les quantités ont changé. Recontrôlez le lot avant une nouvelle confirmation."
                    ),
                    details={"current": before},
                )
            if not initial:
                if stock.balance.physical is None:
                    raise BusinessError(
                        "NOT_COUNTED",
                        gettext(
                            "Un produit non inventorié nécessite d'abord son inventaire initial."
                        ),
                    )
                from apps.cases.models import StockHold

                held = StockHold.objects.filter(product=stock.product).exists()
                if held and row["target_physical"] < stock.balance.physical:
                    held_decreases.append(str(stock.product.pk))
                if not held and row["target_physical"] < stock.balance.reserved:
                    shortages.append(
                        {
                            "product_id": str(stock.product.pk),
                            "physical": stock.balance.physical,
                            "reserved": stock.balance.reserved,
                            "attempted_physical": row["target_physical"],
                            "stock_version": stock.balance.version,
                        }
                    )
        if shortages:
            raise BusinessError(
                "STOCK_BELOW_RESERVED",
                gettext(
                    "Aucune mise à jour du lot enregistrée : quantité inférieure aux réservations. Signalement transmis à la gestionnaire."
                ),
                details={"products": shortages},
            )
        if held_decreases:
            raise BusinessError(
                "PRODUCT_ON_HOLD",
                gettext(
                    "Ce produit est en vérification par la gestionnaire. Aucune diminution du lot n'est enregistrée."
                ),
                details={"products": held_decreases},
            )
        if not initial and all(
            row["target_physical"] == stocks[row["product_id"]].balance.physical for row in rows
        ):
            raise BusinessError("NO_CHANGES", gettext("Aucune quantité n'a été modifiée."))
        batch = InventoryBatch.objects.create(
            operation_id=execution.operation_id, kind="INITIAL" if initial else "DAILY"
        )
        operation = Operation.objects.get(pk=execution.operation_id)
        details = []
        for sequence, row in enumerate(rows, 1):
            stock = stocks[row["product_id"]]
            quantity = row["counted_quantity" if initial else "target_physical"]
            if not initial and quantity == stock.balance.physical:
                continue
            entry = append_change(
                execution,
                stock,
                kind="INITIAL_COUNT" if initial else "MANUAL_ADJUSTMENT",
                physical=quantity,
                sequence=sequence,
            )
            InventoryLine.objects.create(
                batch=batch,
                product=stock.product,
                base_version=row["expected_stock_version"],
                target_physical=quantity,
                entry=entry,
            )
            if initial:
                product = stock.product
                product.counted_at = operation.confirmed_at
                product.initial_inventory_operation_id = execution.operation_id
                if product.status != Product.Status.ARCHIVED:
                    product.status = Product.Status.COUNTED_PENDING_ACTIVATION
                product.version += 1
                product.updated_at = timezone.now()
                product.last_operation_id = execution.operation_id
                product.save(
                    update_fields=[
                        "counted_at",
                        "initial_inventory_operation",
                        "status",
                        "version",
                        "updated_at",
                        "last_operation",
                    ]
                )
            details.append(
                {
                    "product_id": str(stock.product.pk),
                    "unit": stock.product.unit,
                    "counted_quantity": quantity if initial else None,
                    "entry_id": str(entry.pk),
                    "kind": entry.kind,
                    "physical_before": entry.physical_before,
                    "physical_after": entry.physical_after,
                    "reserved_before": entry.reserved_before,
                    "reserved_after": entry.reserved_after,
                    "delta_p": entry.delta_p,
                    "delta_r": entry.delta_r,
                }
            )
        after = [stock_snapshot(stocks[row["product_id"]]) for row in rows]
        return Effect(
            {
                "batch_id": str(batch.pk),
                "confirmed_at": operation.confirmed_at.isoformat(),
                "lines": after,
            },
            before={"lines": before},
            after={"lines": after},
            details=details,
            http_status=201 if initial else 200,
        )

    return apply


def record_rejection(execution, resources, error):
    if error.code != "STOCK_BELOW_RESERVED":
        return {}
    from apps.cases.services import open_stock_case

    return open_stock_case(execution, resources[1], error.details["products"])


COMMANDS = [
    Command(
        "inventory.initial",
        frozenset({User.Role.REPLENISHER}),
        batch_scope,
        lambda actor, identifier: None,
        apply_batch(True),
        lock_payload_resources=lock_batch(True),
    ),
    Command(
        "inventory.adjust",
        frozenset({User.Role.REPLENISHER}),
        batch_scope,
        lambda actor, identifier: None,
        apply_batch(False),
        lock_payload_resources=lock_batch(False),
        record_rejection=record_rejection,
    ),
]


def ensure_registered():
    for command in COMMANDS:
        if command.name not in commands._registry:
            commands.register_command(command)
