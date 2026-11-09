"""Read-only inspection of an isolated restore; external delivery stays disabled."""

import json
import os
import re
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

from config.environment import database, load_local_environment

load_local_environment()
from .base import *  # noqa: E402,F403

try:
    recovery_root = Path(os.environ["RESERVE_RECOVERY_DIR"]).resolve()
    recovery = json.loads((recovery_root / "recovery.json").read_text())
    if not re.fullmatch(r"restore_m11_[a-z0-9_]{1,40}", recovery["database"]):
        raise ValueError("database")
except (KeyError, ValueError, OSError) as exc:
    raise ImproperlyConfigured("An identified isolated restore is required.") from exc

DEBUG = False
ALLOWED_HOSTS = ["localhost", "127.0.0.1"]
DATABASES = {"default": database("migration")}
if DATABASES["default"]["HOST"] not in {"127.0.0.1", "localhost", "::1"}:
    raise ImproperlyConfigured("A loopback-only database is required.")
DATABASES["default"]["NAME"] = recovery["database"]
DATABASES["default"]["OPTIONS"]["options"] = "-c default_transaction_read_only=on"
OPERATION_GENERATION = recovery["generation"]
M11_RESTORED = True
M11_RECOVERY_RECORD = recovery
STORAGES = {
    **STORAGES,  # noqa: F405
    "product_photos": {
        "BACKEND": "apps.catalog.storage.PrivatePhotoStorage",
        "OPTIONS": {"location": recovery_root / "photos"},
    },
}  # noqa: F405
NOTIFICATION_EXTERNAL_ALLOWED = False
NOTIFICATION_PUSH_ENABLED = False
LOCAL_RECOVERY_ENABLED = False
NOTIFICATION_EMAIL_MODE = "CAPTURE"
NOTIFICATION_SMTP_HOST = ""
NOTIFICATION_SMTP_PASSWORD = ""
WEBPUSH_PRIVATE_KEY_FILE = ""
EMAIL_BACKEND = "django.core.mail.backends.dummy.EmailBackend"
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
