"""Persistance d'un refus A03 dans la transaction externe ; aucune régularisation."""

from datetime import timedelta
from uuid import uuid4

from django.conf import settings

from apps.core.commands import Effect, Event
from apps.core.idempotency import finish, record_facts
from apps.core.models import Operation, OperationIntent
from apps.core.payloads import parse_uuid, payload_hash
from apps.core.transactions import database_now

from .models import StockCase, StockCaseProduct, StockHold


def open_stock_case(execution, stocks, shortages, *, source_case_id=None):
    now = database_now()
    intent = OperationIntent.objects.create(
        actor=execution.actor,
        command_type="cases.stock_conflict",
        idempotency_key=uuid4().hex,
        payload_hash=payload_hash({"failed_intent_id": str(execution.intent.pk)}),
        generation_id=settings.OPERATION_GENERATION,
        created_at=now,
        expires_at=now + timedelta(seconds=settings.OPERATION_INTENT_TTL_SECONDS),
    )
    operation = Operation.objects.create(
        intent=intent,
        actor=execution.actor,
        actor_role=execution.actor.role,
        command_type="cases.stock_conflict",
        confirmed_at=now,
        correlation_id=execution.correlation_id,
    )
    case = StockCase.objects.create(
        failed_intent=execution.intent, operation=operation, reporter=execution.actor
    )
    from .workflow import stock_projection

    stock_projection(case)
    for item in shortages:
        stock = stocks[parse_uuid(item["product_id"])]
        stock.balance.refresh_from_db()
        StockCaseProduct.objects.create(
            case=case,
            product=stock.product,
            product_code=stock.product.code,
            product_name=stock.product.name,
            unit=stock.product.unit,
            physical=stock.balance.physical,
            reserved=stock.balance.reserved,
            attempted_physical=item["attempted_physical"],
            is_calculated=execution.intent.command_type == "corrections.confirm",
            stock_version=stock.balance.version,
        )
        StockHold.objects.create(product=stock.product, case=case)
    result = {
        "id": str(case.pk),
        "reference": case.reference,
        "failed_intent_id": str(execution.intent.pk),
        "state": "OPEN",
        "stock_movement_recorded": False,
    }
    extra_events = []
    if source_case_id is not None:
        from types import SimpleNamespace

        from .models import Case
        from .workflow import add_event

        source = (
            Case.objects.select_for_update(of=("self",))
            .select_related("origin_operation")
            .get(pk=source_case_id)
        )
        incident = SimpleNamespace(operation_id=operation.pk)
        extra_events.append(
            add_event(
                incident,
                source,
                "CORRECTION_BLOCKED",
                "UNDER_REVIEW",
                {
                    "verification": True,
                    "message": "Correction refusée : conflit de stock à régulariser, puis opération d’origine à vérifier.",
                    "stock_case_id": str(case.pk),
                },
            )
        )
        result["source_case_id"] = str(source.pk)
    record_facts(
        operation,
        Effect(
            result,
            after=result,
            details=shortages,
            events=(
                *extra_events,
                Event(
                    "stock.conflict.opened",
                    case.pk,
                    1,
                    {"case_id": str(case.pk), "recipient_role": "ADMIN"},
                ),
            ),
        ),
    )
    finish(
        intent,
        "CONFIRMED",
        {
            "operation_id": str(operation.pk),
            "intent_id": str(intent.pk),
            "result": "CONFIRMED",
            "resource": result,
        },
        200,
    )
    return {
        "case_id": str(case.pk),
        "case_reference": case.reference,
        "case_url": f"/stocks/signalements/{case.pk}",
        "incident_operation_id": str(operation.pk),
        "stock_movement_recorded": False,
    }
