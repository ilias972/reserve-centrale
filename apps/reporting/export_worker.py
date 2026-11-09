"""Entrée interne, configuration reçue par pipe privé, aucune lecture du .env."""

import json
import os
import sys
import time

from django.utils import translation
from django.utils.translation import gettext


def main():
    packet = json.load(sys.stdin)
    from django.conf import settings

    settings.configure(**packet["settings"])
    import django

    django.setup()
    from django.db import connection, connections

    from apps.accounts.models import User
    from apps.core.errors import BusinessError
    from apps.core.policies import check_role

    from . import exports, selectors
    from .filters import parse, refused
    from .snapshots import materialize, snapshot

    translation.activate(packet.get("language", settings.LANGUAGE_CODE))

    def bounded(execute, sql, params, many, context):
        remaining = int((packet["deadline"] - time.monotonic()) * 1000)
        if remaining <= 0:
            raise refused(gettext("The export exceeds 30 seconds. Reduce the filters."))
        # Chaque requête a au plus le temps restant, pas trente secondes renouvelables.
        if not sql.startswith(("SELECT set_config", "SET TRANSACTION")):
            execute(
                "SELECT set_config('statement_timeout', %s, false)",
                [str(remaining)],
                False,
                context,
            )
        return execute(sql, params, many, context)

    try:
        with connection.execute_wrapper(bounded):
            actor = User.objects.get(pk=packet["actor"], is_active=True)
            check_role(actor, {"ADMIN"})
            name, f = packet["name"], parse(packet["filters"])
            with snapshot() as meta:
                fields = selectors.validate_filters(actor, name, f)
                meta["filter_labels"] = {
                    field["label"]: next(
                        (label for value, label in field["choices"] if value == field["selected"]),
                        gettext("All"),
                    )
                    for field in fields
                    if field["selected"]
                }
                meta["filter_labels"][gettext("Search")] = f.q or gettext("None")
                rows = materialize(selectors.rows(actor, name, f))
            content = exports.workbook(name, rows, f, meta)
            with open(packet["output"], "xb") as output:
                os.chmod(packet["output"], 0o600)
                output.write(content)
        print(json.dumps({"rows": len(rows), "meta": meta}, default=str))
    except BusinessError as exc:
        print(
            json.dumps({"error": {"code": exc.code, "message": exc.message, "status": exc.status}})
        )
    finally:
        connections.close_all()


if __name__ == "__main__":
    main()
