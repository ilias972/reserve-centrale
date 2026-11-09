from django.utils.translation import gettext

from apps.accounts.models import User
from apps.catalog.models import Product
from apps.core.errors import BusinessError
from apps.core.payloads import parse_uuid
from apps.core.search import matching

from .models import StockEntry


def stock_reader(actor):
    if actor.role not in {User.Role.ADMIN, User.Role.REPLENISHER}:
        raise BusinessError(
            "PERMISSION_DENIED", gettext("Accès aux stocks non autorisé."), status=403
        )


def page_offset(params):
    try:
        value = int(params.get("offset", 0))
        if not 0 <= value <= 100000:
            raise ValueError
        return value
    except (ValueError, TypeError) as exc:
        raise BusinessError("INVALID_INPUT", gettext("Page invalide."), status=400) from exc


def stock_data(product):
    balance = product.stock
    from django.core.exceptions import ObjectDoesNotExist

    try:
        hold = product.stock_hold
    except ObjectDoesNotExist:
        hold = None
    return {
        "product_id": str(product.pk),
        "code": product.code,
        "name": product.name,
        "unit": product.unit,
        "status": product.status,
        "status_label": product.get_status_display(),
        "physical": balance.physical,
        "reserved": balance.reserved,
        "available": balance.available,
        "stock_version": balance.version,
        "product_version": product.version,
        "counted_at": product.counted_at.isoformat() if product.counted_at else None,
        "photo_url": f"/api/v1/stocks/{product.pk}/photo" if product.current_photo_id else None,
        "on_hold": bool(hold),
        "case_id": str(hold.case_id) if hold else None,
    }


def stocks_list(actor, params):
    stock_reader(actor)
    query = matching(
        Product.objects.select_related("stock", "stock_hold", "current_photo__asset"),
        params.get("q", ""),
        ("name", "code"),
    )
    if params.get("category"):
        query = query.filter(category_id=parse_uuid(params["category"]))
    if params.get("mode") == "initial":
        query = query.filter(counted_at__isnull=True)
    elif params.get("mode") == "counted":
        query = query.filter(counted_at__isnull=False)
    elif params.get("mode") not in {None, "", "all"}:
        raise BusinessError("INVALID_INPUT", gettext("Filtre invalide."), status=400)
    offset = page_offset(params)
    rows = list(query.order_by("_search_rank", "name", "id")[offset : offset + 51])
    return [stock_data(row) for row in rows[:50]], offset + 50 if len(rows) > 50 else None


def activity_query(actor):
    stock_reader(actor)
    query = StockEntry.objects.select_related("operation__actor")
    if actor.role != User.Role.ADMIN:
        query = query.filter(operation__actor=actor)
    return query.order_by("-operation__confirmed_at", "-id")


def entry_data(entry):
    return {
        "id": str(entry.pk),
        "operation_id": str(entry.operation_id),
        "actor": entry.operation.actor.display_name,
        "at": entry.operation.confirmed_at.isoformat(),
        "product_id": str(entry.product_id),
        "code": entry.product_code,
        "name": entry.product_name,
        "unit": entry.unit,
        "kind": entry.kind,
        "kind_label": entry.get_kind_display(),
        "physical_before": entry.physical_before,
        "physical_after": entry.physical_after,
        "reserved_before": entry.reserved_before,
        "reserved_after": entry.reserved_after,
        "delta_p": entry.delta_p,
        "delta_r": entry.delta_r,
        "version_after": entry.version_after,
    }
