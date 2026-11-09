"""État technique local expurgé, distinct des conflits métier attendus."""

import json
import os
from datetime import UTC, datetime
from pathlib import Path

from django.db import connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.db.models import Count, Min
from django.utils import timezone

from apps.notifications.models import Activation, Outbox, WorkItem


def heartbeat(path, *, state="RUNNING"):
    path = Path(path)
    if path.is_symlink() or not path.parent.is_dir() or path.parent.stat().st_mode & 0o077:
        raise ValueError("HEARTBEAT_PRIVATE_DIRECTORY_REQUIRED")
    temp = path.with_name(path.name + "." + str(os.getpid()) + ".tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(
                {"pid": os.getpid(), "at": datetime.now(UTC).isoformat(), "state": state}, stream
            )
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def operational_status(
    *, heartbeat_file=None, backup_directory=None, max_age=300, max_backup_age=172800
):
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        cursor.execute("SET LOCAL statement_timeout = '5s'")
        executor = MigrationExecutor(connection)
        pending = bool(executor.migration_plan(executor.loader.graph.leaf_nodes()))
        queue, oldest, expired, dead, activation, unprojected_count = [], None, 0, 0, None, 0
        if not pending:
            queue = list(WorkItem.objects.values("state").annotate(count=Count("pk")))
            oldest = WorkItem.objects.filter(state__in=["PENDING", "RETRY"]).aggregate(
                at=Min("created_at")
            )["at"]
            expired = WorkItem.objects.filter(
                state="CLAIMED", lease_until__lt=timezone.now()
            ).count()
            dead = WorkItem.objects.filter(state="DEAD").count()
            activation = Activation.objects.filter(pk=1).first()
            unprojected = (
                Outbox.objects.none()
                if activation is None
                else Outbox.objects.filter(created_at__gte=activation.started_at).exclude(
                    workitem__kind="EVENT"
                )
            )
            unprojected_count = unprojected.count()
            unprojected_oldest = unprojected.aggregate(at=Min("created_at"))["at"]
            dates = [date for date in [oldest, unprojected_oldest] if date is not None]
            oldest = min(dates) if dates else None
    worker = {"status": "UNKNOWN", "reason": "NO_HEARTBEAT_CONFIGURED"}
    if heartbeat_file:
        try:
            value = json.loads(Path(heartbeat_file).read_text())
            age = (datetime.now(UTC) - datetime.fromisoformat(value["at"])).total_seconds()
            os.kill(int(value["pid"]), 0)
            worker = {
                "status": "RUNNING"
                if value["state"] == "RUNNING" and 0 <= age <= 10
                else "STALE_OR_STOPPED",
                "age_seconds": round(age, 3),
            }
        except (OSError, ValueError, KeyError, TypeError):
            worker = {"status": "UNAVAILABLE"}
    backup = {"status": "NOT_CONFIGURED"}
    if backup_directory:
        from apps.core.local_recovery import sha

        root = Path(backup_directory)
        try:
            manifest = json.loads((root / "manifest.json").read_text())
            intact = sha(root / "backup.enc") == manifest["archive_sha256"]
            attestations = []
            for path in root.glob("restored-verified-*.json"):
                report = json.loads(path.read_text())
                if report.get("archive_sha256") == manifest["archive_sha256"] and report.get("ok"):
                    attestations.append(path.name)
            backup = {
                "status": "RESTORED_VERIFIED"
                if intact and attestations
                else "INTEGRITY_ONLY"
                if intact
                else "ALTERED",
                "created_at": manifest["created_at"],
                "age_seconds": round(
                    (
                        datetime.now(UTC) - datetime.fromisoformat(manifest["created_at"])
                    ).total_seconds(),
                    3,
                ),
            }
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            backup = {"status": "INCOMPLETE"}
    age = round((timezone.now() - oldest).total_seconds(), 3) if oldest else 0
    warnings = []
    if pending:
        warnings.append("MIGRATIONS_PENDING")
    if activation is None and not pending:
        warnings.append("NOTIFICATIONS_NOT_INITIALIZED")
    if age > max_age:
        warnings.append("OUTBOX_OLD")
    if expired:
        warnings.append("LEASE_EXPIRED")
    if dead:
        warnings.append("DELIVERY_PERMANENT_FAILURE")
    if worker["status"] != "RUNNING":
        warnings.append("WORKER_NOT_CONFIRMED")
    if backup["status"] != "RESTORED_VERIFIED":
        warnings.append("BACKUP_NOT_RESTORE_VERIFIED")
    if backup.get("age_seconds", 0) > max_backup_age:
        warnings.append("BACKUP_OLD")
    return {
        "ok": not warnings,
        "database": "AVAILABLE",
        "migrations_pending": pending,
        "queue": queue,
        "unprojected_events": unprojected_count,
        "notifications_initialized": None if pending else activation is not None,
        "oldest_pending_seconds": age,
        "expired_leases": expired,
        "dead_jobs": dead,
        "worker": worker,
        "backup": backup,
        "warnings": warnings,
    }
