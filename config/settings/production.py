"""Configuration stricte préparée, sans déploiement ni service institutionnel."""

import os

from django.core.exceptions import ImproperlyConfigured

from config.environment import database, required

from .base import *  # noqa: F403

if len(SECRET_KEY) < 50:  # noqa: F405
    raise ImproperlyConfigured("DJANGO_SECRET_KEY de production trop court.")
ALLOWED_HOSTS = required("DJANGO_ALLOWED_HOSTS").split(",")
DATABASES = {"default": database("runtime", sslmode="verify-full")}
DATABASES["default"]["OPTIONS"]["sslrootcert"] = required("DATABASE_SSLROOTCERT")
SESSION_COOKIE_SECURE = True
SESSION_COOKIE_NAME = "__Host-reserve_session"
CSRF_COOKIE_SECURE = True
LANGUAGE_COOKIE_SECURE = True
SECURE_SSL_REDIRECT = True
SECURE_HSTS_SECONDS = int(os.environ.get("SECURE_HSTS_SECONDS", "0"))
