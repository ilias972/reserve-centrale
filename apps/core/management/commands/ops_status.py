import json

from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError

from apps.core.diagnostics import operational_status


class Command(BaseCommand):
    help = "Diagnostic local sans secrets ; code 1 en cas d'incident ou de vérification manquante."

    def add_arguments(self, parser):
        parser.add_argument("--heartbeat-file")
        parser.add_argument("--backup-directory")
        parser.add_argument("--max-outbox-age", type=int, default=300)
        parser.add_argument("--max-backup-age", type=int, default=172800)

    def handle(self, *args, **options):
        if (
            not 1 <= options["max_outbox_age"] <= 86400
            or not 1 <= options["max_backup_age"] <= 2678400
        ):
            raise CommandError("OUTBOX_AGE_LIMIT")
        try:
            result = operational_status(
                heartbeat_file=options["heartbeat_file"],
                backup_directory=options["backup_directory"],
                max_age=options["max_outbox_age"],
                max_backup_age=options["max_backup_age"],
            )
        except DatabaseError:
            raise CommandError("TECHNICAL_DATABASE_UNAVAILABLE_OR_TIMEOUT") from None
        self.stdout.write(json.dumps(result))
        if not result["ok"]:
            raise CommandError("OPERATIONAL_CHECK_REQUIRED")
