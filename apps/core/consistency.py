"""Diagnostic de support : snapshot cohérent, aucune réparation ni donnée personnelle."""

import hashlib
from pathlib import Path

from django.db import DatabaseError, connection, transaction
from django.db.migrations.executor import MigrationExecutor

from apps.cases.models import Case
from apps.catalog.models import PhotoAsset, Product
from apps.catalog.storage import photo_storage
from apps.core.models import Operation, OperationIntent
from apps.notifications.models import (
    Attempt,
    BusinessEvent,
    CapturedEmail,
    Notification,
    Outbox,
    WorkItem,
)
from apps.purchasing.models import PurchaseOrder
from apps.receiving.models import ArrivalReport, QuarantineItem, Receipt
from apps.requests.models import InternalRequest


def check_consistency(*, files=True):
    """Les codes stables identifient la règle, jamais le texte brut d'une exception SQL."""
    issues, counts = [], {}

    def issue(rule, identifier):
        issues.append({"rule": rule, "object": str(identifier)})

    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        cursor.execute("SET LOCAL statement_timeout = '15s'")
        cursor.execute("SET LOCAL lock_timeout = '2s'")
        executor = MigrationExecutor(connection)
        if executor.migration_plan(executor.loader.graph.leaf_nodes()):
            issue("MIGRATIONS_PENDING", "schema")
            return {"ok": False, "counts": counts, "issues": issues}
        checks = [
            (Product, "reserve_assert_stock"),
            (Product, "reserve_assert_reservations"),
            (InternalRequest, "reserve_assert_request"),
            (PurchaseOrder, "reserve_m7_assert_order"),
            (Receipt, "reserve_m8_assert_receipt"),
            (ArrivalReport, "reserve_m8_assert_report"),
            (QuarantineItem, "reserve_m8_assert_item"),
        ]
        for model, function in checks:
            count = 0
            for identifier in model.objects.values_list("pk", flat=True).iterator():
                try:
                    with transaction.atomic():
                        # Noms constants internes, identifiant métier paramétré.
                        cursor.execute(f"SELECT public.{function}(%s)", [identifier])
                except DatabaseError as exc:
                    code = getattr(exc.__cause__, "sqlstate", "") or ""
                    if not code.startswith("23"):
                        raise
                    issue(function, identifier)
                count += 1
            counts[function] = count
        for operation in Operation.objects.select_related("intent").iterator():
            if operation.intent.state != OperationIntent.State.CONFIRMED:
                issue("OPERATION_INTENT_NOT_CONFIRMED", operation.pk)
            if not hasattr(operation, "auditevent"):
                issue("OPERATION_AUDIT_MISSING", operation.pk)
            if operation.actor_id != operation.intent.actor_id:
                issue("OPERATION_ACTOR_MISMATCH", operation.pk)
        for intent in OperationIntent.objects.filter(state="CONFIRMED").iterator():
            if not hasattr(intent, "operation"):
                issue("CONFIRMED_OPERATION_MISSING", intent.pk)
        for event in BusinessEvent.objects.iterator():
            if not Outbox.objects.filter(event=event).exists():
                issue("EVENT_OUTBOX_MISSING", event.pk)
        for notification in Notification.objects.select_related("event").iterator():
            if notification.operation_id != notification.event.operation_id:
                issue("NOTIFICATION_EVENT_OPERATION", notification.pk)
        for work in WorkItem.objects.select_related(
            "outbox", "notification", "subscription"
        ).iterator():
            if work.kind == "PUSH" and (
                work.notification.event_id != work.outbox.event_id
                or work.notification.recipient_id != work.subscription.user_id
            ):
                issue("PUSH_RECIPIENT_EVENT", work.pk)
            attempts = list(Attempt.objects.filter(work=work).values_list("number", "token"))
            if sorted(n for n, _ in attempts) != list(range(1, work.attempts + 1)):
                issue("WORK_ATTEMPTS_SEQUENCE", work.pk)
            if work.state == "CLAIMED" and work.lease_token not in {token for _, token in attempts}:
                issue("WORK_LEASE_ATTEMPT", work.pk)
        for capture in CapturedEmail.objects.select_related("work").iterator():
            if capture.work.kind != "EMAIL" or capture.work.mode != "CAPTURE":
                issue("CAPTURE_WRONG_CHANNEL", capture.pk)
        cursor.execute(
            "SELECT conname FROM pg_constraint WHERE connamespace='public'::regnamespace AND NOT convalidated"
        )
        for (name,) in cursor.fetchall():
            issue("SQL_CONSTRAINT_NOT_VALIDATED", name)
        for case in Case.objects.prefetch_related("events").iterator(chunk_size=200):
            latest = max(case.events.all(), key=lambda event: event.revision, default=None)
            initial = latest is None and case.version == 0 and case.status == "OPEN"
            if not initial and (
                latest is None or (latest.revision, latest.status) != (case.version, case.status)
            ):
                issue("CASE_EVENT_PROJECTION", case.pk)
        if files:
            storage = photo_storage()
            root = Path(storage.location).resolve()
            for asset in PhotoAsset.objects.iterator():
                for key, size, digest in [
                    (asset.object_key, asset.size, asset.digest),
                    (asset.thumbnail_key, asset.thumbnail_size, None),
                ]:
                    path = root / key
                    if path.is_symlink() or not path.resolve().is_relative_to(root):
                        issue("PHOTO_PATH_UNSAFE", asset.pk)
                    elif not path.is_file():
                        issue("PHOTO_MISSING", asset.pk)
                    elif path.stat().st_size != size:
                        issue("PHOTO_SIZE", asset.pk)
                    elif digest and hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                        issue("PHOTO_DIGEST", asset.pk)
        counts["operations"] = Operation.objects.count()
    return {"ok": not issues, "counts": counts, "issues": issues}
