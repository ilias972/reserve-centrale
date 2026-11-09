import os
from copy import deepcopy

from django.core.exceptions import ImproperlyConfigured

from config.environment import database, load_local_environment

load_local_environment()
from .base import *  # noqa: E402,F403

# Existing regression scenarios assert the original French UI. Dedicated i18n
# tests override this setting to check the English new-install default and RTL.
LANGUAGE_CODE = "fr"

ALLOWED_HOSTS = ["testserver", "localhost", "127.0.0.1"]
APPLICATION_DATABASE_USER = os.environ.get("DATABASE_USER", "")
APPLICATION_RUNTIME_DATABASE_USER = os.environ.get("RUNTIME_DATABASE_USER", "")
DATABASES = {"default": database("migration")}
TEST_DATABASE_USER = os.environ.get("TEST_DATABASE_USER", "")
TEST_DATABASE_PASSWORD = os.environ.get("TEST_DATABASE_PASSWORD", "")
if not TEST_DATABASE_USER or not TEST_DATABASE_PASSWORD:
    raise ImproperlyConfigured(
        "Tests require a dedicated TEST_DATABASE_USER and TEST_DATABASE_PASSWORD."
    )
if TEST_DATABASE_USER in {APPLICATION_DATABASE_USER, APPLICATION_RUNTIME_DATABASE_USER}:
    raise ImproperlyConfigured("The test role must differ from both application database roles.")
DATABASES["default"]["USER"] = TEST_DATABASE_USER
DATABASES["default"]["PASSWORD"] = TEST_DATABASE_PASSWORD
# Child test processes must reconnect to the isolated test database as its owner.
os.environ["DATABASE_USER"] = TEST_DATABASE_USER
os.environ["DATABASE_PASSWORD"] = TEST_DATABASE_PASSWORD
if DATABASES["default"]["NAME"] != "postgres":
    raise ImproperlyConfigured(
        "Tests require the isolated PostgreSQL maintenance database named postgres."
    )
if (
    not DATABASES["default"]["TEST"]["NAME"].startswith("test_stock_app_")
    or DATABASES["default"]["TEST"]["NAME"] == DATABASES["default"]["NAME"]
    or DATABASES["default"]["HOST"] not in {"localhost", "127.0.0.1", "::1"}
):
    raise ImproperlyConfigured(
        "Les tests doivent utiliser une base de test locale distincte, nommée test_stock_app_*."
    )
INSTALLED_APPS = [*INSTALLED_APPS, "tests.probes"]  # noqa: F405
ROOT_URLCONF = "tests.urls"
TEST_RUNNER = "tests.runner.EvidenceRunner"
LOGGING = deepcopy(LOGGING)  # noqa: F405
LOGGING["handlers"]["console"] = {"class": "logging.NullHandler"}
NOTIFICATION_EXTERNAL_ALLOWED = False
NOTIFICATION_PUSH_ENABLED = False
LOCAL_RECOVERY_ENABLED = True
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
NOTIFICATION_EMAIL_MODE = "CAPTURE"
NOTIFICATION_EMAIL_RECIPIENTS = {
    "RESPONSIBLE": "responsable@example.invalid",
    "ACCOUNTANT": "comptabilite@example.invalid",
}
NOTIFICATION_EMAIL_FROM = "no-reply@example.invalid"
