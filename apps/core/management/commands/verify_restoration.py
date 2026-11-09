import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction

from apps.core.consistency import check_consistency
from apps.core.local_recovery import sha, table_snapshot, write_private


class Command(BaseCommand):
    help = "Atteste un contrôle réellement exécuté sur la base restaurée isolée."

    def add_arguments(self, parser):
        parser.add_argument("--backup-directory", required=True)

    def handle(self, *args, **options):
        if not getattr(settings, "M11_RESTORED", False):
            raise CommandError("ISOLATED_RESTORE_SETTINGS_REQUIRED")
        root = Path(options["backup_directory"])
        if sha(root / "backup.enc") != settings.M11_RECOVERY_RECORD["archive_sha256"]:
            raise CommandError("RESTORE_SOURCE_MISMATCH")
        result = check_consistency()
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            tables = table_snapshot(cursor)
        if tables != settings.M11_RECOVERY_RECORD["tables"]:
            raise CommandError("RESTORE_TABLE_CONTENT_MISMATCH")
        if not result["ok"]:
            self.stdout.write(json.dumps(result))
            raise CommandError("RESTORE_CONSISTENCY_FAILED")
        result.update(
            archive_sha256=settings.M11_RECOVERY_RECORD["archive_sha256"],
            database=settings.M11_RECOVERY_RECORD["database"],
            reconciliation_still_required=True,
            checked_at_utc=datetime.now(UTC).isoformat(),
        )
        write_private(root / f"restored-verified-{uuid.uuid4().hex}.json", result)
        self.stdout.write(json.dumps(result))
