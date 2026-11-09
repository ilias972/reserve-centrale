from django.core.management.base import BaseCommand
from django.db import connection, transaction

from apps.catalog.models import Product
from apps.inventory.models import StockEntry


class Command(BaseCommand):
    help = "Contrôle en lecture seule la reconstruction des soldes et les chaînes du registre ; aucune réparation."

    def handle(self, *args, **options):
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            count = 0
            for identifier in (
                Product.objects.order_by("pk").values_list("pk", flat=True).iterator()
            ):
                cursor.execute("SELECT public.reserve_assert_stock(%s)", [identifier])
                cursor.execute("SELECT public.reserve_assert_reservations(%s)", [identifier])
                count += 1
            from apps.requests.models import InternalRequest

            for identifier in InternalRequest.objects.values_list("pk", flat=True).iterator():
                cursor.execute("SELECT public.reserve_assert_request(%s)", [identifier])
            entries = StockEntry.objects.count()
        self.stdout.write(
            f"Registre cohérent : {count} produit(s), {entries} écriture(s). Aucune donnée modifiée."
        )
