from importlib import import_module
from django.db import migrations

source = import_module("apps.cases.migrations.0002_guards").FORWARD
SQL = source.split("CREATE FUNCTION public.reserve_case_insert_guard()", 1)[1].split(
    "CREATE TRIGGER case_insert_guard", 1
)[0]
SQL = "CREATE OR REPLACE FUNCTION public.reserve_case_insert_guard()" + SQL
SQL = SQL.replace(
    "IF TG_TABLE_NAME='cases_stockcaseproduct' THEN",
    """IF TG_TABLE_NAME='cases_stockcaseproduct' THEN
  IF NEW.is_calculated IS DISTINCT FROM (SELECT i.command_type='corrections.confirm' FROM public.cases_stockcase c JOIN public.core_operationintent i ON i.id=c.failed_intent_id WHERE c.id=NEW.case_id) THEN RAISE EXCEPTION 'Computed conflict must derive from a delivery correction intent' USING ERRCODE='23514'; END IF;""",
)


class Migration(migrations.Migration):
    dependencies = [("cases", "0006_computed_conflict_diagnostic")]
    operations = [migrations.RunSQL(SQL)]
