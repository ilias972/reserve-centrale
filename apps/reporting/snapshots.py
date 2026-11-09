import time
from contextlib import contextmanager

from django.db import OperationalError, connection, transaction
from django.utils.translation import gettext

from apps.core.policies import check_role, lock_actor

from .filters import limit, refused


def authorize(context, personal=False):
    with transaction.atomic():
        actor = lock_actor(context)
        if not personal:
            check_role(actor, {"ADMIN"})
        return actor


@contextmanager
def snapshot():
    # Première commande de la transaction ; aucun verrou d'acteur dans read-only.
    try:
        with transaction.atomic(durable=True):
            with connection.cursor() as c:
                c.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                c.execute(
                    "SELECT set_config('statement_timeout', %s, true)",
                    [str(limit("TIMEOUT_MS", 10000))],
                )
                c.execute("SELECT set_config('work_mem', '4MB', true)")
                c.execute("SELECT transaction_timestamp(), txid_current_snapshot()::text")
                extracted, version = c.fetchone()
            yield {"extracted": extracted, "snapshot": version, "timezone": "Europe/Paris"}
    except OperationalError as exc:
        if getattr(exc.__cause__, "sqlstate", None) == "57014":
            raise refused(
                gettext("The calculation is taking too long. Reduce the period or filters.")
            ) from exc
        raise


def materialize(iterator, *, max_rows=None):
    started = time.monotonic()
    result = []
    characters = 0
    for row in iterator:
        if len(result) >= (limit("MAX_ROWS", 10000) if max_rows is None else max_rows):
            raise refused(
                gettext("Too many rows. Reduce the filters; no partial file will be created.")
            )
        characters += sum(len(str(c)) for c in row["cells"] if c is not None)
        if characters > limit("MAX_CHARACTERS", 4000000):
            raise refused(gettext("The data is too large. Reduce the filters."))
        if time.monotonic() - started > limit("TIMEOUT_MS", 10000) / 1000:
            raise refused(gettext("The calculation exceeds the time limit. Reduce the filters."))
        result.append(row)
    return result
