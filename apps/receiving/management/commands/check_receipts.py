"""Contrôle reproductible en lecture seule, jamais de réparation silencieuse."""

from django.core.management.base import BaseCommand
from django.db import connection, transaction

from apps.receiving.models import ArrivalReport, QuarantineItem, Receipt


class Command(BaseCommand):
    help = "Vérifie les reçus et unités isolées M8 sans modifier les données."

    def handle(self, *args, **options):
        counts = {}
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            for label, model, function in [
                ("reçus", Receipt, "reserve_m8_assert_receipt"),
                ("constats", ArrivalReport, "reserve_m8_assert_report"),
                ("isolations", QuarantineItem, "reserve_m8_assert_item"),
            ]:
                count = 0
                for identifier in (
                    model.objects.order_by("pk").values_list("pk", flat=True).iterator()
                ):
                    cursor.execute(f"SELECT public.{function}(%s)", [identifier])
                    count += 1
                counts[label] = count
        self.stdout.write(
            "M8 cohérent : "
            + ", ".join(f"{n} {label}" for label, n in counts.items())
            + ". Aucune donnée modifiée."
        )
