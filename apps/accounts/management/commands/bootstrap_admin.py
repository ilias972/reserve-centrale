import os

from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from apps.accounts.services import bootstrap_admin
from apps.core.errors import BusinessError


class Command(BaseCommand):
    help = "Explicitly initialize or replace the manager account as the local database owner."

    def add_arguments(self, parser):
        parser.add_argument("--username", required=True)
        parser.add_argument("--display-name", required=True)
        parser.add_argument("--confirm-local-test", action="store_true", required=True)
        parser.add_argument("--replace", type=str)
        parser.add_argument("--reason")

    def handle(self, *args, **options):
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_user")
            owner = cursor.fetchone()[0]
        if (
            connection.settings_dict["HOST"] not in {"127.0.0.1", "localhost", "::1"}
            or owner != os.environ.get("DATABASE_USER")
            or owner == os.environ.get("RUNTIME_DATABASE_USER")
        ):
            raise CommandError(
                "This command is restricted to the local database owner, never runtime."
            )
        try:
            user, temporary = bootstrap_admin(
                username=options["username"],
                display_name=options["display_name"],
                replace=options["replace"],
                reason=options["reason"],
            )
        except (BusinessError, ValueError) as exc:
            raise CommandError(getattr(exc, "message", "Initialisation refusée.")) from exc
        self.stdout.write(f"Gestionnaire créé : {user.username} ({user.pk})")
        self.stdout.write(f"Mot de passe temporaire — affichage unique : {temporary}")
        self.stdout.write("À modifier à la première connexion. Ne pas archiver cette sortie.")
