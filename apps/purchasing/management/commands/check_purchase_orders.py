"""Vérification M7 en lecture seule ; aucune correction automatique."""

from django.core.management.base import BaseCommand
from django.db import connection, transaction

from apps.purchasing.models import OrderLine, PurchaseOrder


class Command(BaseCommand):
    help = "Contrôle les quantités et faits d’achat M7, sans modifier les données."

    def handle(self, *args, **options):
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            count = 0
            for identifier in (
                PurchaseOrder.objects.order_by("pk").values_list("pk", flat=True).iterator()
            ):
                cursor.execute("SELECT public.reserve_m7_assert_order(%s)", [identifier])
                count += 1
            lines = OrderLine.objects.count()
        self.stdout.write(
            f"Achats cohérents : {count} commande(s), {lines} ligne(s). Qf=F+A+W. Aucune donnée modifiée."
        )
