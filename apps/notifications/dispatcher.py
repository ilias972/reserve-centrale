"""Projections et transitions A11 techniques, dans la transaction de traitement d'événement."""

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext_noop

from . import routing
from .models import Activation, Notification, Outbox, ProductAlertState, PushSubscription, WorkItem


def alert_values(product):
    from apps.purchasing.recommendations import data

    value = data(product)
    return value["state"] == "ACTIVE" and value["recommended"] > 0, value["shortage"]


@transaction.atomic
def initialize():
    # Verrou distinct de tout verrou métier : les notifications ne bloquent pas les stocks.
    from django.db import connection

    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(93279, 9)")
    row, created = Activation.objects.get_or_create(pk=1)
    if created:
        from apps.purchasing.recommendations import query

        for product in query().filter(counted_at__isnull=False).order_by("pk"):
            need, shortage = alert_values(product)
            ProductAlertState.objects.get_or_create(
                product=product, defaults={"need": need, "shortage": shortage}
            )
    return row


def enqueue_events(limit=100):
    activation = Activation.objects.filter(pk=1).first()
    if activation is None:
        raise RuntimeError("Initialisez M9 avant le worker : initialize_notifications.")
    pending = (
        Outbox.objects.filter(created_at__gte=activation.started_at)
        .exclude(workitem__kind="EVENT")
        .order_by("created_at", "pk")[:limit]
    )
    WorkItem.objects.bulk_create(
        [WorkItem(kind="EVENT", outbox=o, dedupe_key=f"event:{o.pk}") for o in pending],
        ignore_conflicts=True,
    )


def notify(work, user, title, message, target_kind, target_id, fact_key):
    notification, created = Notification.objects.get_or_create(
        operation=work.outbox.event.operation,
        recipient=user,
        fact_key=fact_key,
        defaults={
            "event": work.outbox.event,
            "scope": routing.scope(user),
            "title": title,
            "message": message[:500],
            "target_kind": target_kind,
            "target_id": target_id,
        },
    )
    if created and user.role == "REPLENISHER":
        for sub in PushSubscription.objects.filter(user=user, revoked_at__isnull=True):
            WorkItem.objects.get_or_create(
                dedupe_key=f"push:{notification.pk}:{sub.pk}",
                defaults={
                    "kind": "PUSH",
                    "mode": "PUSH",
                    "outbox": work.outbox,
                    "notification": notification,
                    "subscription": sub,
                },
            )
    return notification


def alerts(work):
    from apps.purchasing.recommendations import query

    # Toute écriture métier a un événement operation.confirmed, y compris inventaire/
    # paramètres. Les autres événements de la même opération ne recalculent pas les besoins.
    if work.outbox.event.event_type != "operation.confirmed":
        return
    from apps.catalog.models import Product
    from apps.inventory.models import StockEntry
    from apps.purchasing.models import OrderLine, RecommendationEvent

    from .models import BusinessEvent

    operation = work.outbox.event.operation
    identifiers = set(
        StockEntry.objects.filter(operation=operation).values_list("product_id", flat=True)
    )
    identifiers.update(
        Product.objects.filter(last_operation=operation).values_list("pk", flat=True)
    )
    identifiers.update(
        RecommendationEvent.objects.filter(operation=operation).values_list(
            "recommendation_id", flat=True
        )
    )
    order_ids = BusinessEvent.objects.filter(
        operation=operation, event_type__startswith="purchase."
    ).values("aggregate_id")
    identifiers.update(
        OrderLine.objects.filter(order_id__in=order_ids).values_list("product_id", flat=True)
    )
    for identifier in sorted(identifiers):
        row, _ = ProductAlertState.objects.get_or_create(product_id=identifier)
        row = ProductAlertState.objects.select_for_update().get(pk=row.pk)
        # Relecture APRÈS le verrou de projection : un autre worker a pu traiter
        # une réception/commande pendant notre attente. Jamais de snapshot recyclé.
        product = query().filter(pk=identifier, counted_at__isnull=False).first()
        if product is None:
            continue
        need, shortage = alert_values(product)
        # Une réactivation A11 possède désormais son fait source propre et unique.
        # Même si ce worker traite le report en retard, il ne doit pas annoncer
        # une seconde fois la baisse qui a déjà son événement transactionnel.
        trigger = None
        if product._reactivated:
            from apps.purchasing.recommendations import STOCK_REACTIVATION

            trigger = BusinessEvent.objects.filter(
                event_type=STOCK_REACTIVATION,
                aggregate_id=identifier,
                revision__gt=product.purchase_recommendation.latest_event.stock_version,
            ).first()
        new_need = need and not row.need and trigger is None
        new_shortage = (
            shortage
            and not row.shortage
            and not (trigger and trigger.payload["available_after"] == 0)
        )
        if new_need or new_shortage:
            title = (
                gettext_noop("Produit indisponible")
                if shortage and not row.shortage
                else gettext_noop("Besoin d'achat à consulter")
            )
            for user in routing.active_users(role="ADMIN"):
                notify(
                    work,
                    user,
                    title,
                    gettext_noop(
                        "Un produit nécessite votre attention. Consultez le besoin et les quantités encore attendues."
                    ),
                    "PRODUCT",
                    product.pk,
                    f"need:{product.pk}",
                )
        row.need, row.shortage, row.updated_at = need, shortage, timezone.now()
        row.save(update_fields=["need", "shortage", "updated_at"])


def process(work):
    event = work.outbox.event
    for user, title, message, kind, identifier, key in routing.project(event):
        notify(work, user, title, message, kind, identifier, key)
    if event.event_type == "purchase.recorded":
        # Canal figé à la création : une capture ne devient jamais un envoi SMTP.
        mode = "SMTP" if settings.NOTIFICATION_EMAIL_MODE == "SMTP" else "CAPTURE"
        for alias in ("RESPONSIBLE", "ACCOUNTANT"):
            WorkItem.objects.get_or_create(
                dedupe_key=f"email:{event.pk}:{alias}",
                defaults={
                    "kind": "EMAIL",
                    "mode": mode,
                    "destination": alias,
                    "email_to": settings.NOTIFICATION_EMAIL_RECIPIENTS.get(alias, ""),
                    "outbox": work.outbox,
                },
            )
    alerts(work)
