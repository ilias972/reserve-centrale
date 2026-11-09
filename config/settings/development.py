import os

from config.environment import database, load_local_environment

load_local_environment()
from .base import *  # noqa: E402,F403

DATABASES = {"default": database("migration")}
ALLOWED_HOSTS = [
    host.strip()
    for host in os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")
    if host.strip()
]
DEBUG = os.environ.get("DJANGO_DEBUG", "0") == "1"
SESSION_COOKIE_SECURE = False
CSRF_COOKIE_SECURE = False
LANGUAGE_COOKIE_SECURE = False
NOTIFICATION_EXTERNAL_ALLOWED = False
NOTIFICATION_PUSH_ENABLED = False
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
