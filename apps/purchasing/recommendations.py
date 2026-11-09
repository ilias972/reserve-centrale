"""A11 validé le 30/10/2026 : calcul pur ; transitions liées aux faits confirmés."""

from django.db.models import (
    BigIntegerField,
    Case,
    Exists,
    F,
    OuterRef,
    Prefetch,
    Q,
    Subquery,
    Sum,
    Value,
    When,
)
from django.db.models.functions import Coalesce, Greatest
from django.utils.translation import gettext_lazy

from apps.cases.previews import fingerprint
from apps.catalog.models import Product, ProductSupplier
from apps.core.commands import Event
from apps.inventory.models import StockEntry
from apps.notifications.models import BusinessEvent

from .models import OrderLine

LABELS = {
    "ACTIVE": gettext_lazy("À commander"),
    "DEFERRED": gettext_lazy("Reportée"),
    "COVERED": gettext_lazy("Couverte par les achats attendus"),
    "INACTIVE": gettext_lazy("Aucun besoin actuel"),
}
STOCK_REACTIVATION = "recommendation.stock_reactivated"
POLICY = "A11-VALIDATED-2026-10-30"


def query():
    waiting = (
        OrderLine.objects.filter(product_id=OuterRef("pk"), unit=OuterRef("unit"))
        .values("product_id")
        .annotate(total=Sum(F("quantity") - F("received") - F("cancelled")))
        .values("total")
    )
    # L'éligibilité D/seuil/cible/W est figée à la vraie baisse, pas au prochain GET.
    changes = BusinessEvent.objects.filter(
        event_type=STOCK_REACTIVATION,
        aggregate_id=OuterRef("pk"),
        revision__gt=OuterRef("purchase_recommendation__latest_event__stock_version"),
    )
    return (
        Product.objects.select_related(
            "stock", "purchase_recommendation__latest_event", "current_photo"
        )
        .prefetch_related(
            Prefetch(
                "supplier_links",
                queryset=ProductSupplier.objects.filter(active=True, supplier__active=True)
                .select_related("supplier")
                .order_by("-preferred", "supplier__name", "supplier_id"),
                to_attr="available_suppliers",
            ),
            Prefetch(
                "orderline_set",
                queryset=OrderLine.objects.filter(quantity__gt=F("received") + F("cancelled"))
                .exclude(unit=F("product__unit"))
                .select_related("order")
                .order_by("pk"),
                to_attr="incompatible_purchases",
            ),
        )
        .annotate(
            _D=F("stock__physical") - F("stock__reserved"),
            _W=Coalesce(Subquery(waiting), Value(0), output_field=BigIntegerField()),
            _reactivated=Exists(changes),
            _rec_version=Coalesce(
                F("purchase_recommendation__version"), Value(0), output_field=BigIntegerField()
            ),
        )
        .annotate(
            _need=Greatest(
                Value(0), F("target") - F("_D") - F("_W"), output_field=BigIntegerField()
            )
        )
        .annotate(
            _deferred=Case(
                When(
                    purchase_recommendation__latest_event__action="DEFER",
                    _reactivated=False,
                    then=Value(True),
                ),
                default=Value(False),
            )
        )
        .annotate(
            _rec_state=Case(
                When(
                    ~Q(status="ACTIVE") | Q(counted_at__isnull=True) | Q(_D__gt=F("minimum")),
                    then=Value("INACTIVE"),
                ),
                When(_need=0, _W__gt=0, then=Value("COVERED")),
                When(_need=0, then=Value("INACTIVE")),
                When(
                    purchase_recommendation__latest_event__action="DEFER",
                    _reactivated=False,
                    then=Value("DEFERRED"),
                ),
                default=Value("ACTIVE"),
            )
        )
    )


def data(product):
    balance = product.stock
    suppliers = [
        {
            "id": str(link.supplier_id),
            "name": link.supplier.name,
            "website": link.website or link.supplier.website,
            "preferred": link.preferred,
            "reference": link.reference,
        }
        for link in product.available_suppliers
    ]
    incompatible = [
        {
            "order_id": str(line.order_id),
            "reference": line.order.reference,
            "line_id": str(line.pk),
            "waiting": line.waiting,
            "unit": line.unit,
        }
        for line in product.incompatible_purchases
    ]
    version = {
        "product": product.version,
        "stock": balance.version,
        "recommendation": product._rec_version,
        "waiting": int(product._W),
        "incompatible_waiting": incompatible,
        "reactivated": product._reactivated,
        "state": product._rec_state,
    }
    return {
        "product_id": str(product.pk),
        "code": product.code,
        "name": product.name,
        "unit": product.unit,
        "physical": balance.physical,
        "reserved": balance.reserved,
        "available": balance.available,
        "minimum": product.minimum,
        "target": product.target,
        "waiting": int(product._W),
        "incompatible_waiting": incompatible,
        "recommended": int(product._need) if product._rec_state != "INACTIVE" else 0,
        "state": product._rec_state,
        "state_label": LABELS[product._rec_state],
        "deferred": product._deferred,
        "shortage": balance.available == 0
        and product.status == "ACTIVE"
        and product.counted_at is not None,
        "suppliers": suppliers,
        "group_supplier": suppliers[0] if suppliers else None,
        "version": product._rec_version,
        "state_fingerprint": fingerprint(version),
        "photo_url": f"/api/v1/products/{product.pk}/photo" if product.current_photo_id else None,
        "reactivated_by_stock": product._reactivated,
    }


def stock_reactivations(operation):
    """Aucun verrou supplémentaire ni modification de stock.

    Appelé après apply, sous les verrous produits/stocks déjà détenus, avant outbox
    et commit. W est donc le solde final de l'opération, même pour une correction.
    Une libération suivie d'un mouvement du même lot ne vaut qu'une transition nette.
    """
    entries = list(StockEntry.objects.filter(operation=operation).order_by("sequence"))
    grouped = {}
    for entry in entries:
        grouped.setdefault(entry.product_id, []).append(entry)
    if not grouped:
        return ()
    events = []
    for product in query().filter(pk__in=grouped).order_by("pk"):
        report = getattr(product, "purchase_recommendation", None)
        latest = report.latest_event if report else None
        if not latest or latest.action != "DEFER" or product._reactivated:
            continue
        first, last = grouped[product.pk][0], grouped[product.pk][-1]
        if first.physical_before is None:
            continue
        before = first.physical_before - first.reserved_before
        after = last.physical_after - last.reserved_after
        if not (
            after < before
            and (after < latest.available or (before > 0 and after == 0))
            and product.status == "ACTIVE"
            and product.counted_at is not None
            and after <= product.minimum
            and product._need > 0
        ):
            continue
        events.append(
            Event(
                STOCK_REACTIVATION,
                product.pk,
                last.version_after,
                {
                    "policy": POLICY,
                    "report_event_id": str(latest.pk),
                    "stock_entry_id": str(last.pk),
                    "available_at_report": latest.available,
                    "available_before": before,
                    "available_after": after,
                    "waiting": int(product._W),
                    "recommended": int(product._need),
                    "minimum": product.minimum,
                    "target": product.target,
                    "product_name": product.name,
                    "unit": product.unit,
                },
            )
        )
    return tuple(events)
