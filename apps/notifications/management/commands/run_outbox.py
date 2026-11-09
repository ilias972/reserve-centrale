import time
from functools import partial

from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections

from apps.notifications.worker import drain


class Command(BaseCommand):
    help = "Traite les notifications ; capture locale, transports externes interdits par défaut."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")
        parser.add_argument("--limit", type=int, default=100)
        parser.add_argument("--allow-external", action="store_true")
        parser.add_argument(
            "--work-item",
            help="Cibler un seul travail de notification (réservé à la recette A10).",
        )
        parser.add_argument(
            "--project-only",
            action="store_true",
            help="Projeter uniquement un événement métier ciblé, sans transport externe.",
        )
        parser.add_argument("--heartbeat-file")

    def handle(self, *args, **options):
        if not 1 <= options["limit"] <= 1000:
            raise CommandError("Limite de traitement : 1 à 1000.")
        from django.conf import settings

        if getattr(settings, "M11_RESTORED", False):
            raise CommandError(
                "Restauration isolée : worker suspendu jusqu’au rapprochement humain."
            )

        from apps.notifications.models import Activation

        if not Activation.objects.filter(pk=1).exists():
            raise CommandError("Exécutez initialize_notifications avant de démarrer le worker.")
        if options["allow_external"] and not settings.NOTIFICATION_EXTERNAL_ALLOWED:
            raise CommandError("Envois externes non autorisés par la configuration locale.")
        if options["project_only"]:
            if not options["work_item"] or not options["once"] or options["allow_external"]:
                raise CommandError(
                    "La projection ciblée exige --work-item --once, sans --allow-external."
                )
            if not getattr(settings, "PUSH_SINGLE_TARGET_TEST_MODE", False):
                raise CommandError("La projection ciblée est réservée à la recette A10 iPhone.")
            from apps.notifications.models import WorkItem
            from apps.notifications.worker import claim, perform

            item = WorkItem.objects.filter(pk=options["work_item"], kind="EVENT").first()
            if item is None:
                raise CommandError("Le travail ciblé n'est pas un événement à projeter.")
            attempt = claim(work_item_id=item.pk)
            if attempt is None:
                raise CommandError("L'événement ciblé n'est pas prêt à être projeté.")
            perform(attempt, allow_external=False)
            self.stdout.write("1 événement projeté ; aucun transport externe exécuté.")
            return
        if options["work_item"]:
            if not options["once"] or not options["allow_external"]:
                raise CommandError("Un travail ciblé exige --once et --allow-external.")
            if not getattr(settings, "PUSH_SINGLE_TARGET_TEST_MODE", False):
                raise CommandError("Le traitement ciblé est réservé à la recette A10 iPhone.")
            from apps.notifications.models import WorkItem
            from apps.notifications.worker import claim, perform

            item = (
                WorkItem.objects.select_related("subscription")
                .filter(pk=options["work_item"])
                .first()
            )
            if (
                item is None
                or item.kind != "PUSH"
                or item.subscription_id is None
                or str(item.subscription_id) != settings.PUSH_TEST_ALLOWED_SUBSCRIPTION_ID
                or str(item.subscription.user_id) != settings.PUSH_TEST_ALLOWED_USER_ID
            ):
                raise CommandError("Ce travail ne cible pas l'abonnement iPhone autorisé.")
            attempt = claim(work_item_id=item.pk)
            if attempt is None:
                raise CommandError("Le travail ciblé n'est pas prêt à être traité.")
            perform(attempt, allow_external=True)
            self.stdout.write("1 travail PUSH ciblé traité.")
            return
        if options["allow_external"] and getattr(settings, "PUSH_SINGLE_TARGET_TEST_MODE", False):
            raise CommandError(
                "En recette A10, indiquez un --work-item PUSH précis ; aucun traitement groupé externe n'est permis."
            )
        try:
            while True:
                if options["heartbeat_file"]:
                    from apps.core.diagnostics import heartbeat

                    heartbeat(options["heartbeat_file"])
                close_old_connections()
                progress = None
                if options["heartbeat_file"]:
                    progress = partial(heartbeat, options["heartbeat_file"])
                count = drain(
                    limit=options["limit"],
                    allow_external=options["allow_external"],
                    on_progress=progress,
                )
                if options["once"]:
                    self.stdout.write(f"{count} tentative(s) traitée(s).")
                    return
                time.sleep(1)
        except KeyboardInterrupt:
            self.stdout.write("Worker arrêté sans modification des faits métier.")
        finally:
            if options["heartbeat_file"]:
                from apps.core.diagnostics import heartbeat

                heartbeat(options["heartbeat_file"], state="STOPPED")
