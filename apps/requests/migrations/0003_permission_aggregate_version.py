from importlib import import_module
from django.db import migrations

base = import_module("apps.requests.migrations.0002_reservation_integrity")
guard = base.FORWARD.split("CREATE FUNCTION public.reserve_request_guard()", 1)[1].split(
    "CREATE TRIGGER request_guard", 1
)[0]
sql = "CREATE OR REPLACE FUNCTION public.reserve_request_guard()" + guard


class Migration(migrations.Migration):
    dependencies = [("requests", "0002_reservation_integrity")]
    operations = [migrations.RunSQL(sql, migrations.RunSQL.noop)]
