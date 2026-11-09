"""Configuration explicite ; aucun secret ni code shell exécuté depuis .env."""

import json
import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent


def load_local_environment():
    path = BASE_DIR / ".env"
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or not key.replace("_", "").isalnum():
            raise ImproperlyConfigured("Ligne .env invalide.")
        if value.startswith('"'):
            value = json.loads(value)
        os.environ.setdefault(key, value)


def required(name):
    value = os.environ.get(name)
    if not value:
        raise ImproperlyConfigured(f"Paramètre obligatoire manquant : {name}.")
    return value


def database(role="runtime", sslmode="disable"):
    prefix = "RUNTIME_" if role == "runtime" else ""
    if role not in {"runtime", "migration"}:
        raise ImproperlyConfigured("DATABASE_ROLE doit valoir runtime ou migration.")
    return {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": required("DATABASE_NAME"),
        "USER": required(prefix + "DATABASE_USER"),
        "PASSWORD": required(prefix + "DATABASE_PASSWORD"),
        "HOST": required("DATABASE_HOST"),
        "PORT": required("DATABASE_PORT"),
        "CONN_MAX_AGE": 0,
        "ATOMIC_REQUESTS": False,
        "OPTIONS": {"sslmode": sslmode, "connect_timeout": 5},
        "TEST": {"NAME": os.environ.get("TEST_DATABASE_NAME", "test_stock_app_default")},
    }
