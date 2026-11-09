from django.conf import settings
from django.db import connection


def configure_transaction():
    if connection.vendor != "postgresql" or not connection.in_atomic_block:
        raise RuntimeError("Une transaction PostgreSQL explicite est obligatoire.")
    with connection.cursor() as cursor:
        for name, value in (
            ("lock_timeout", settings.OPERATION_LOCK_TIMEOUT_MS),
            ("statement_timeout", settings.OPERATION_STATEMENT_TIMEOUT_MS),
        ):
            cursor.execute("SELECT set_config(%s, %s, true)", [name, f"{int(value)}ms"])


def database_now():
    with connection.cursor() as cursor:
        cursor.execute("SELECT clock_timestamp()")
        return cursor.fetchone()[0]
