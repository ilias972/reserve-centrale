import json

from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError

from apps.core.consistency import check_consistency


class Command(BaseCommand):
    help = "Vérifie les invariants métier et les photos, sans correction ni écriture."

    def handle(self, *args, **options):
        try:
            report = check_consistency()
        except DatabaseError:
            raise CommandError("TECHNICAL_DATABASE_UNAVAILABLE_OR_TIMEOUT") from None
        self.stdout.write(json.dumps(report, ensure_ascii=False))
        if not report["ok"]:
            raise CommandError("CONSISTENCY_FAILED : consulter les règles et objets ci-dessus.")
