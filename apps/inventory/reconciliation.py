"""A03 : libérations explicitement sélectionnées et comptage, en une transaction M0."""

from django.utils.translation import gettext

from apps.cases.models import Case, ReconciledStock, StockHold, StockReconciliation
from apps.cases.previews import fingerprint
from apps.cases.workflow import add_event, reviewed, scope
from apps.core.commands import Command, Effect
from apps.core.errors import BusinessError
from apps.core.payloads import invalid_input, parse_uuid
from apps.core.policies import check_version
from apps.inventory.primitives import append_change, lock_stocks
from apps.requests.corrections import decision, lock_requests, persist_line, project, result_effect
from apps.requests.models import RequestLine


def resources(actor, identifier, payload):
    rows = payload.get("product_counts")
    if not isinstance(rows, list) or not 1 <= len(rows) <= 100:
        raise invalid_input()
    ids = {parse_uuid(r.get("product_id")) for r in rows if isinstance(r, dict)}
    if len(ids) != len(rows):
        raise invalid_input()
    request_ids = set(
        RequestLine.objects.filter(product_id__in=ids, reservation__remaining__gt=0).values_list(
            "request_id", flat=True
        )
    )
    requests, lines = lock_requests(request_ids)
    products = ids | {line_item.product_id for group in lines.values() for line_item in group}
    stocks = lock_stocks(products)
    case = (
        Case.objects.select_for_update(of=("self",))
        .select_related("origin_operation")
        .get(pk=identifier)
    )
    current_ids = set(
        RequestLine.objects.filter(product_id__in=ids, reservation__remaining__gt=0).values_list(
            "request_id", flat=True
        )
    )
    if current_ids != request_ids:
        raise BusinessError(
            "SCOPE_CHANGED",
            gettext("Les engagements ont changé. Rechargez le dossier avant de confirmer."),
        )
    return case, requests, lines, stocks


def plan(data, payload):
    case, requests, lines, stocks = data
    if set(payload) - {
        "case_id",
        "expected_case_version",
        "product_counts",
        "commitments",
        "verification",
        "message",
        "preview_hash",
    }:
        raise invalid_input()
    if parse_uuid(payload.get("case_id")) != case.pk:
        raise invalid_input()
    reviewed(payload)
    check_version(case.version, payload.get("expected_case_version"))
    if case.kind != "STOCK_CONFLICT" or case.status == "RESOLVED":
        raise BusinessError(
            "INVALID_STATE", gettext("Ce dossier n'attend pas de régularisation de stock.")
        )
    targets = {}
    for row in payload["product_counts"]:
        if set(row) != {"product_id", "expected_stock_version", "counted_quantity"}:
            raise invalid_input()
        pid = parse_uuid(row["product_id"])
        q = row["counted_quantity"]
        if type(q) is not int or not 0 <= q <= 1000000000:
            raise invalid_input()
        check_version(stocks[pid].balance.version, row["expected_stock_version"])
        targets[pid] = q
    expected = set(case.stock_case.products.values_list("product_id", flat=True))
    if (
        set(targets) != expected
        or set(
            StockHold.objects.filter(case_id=case.stock_case_id).values_list(
                "product_id", flat=True
            )
        )
        != expected
    ):
        raise BusinessError(
            "CASE_SCOPE_CHANGED",
            gettext("Vérifiez tous les produits de ce conflit, sans en ajouter d'autres."),
        )
    commitments = payload.get("commitments", [])
    if not isinstance(commitments, list) or len(commitments) > 1000:
        raise invalid_input()
    byline = {
        line_item.pk: (r, line_item)
        for r in requests
        for line_item in lines[r.pk]
        if line_item.product_id in targets and line_item.reservation.remaining
    }
    releases = {}
    for row in commitments:
        if not isinstance(row, dict) or set(row) != {
            "request_id",
            "request_line_id",
            "expected_request_version",
            "release_quantity",
        }:
            raise invalid_input()
        lid = parse_uuid(row["request_line_id"])
        q = row["release_quantity"]
        if lid not in byline or lid in releases or type(q) is not int:
            raise invalid_input()
        r, line_item = byline[lid]
        check_version(r.version, row["expected_request_version"])
        if parse_uuid(row["request_id"]) != r.pk or not 0 <= q <= line_item.reservation.remaining:
            raise invalid_input()
        releases[lid] = q
    impacted = []
    for lid, (r, line_item) in byline.items():
        impacted.append(
            {
                "request_id": str(r.pk),
                "request_line_id": str(lid),
                "request_reference": r.reference,
                "request_version": r.version,
                "floor": r.floor_name,
                "assistant": r.assistant_name,
                "assigned_to_id": str(r.assigned_to_id) if r.assigned_to_id else None,
                "product_id": str(line_item.product_id),
                "reserved_before": line_item.reservation.remaining,
                "release_quantity": releases.get(lid, 0),
                "reserved_after": line_item.reservation.remaining - releases.get(lid, 0),
            }
        )
    counts = []
    for pid, target in targets.items():
        stock = stocks[pid]
        released = sum(x["release_quantity"] for x in impacted if x["product_id"] == str(pid))
        reserved = stock.balance.reserved - released
        if target < reserved:
            raise BusinessError(
                "RECONCILIATION_INCOMPATIBLE",
                gettext(
                    "Les engagements choisis ne suffisent pas. Aucune libération ni correction effectuée."
                ),
                details={
                    "product_id": str(pid),
                    "reserved_after": reserved,
                    "counted_quantity": target,
                },
            )
        counts.append(
            {
                "product_id": str(pid),
                "name": stock.product.name,
                "unit": stock.product.unit,
                "P_before": stock.balance.physical,
                "R_before": stock.balance.reserved,
                "D_before": stock.balance.available,
                "P_after": target,
                "R_after": reserved,
                "D_after": target - reserved,
                "version_before": stock.balance.version,
            }
        )
    result = {
        "case_id": str(case.pk),
        "case_version": case.version,
        "products": counts,
        "commitments": impacted,
    }
    result["preview_hash"] = fingerprint(result)
    return result


