from django.core.management.base import BaseCommand

from apps.notifications.dispatcher import initialize


class Command(BaseCommand):
    help = "Active M9 sans rejouer les événements antérieurs ; aucun envoi externe."

    def handle(self, *args, **options):
        row = initialize()
        self.stdout.write(
            f"Notifications initialisées à {row.started_at.isoformat()} ; événements antérieurs exclus."
        )
