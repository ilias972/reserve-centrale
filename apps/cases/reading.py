from django.utils.translation import gettext

from apps.core.errors import BusinessError

from .models import Case


def query(actor):
    rows = Case.objects.select_related(
        "origin_operation__actor",
        "origin_operation__auditevent",
        "stock_case",
        "delivery__request",
        "delivery_issue",
        "receipt__event__order",
    ).order_by("-origin_operation__confirmed_at", "-id")
    if actor.role == "ADMIN":
        return rows
    if actor.role == "REPLENISHER":
        return rows.filter(origin_operation__actor=actor)
    raise BusinessError(
        "PERMISSION_DENIED", gettext("Accès aux dossiers non autorisé."), status=403
    )


def get(actor, identifier):
    row = query(actor).filter(pk=identifier).first()
    if row is None:
        raise BusinessError("NOT_FOUND", gettext("Dossier introuvable."), status=404)
    return row


def data(case):
    from .classification import CATEGORIES, primary_category

    category = primary_category(case.kind)
    result = {
        "id": str(case.pk),
        "reference": case.reference,
        "type": case.kind,
        "primary_category": category,
        "primary_category_label": CATEGORIES[category],
        "status": case.status,
        "version": case.version,
        "reporter": case.origin_operation.actor.display_name,
        "at": case.origin_operation.confirmed_at.isoformat(),
        "origin_operation_id": str(case.origin_operation_id),
        "message": case.delivery_issue.message
        if case.delivery_issue_id
        else case.origin_operation.auditevent.after.get("message", ""),
        "events": [
            {
                "kind": e.kind,
                "status": e.status,
                "at": e.operation.confirmed_at.isoformat(),
                "actor": e.operation.actor.display_name,
                "details": e.details,
            }
            for e in case.events.select_related("operation__actor").order_by("revision")
        ],
    }
    if case.stock_case_id:
        from .selectors import case_data

        result = {**case_data(case.stock_case), **result}
        failed = case.stock_case.failed_intent
        if failed.command_type == "corrections.confirm":
            from apps.requests.models import Delivery

            source = (
                Delivery.objects.select_related("request").filter(pk=failed.resource_id).first()
            )
            if source:
                result.update(
                    source_delivery_id=str(source.pk),
                    source_request_id=str(source.request_id),
                    source_request_reference=source.request.reference,
                )
            else:
                from apps.receiving.models import Receipt

                receipt = Receipt.objects.select_related("event__order").get(pk=failed.resource_id)
                result.update(
                    source_receipt_id=str(receipt.pk),
                    source_order_id=str(receipt.event.order_id),
                    source_order_reference=receipt.event.order.reference,
                )

    if case.delivery_id:
        result.update(
            delivery_id=str(case.delivery_id),
            request_id=str(case.delivery.request_id),
            request_reference=case.delivery.request.reference,
        )
    if case.receipt_id:
        result.update(
            receipt_id=str(case.receipt_id),
            receipt_reference=case.receipt.reference,
            order_id=str(case.receipt.event.order_id),
            order_reference=case.receipt.event.order.reference,
        )
    return result