def confirm(execution, data, payload):
    result = plan(data, payload)
    if payload.get("preview_hash") != result["preview_hash"]:
        raise BusinessError(
            "PREVIEW_CHANGED", gettext("Les engagements ont changé. Prévisualisez de nouveau.")
        )
    case, requests, lines, stocks = data
    fact = StockReconciliation.objects.create(
        case=case, operation_id=execution.operation_id, verification=True, message=reviewed(payload)
    )
    releases = {
        parse_uuid(r["request_line_id"]): r["release_quantity"] for r in result["commitments"]
    }
    seq = 0
    events = []
    for request in requests:
        if not any(releases.get(line_item.pk, 0) for line_item in lines[request.pk]):
            continue
        dec = decision(
            execution,
            request,
            "RECONCILED",
            case=case,
            cause="ADMIN_RECONCILIATION",
            message=fact.message,
        )
        for line_item in lines[request.pk]:
            seq += 1
            b = releases.get(line_item.pk, 0)
            persist_line(
                execution,
                dec,
                line_item,
                stocks[line_item.product_id],
                delivered=line_item.delivered,
                closed=line_item.closed + b,
                reserved=line_item.reservation.remaining - b,
                sequence=seq,
            )
        request.closure_reason = "ADMIN_RECONCILIATION"
        project(request, lines[request.pk])
        events.extend(result_effect(request, "request.reconciled").events)
    for count in result["products"]:
        stock = stocks[parse_uuid(count["product_id"])]
        entry = None
        if stock.balance.physical != count["P_after"]:
            seq += 1
            entry = append_change(
                execution,
                stock,
                kind="ADMIN_RECONCILIATION",
                physical=count["P_after"],
                sequence=seq,
            )
        ReconciledStock.objects.create(
            reconciliation=fact,
            product=stock.product,
            physical_before=count["P_before"],
            reserved_before=count["R_before"],
            counted_quantity=count["P_after"],
            reserved_after=stock.balance.reserved,
            version_before=count["version_before"],
            version_after=stock.balance.version,
            entry=entry,
        )
    events.append(
        add_event(
            execution,
            case,
            "STOCK_RECONCILED",
            "RESOLVED",
            {"reconciliation_id": str(fact.pk), "verification": True, "message": fact.message},
        )
    )
    StockHold.objects.filter(case_id=case.stock_case_id).delete()
    return Effect(
        {**result, "status": "RESOLVED", "version": case.version},
        after=result,
        details=result["commitments"],
        events=tuple(events),
    )


COMMAND = Command(
    "stock_reconciliations.confirm",
    frozenset({"ADMIN"}),
    scope,
    lambda a, i: None,
    confirm,
    lock_payload_resources=resources,
)
