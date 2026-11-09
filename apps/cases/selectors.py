from django.utils.translation import gettext

from apps.accounts.models import User
from apps.core.errors import BusinessError

from .models import StockCase


def case_query(actor):
    query = StockCase.objects.select_related("operation", "reporter").prefetch_related("products")
    if actor.role == User.Role.ADMIN:
        return query.order_by("-operation__confirmed_at", "id")
    if actor.role == User.Role.REPLENISHER:
        return query.filter(reporter=actor).order_by("-operation__confirmed_at", "id")
    raise BusinessError(
        "PERMISSION_DENIED", gettext("Accès aux signalements non autorisé."), status=403
    )


def case_data(case):
    return {
        "id": str(case.pk),
        "reference": case.reference,
        "status": case.case.status,
        "type": "STOCK_CONFLICT",
        "reporter": case.reporter.display_name,
        "at": case.operation.confirmed_at.isoformat(),
        "failed_intent_id": str(case.failed_intent_id),
        "incident_operation_id": str(case.operation_id),
        "stock_movement_recorded": False,
        "products": [
            {
                "product_id": str(row.product_id),
                "code": row.product_code,
                "name": row.product_name,
                "unit": row.unit,
                "physical": row.physical,
                "reserved": row.reserved,
                "attempted_physical": row.attempted_physical,
                "is_calculated": row.is_calculated,
                "stock_version": row.stock_version,
            }
            for row in case.products.all()
        ],
    }
