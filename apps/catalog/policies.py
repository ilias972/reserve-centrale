from django.db.models import Exists, F, OuterRef
from django.utils.translation import gettext

from apps.accounts.models import User
from apps.core.errors import BusinessError

from .models import Product, ProductSupplier


def visible_products(actor):
    from apps.cases.models import StockHold

    query = Product.objects.select_related(
        "category", "current_photo__asset", "stock", "initial_inventory_operation__actor"
    ).annotate(
        _on_hold=Exists(StockHold.objects.filter(product_id=OuterRef("pk"))),
        _supplier_configured=Exists(
            ProductSupplier.objects.filter(
                product_id=OuterRef("pk"), active=True, supplier__active=True
            )
        ),
    )
    if actor.role == User.Role.ADMIN:
        return query
    query = query.exclude(status=Product.Status.ARCHIVED)
    if actor.is_requester:
        if not actor.floor_id or not actor.floor.active:
            return query.none()
        return query.filter(
            floor_permissions__floor_id=actor.floor_id, floor_permissions__visible=True
        ).annotate(
            _floor_maximum=F("floor_permissions__maximum"),
            _floor_demandable=F("floor_permissions__demandable"),
        )
    return query


def product_in_scope(actor, product_id):
    if not visible_products(actor).filter(pk=product_id).exists():
        raise BusinessError("NOT_FOUND", gettext("Produit introuvable."), status=404)


def catalog_admin(actor):
    if actor.role != User.Role.ADMIN:
        raise BusinessError(
            "PERMISSION_DENIED",
            gettext("La configuration du catalogue est réservée à la gestionnaire."),
            status=403,
        )
