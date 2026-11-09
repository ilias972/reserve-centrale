from config.environment import database, load_local_environment

load_local_environment()
from .base import *  # noqa: E402,F403

DATABASES = {"default": database("migration")}
ALLOWED_HOSTS = ["localhost", "127.0.0.1"]
NOTIFICATION_EXTERNAL_ALLOWED = False
NOTIFICATION_PUSH_ENABLED = False
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
