from django.db import transaction
from django.http import JsonResponse

from apps.core.api import actor_context, endpoint, json_body
from apps.core.payloads import parse_uuid
from apps.core.policies import check_role, lock_actor
from apps.core.transactions import configure_transaction
from apps.requests.api import perform

from . import reading


@endpoint(access="authenticated", methods=["GET", "POST"])
def cases(request, case_id=None):
    if request.method == "POST":
        return perform(request, "cases.open", json_body(request))
    with transaction.atomic():
        actor = lock_actor(actor_context(request))
        if case_id:
            return JsonResponse({"case": reading.data(reading.get(actor, case_id))})
        from apps.inventory.selectors import page_offset

        offset = page_offset(request.GET)
        rows = reading.query(actor)
        if request.GET.get("status"):
            rows = rows.filter(status=request.GET["status"])
        listed = list(rows[offset : offset + 51])
        return JsonResponse(
            {
                "cases": [reading.data(c) for c in listed[:50]],
                "next_offset": offset + 50 if len(listed) > 50 else None,
            }
        )


@endpoint(access="authenticated", methods=["POST"])
def review(request, case_id):
    return perform(request, "cases.review", json_body(request), case_id)


@endpoint(access="authenticated", methods=["POST"])
def resolve(request, case_id):
    payload = json_body(request)
    from apps.receiving.state import CASE_KINDS

    from .models import Case

    command = (
        "receiving.case_resolve"
        if Case.objects.filter(pk=case_id, kind__in=CASE_KINDS).exists()
        else "cases.resolve"
    )
    return perform(request, command, payload, case_id)


@endpoint(access="authenticated", methods=["POST"])
def correction(request, action):
    payload = json_body(request)
    source = parse_uuid(payload.get("source_id"))
    if action == "confirm":
        return perform(request, "corrections.confirm", payload, source)
    from apps.requests.corrections import correction_plan, correction_resources

    with transaction.atomic():
        configure_transaction()
        actor = lock_actor(actor_context(request))
        check_role(actor, {"ADMIN"})
        if payload.get("kind") == "RECEIPT":
            from apps.receiving import corrections
            from apps.receiving.locks import receipt_supplier

            receipt_supplier(actor, source)
            return JsonResponse(
                {
                    "preview": corrections.plan(
                        corrections.resources(actor, source, payload), payload
                    )
                }
            )
        result, _ = correction_plan(correction_resources(actor, source), payload)
        return JsonResponse({"preview": result})


@endpoint(access="authenticated", methods=["POST"])
def reconciliation(request, action):
    payload = json_body(request)
    identifier = parse_uuid(payload.get("case_id"))
    if action == "confirm":
        return perform(request, "stock_reconciliations.confirm", payload, identifier)
    from apps.inventory.reconciliation import plan, resources

    with transaction.atomic():
        configure_transaction()
        actor = lock_actor(actor_context(request))
        check_role(actor, {"ADMIN"})
        result = plan(resources(actor, identifier, payload), payload)
        return JsonResponse({"preview": result})


@endpoint(access="authenticated", methods=["POST"])
def request_action(request, request_id, action):
    return perform(
        request,
        {
            "close-remainder": "requests.close_remainder",
            "reopen": "requests.reopen",
            "reassign": "requests.reassign",
        }[action],
        json_body(request),
        request_id,
    )


@endpoint(access="authenticated", methods=["POST"])
def request_preview(request, request_id, action):
    from apps.requests.corrections import request_plan, resources

    payload = json_body(request)
    with transaction.atomic():
        configure_transaction()
        actor = lock_actor(actor_context(request))
        check_role(actor, {"ADMIN"})
        return JsonResponse(
            {"preview": request_plan(action, resources(actor, request_id), payload)}
        )
