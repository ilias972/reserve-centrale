from django.db import connection

from apps.cases.models import Case
from apps.cases.workflow import add_event
from apps.core.commands import Event
from apps.core.payloads import invalid_input


def note(payload):
    value = payload.get("message", "")
    if not isinstance(value, str) or len(value) > 2000:
        raise invalid_input()
    return value.strip()


def reference(kind):
    sequence, prefix = {
        "receipt": ("receipt_reference_seq", "REC"),
        "report": ("arrival_reference_seq", "ISO"),
    }[kind]
    with connection.cursor() as cursor:
        cursor.execute("SELECT nextval(%s::regclass)", ["public." + sequence])
        return f"{prefix}-{cursor.fetchone()[0]:08d}"


def new_case(execution, kind, *, receipt=None):
    return Case.objects.create(
        kind=kind, receipt=receipt, origin_operation_id=execution.operation_id
    )


def opened(case):
    return Event("case.opened", case.pk, 1, {"case_id": str(case.pk), "recipient_roles": ["ADMIN"]})


def isolation_event(execution, case, kind, *, message="", verification=True):
    from .models import ArrivalReport, QuarantineItem, UnknownLink
    from .state import item_state

    items = list(QuarantineItem.objects.filter(case=case))
    report = ArrivalReport.objects.filter(case=case).first()
    identified = (
        report is None
        or report.kind != "UNKNOWN_ORDER"
        or UnknownLink.objects.filter(report=report).exists()
    )
    complete = bool(items) and all(item_state(item.pk)["remaining"] == 0 for item in items)
    status = "RESOLVED" if identified and complete else "UNDER_REVIEW"
    return add_event(
        execution, case, kind, status, {"verification": verification, "message": message}
    )
