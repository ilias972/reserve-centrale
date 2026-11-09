from django.db.models import Q
from django.utils.translation import gettext

from apps.accounts.models import User
from apps.audit.models import AuditEvent
from apps.core.errors import BusinessError
from apps.core.payloads import parse_uuid
from apps.core.search import matching

from .models import FloorProduct, Product
from .policies import visible_products


def permission_data(row):
    return {
        "floor_id": str(row.floor_id),
        "visible": row.visible,
        "demandable": row.demandable,
        "maximum": row.maximum,
    }


def supplier_link_data(row):
    return {
        "supplier_id": str(row.supplier_id),
        "active": row.active,
        "preferred": row.preferred,
        "reference": row.reference,
        "website": row.website,
        "indicative_price": str(row.indicative_price) if row.indicative_price is not None else None,
        "currency": row.currency,
    }


def permission_snapshot(row):
    return {**permission_data(row), "floor_code": row.floor.code, "floor_name": row.floor.name}


def supplier_link_snapshot(row):
    return {
        **supplier_link_data(row),
        "supplier_code": row.supplier.code,
        "supplier_name": row.supplier.name,
    }


def product_data(product, actor=None):
    admin = actor is None or actor.role == User.Role.ADMIN
    result = {
        "id": str(product.pk),
        "code": product.code,
        "name": product.name,
        "category_id": str(product.category_id) if product.category_id else None,
        "category_name": product.category.name if product.category_id else None,
        "unit": product.unit,
        "status": product.status,
        "status_label": product.get_status_display(),
        "photo_id": str(product.current_photo_id) if product.current_photo_id else None,
        "photo_url": f"/api/v1/products/{product.pk}/photo" if product.current_photo_id else None,
        "unit_locked": product.counted_at is not None,
        "inventory_confirmed": product.counted_at is not None,
        "stock_module_available": True,
        "physical_quantity": None,
        "available_quantity": None,
        "demandable": False,
        "request_module_available": True,
    }
    result["available_quantity"] = product.stock.available
    if actor is None or actor.role in {User.Role.ADMIN, User.Role.REPLENISHER}:
        result["physical_quantity"] = product.stock.physical
        result["reserved_quantity"] = product.stock.reserved
    if product.initial_inventory_operation_id:
        result["initial_inventory_actor"] = (
            product.initial_inventory_operation.actor.display_name if admin else None
        )
    if admin:
        result.update(
            minimum=product.minimum,
            target=product.target,
            version=product.version,
            counted_at=product.counted_at.isoformat() if product.counted_at else None,
            supplier_configured=product._supplier_configured
            if hasattr(product, "_supplier_configured")
            else product.supplier_links.filter(active=True, supplier__active=True).exists(),
        )
    elif actor.is_requester:
        if hasattr(product, "_floor_maximum"):
            maximum = product._floor_maximum
            allowed = product._floor_demandable
        else:
            row = FloorProduct.objects.filter(
                product=product, floor_id=actor.floor_id, visible=True
            ).first()
            maximum = row.maximum if row else None
            allowed = bool(row and row.demandable)
        result["maximum"] = maximum
        result["permission_allows_request"] = bool(allowed and maximum and actor.floor.active)
        held = getattr(product, "_on_hold", None)
        if held is None:
            from apps.cases.models import StockHold

            held = StockHold.objects.filter(product=product).exists()
        usable = bool(
            result["permission_allows_request"]
            and product.status == "ACTIVE"
            and product.counted_at
            and not held
        )
        result["maximum_allowed"] = min(product.stock.available or 0, maximum or 0) if usable else 0
        result["demandable"] = bool(result["maximum_allowed"])
        result["request_reason"] = (
            ""
            if result["demandable"]
            else (
                "Demande non autorisée ou plafond absent."
                if not result["permission_allows_request"]
                else "Stock en vérification."
                if held
                else "Non inventorié — quantité inconnue."
                if not product.counted_at
                else "Produit en attente d'activation."
                if product.status != "ACTIVE"
                else "Indisponible actuellement."
            )
        )
    return result


def get_product(actor, identifier):
    product = visible_products(actor).filter(pk=identifier).first()
    if product is None:
        raise BusinessError("NOT_FOUND", gettext("Produit introuvable."), status=404)
    return product


def catalog_list(actor, params):
    query = matching(visible_products(actor), params.get("q", ""), ("name", "code"))
    if params.get("category"):
        query = query.filter(category_id=parse_uuid(params["category"]))
    if params.get("status"):
        if params["status"] not in Product.Status.values:
            raise BusinessError("INVALID_INPUT", gettext("Statut invalide."), status=400)
        query = query.filter(status=params["status"])
    try:
        offset = int(params.get("offset", 0))
        if not 0 <= offset <= 100000:
            raise ValueError
    except (TypeError, ValueError) as exc:
        raise BusinessError("INVALID_INPUT", gettext("Page invalide."), status=400) from exc
    rows = list(query.order_by("_search_rank", "name", "id")[offset : offset + 51])
    return rows[:50], offset + 50 if len(rows) > 50 else None


def parameter_history(product):
    return (
        AuditEvent.objects.select_related("operation__actor")
        .filter(operation__command_type__startswith="catalog.product.")
        .filter(Q(operation__resource_id=product.pk) | Q(after__id=str(product.pk)))
        .order_by("-operation__confirmed_at", "-pk")
    )
