"""Génération XLSX interruptible : lecteur jetable, aucun worker persistant."""

import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.db import connection
from django.utils.translation import get_language, gettext

from apps.core.errors import BusinessError

from .filters import limit, refused


@contextmanager
def slot():
    # Deux générations simultanées par base ; libération avec la connexion en cas de crash.
    selected = None
    with connection.cursor() as cursor:
        for number in range(2):
            cursor.execute("SELECT pg_try_advisory_lock(74142026, %s)", [number])
            if cursor.fetchone()[0]:
                selected = number
                break
    if selected is None:
        raise refused(gettext("Two exports are already running. Try again shortly."))
    try:
        yield
    finally:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_unlock(74142026, %s)", [selected])


def worker_command():
    return [sys.executable, "-m", "apps.reporting.export_worker"]


def generate(actor, name, filters, *, started=None):
    started = time.monotonic() if started is None else started
    deadline = started + min(limit("EXPORT_TIMEOUT_MS", 30000), 30000) / 1000
    # Configuration résolue, y compris le nom de la base de test et les surcharges.
    configured = {
        key: getattr(settings, key)
        for key in [
            "INSTALLED_APPS",
            "AUTH_USER_MODEL",
            "DEFAULT_AUTO_FIELD",
            "TIME_ZONE",
            "USE_TZ",
            "LANGUAGE_CODE",
            "LANGUAGES",
            "OPERATION_GENERATION",
            "REPORTING_MAX_DAYS",
            "REPORTING_MAX_CHOICES",
            "REPORTING_MAX_CHARACTERS",
        ]
    }
    configured.update(
        SECRET_KEY=secrets.token_urlsafe(32),
        BASE_DIR=str(settings.BASE_DIR),
        DATABASES={"default": dict(connection.settings_dict)},
        REPORTING_EXPORT_READER=True,
        REPORTING_MAX_ROWS=min(limit("EXPORT_MAX_ROWS", 20000), 20000),
        REPORTING_EXPORT_MAX_ROWS=min(limit("EXPORT_MAX_ROWS", 20000), 20000),
        REPORTING_MAX_BYTES=min(limit("MAX_BYTES", 20000000), 20000000),
        REPORTING_TIMEOUT_MS=max(1, int((deadline - time.monotonic()) * 1000)),
        NOTIFICATION_EXTERNAL_ALLOWED=False,
        NOTIFICATION_PUSH_ENABLED=False,
        NOTIFICATION_EMAIL_MODE="CAPTURE",
        LOGGING_CONFIG=None,
    )
    database = configured["DATABASES"]["default"]
    database["CONN_MAX_AGE"] = 0
    database["DISABLE_SERVER_SIDE_CURSORS"] = True
    database["OPTIONS"] = {
        **database.get("OPTIONS", {}),
        "connect_timeout": 3,
        "options": "-c default_transaction_read_only=on -c statement_timeout=30000",
    }
    with slot(), tempfile.TemporaryDirectory(prefix="reserve-export-") as temporary:
        output = Path(temporary) / "result.xlsx"
        packet = {
            "settings": configured,
            "actor": str(actor.pk),
            "language": get_language() or settings.LANGUAGE_CODE,
            "name": name,
            "filters": filters.public(),
            "deadline": deadline,
            "output": str(output),
        }
        env = {"PATH": os.defpath, "PYTHONIOENCODING": "utf-8"}
        process = subprocess.Popen(  # noqa: S603 — commande interne fixe sans argument utilisateur.
            worker_command(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,  # noqa: S603 — commande interne fixe, aucun argument utilisateur.
            stderr=subprocess.DEVNULL,
            cwd=settings.BASE_DIR,
            env=env,
        )
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(process.args, 0)
            result, _ = process.communicate(
                json.dumps(packet, default=str).encode(), timeout=remaining
            )
        except subprocess.TimeoutExpired as exc:
            process.kill()
            process.communicate()  # Récolter le processus avant de libérer le fichier et le slot.
            raise refused(gettext("The export exceeds 30 seconds. Reduce the filters.")) from exc
        except BaseException:
            process.kill()
            process.communicate()
            raise
        if process.returncode or len(result) > 65536:
            raise refused(
                gettext("The export could not be created. Reduce the filters and try again.")
            )
        report = json.loads(result)
        if "error" in report:
            error = report["error"]
            raise BusinessError(error["code"], error["message"], status=error["status"])
        if not output.is_file() or output.stat().st_size > configured["REPORTING_MAX_BYTES"]:
            raise refused(gettext("The file is too large. Reduce the filters."))
        content = output.read_bytes()
        if time.monotonic() > deadline:
            raise refused(gettext("The export exceeds 30 seconds. Reduce the filters."))
        report["meta"]["extracted"] = datetime.fromisoformat(report["meta"]["extracted"])
        return content, report["meta"], report["rows"]
