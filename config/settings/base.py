import os

from config.environment import BASE_DIR, required

SECRET_KEY = required("DJANGO_SECRET_KEY")
DEBUG = False
LOCAL_RECOVERY_ENABLED = os.environ.get("LOCAL_RECOVERY_ENABLED") == "1"
ALLOWED_HOSTS = []
INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.staticfiles",
    "apps.catalog",
    "apps.inventory",
    "apps.cases",
    "apps.requests",
    "apps.purchasing",
    "apps.receiving",
    "apps.accounts",
    "apps.core",
    "apps.audit",
    "apps.notifications",
    "apps.reporting",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "apps.core.middleware.ApiErrorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "apps.core.middleware.ExplicitLanguageMiddleware",
    "django.middleware.common.CommonMiddleware",
    "apps.core.upload_limits.PhotoUploadLimitMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "apps.accounts.middleware.AccountSessionMiddleware",
    "apps.core.middleware.DefaultDenyMiddleware",
]
ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"
AUTH_USER_MODEL = "accounts.User"
LANGUAGE_CODE = "en"
LANGUAGES = (("en", "English"), ("fr", "Français"), ("ar", "العربية"))
FORMAT_MODULE_PATH = ["config.formats"]
LOCALE_PATHS = [BASE_DIR / "locale"]
LANGUAGE_COOKIE_NAME = "reserve_language"
LANGUAGE_COOKIE_AGE = 60 * 60 * 24 * 365
LANGUAGE_COOKIE_SAMESITE = "Lax"
LANGUAGE_COOKIE_HTTPONLY = True
TIME_ZONE = "Europe/Paris"
USE_TZ = True
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / ".local" / "static"
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "apps.requests.navigation.navigation_counts",
                "apps.purchasing.navigation.navigation_counts",
                "apps.notifications.views.navigation_count",
            ]
        },
    }
]
STATICFILES_DIRS = [BASE_DIR / "static"]
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    "product_photos": {
        "BACKEND": "apps.catalog.storage.PrivatePhotoStorage",
        "OPTIONS": {
            "location": os.environ.get(
                "PRIVATE_PHOTO_ROOT", str(BASE_DIR / ".local" / "private" / "photos")
            ),
        },
    },
}
# Limites techniques de prototype P, substituables avant exploitation (A12/A14).
PHOTO_MAX_BYTES = 5 * 1024 * 1024
PHOTO_MAX_REQUEST_BYTES = PHOTO_MAX_BYTES + 256 * 1024
PHOTO_MAX_PIXELS = 20000000
PHOTO_MAX_DIMENSION = 8192
FILE_UPLOAD_MAX_MEMORY_SIZE = 1024 * 1024
PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.Argon2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
]
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 15},
    },
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]
# A10 OUVERT : valeurs de prototype, pas une politique approuvée par le centre.
SESSION_IDLE_SECONDS = int(os.environ.get("SESSION_IDLE_SECONDS", 30 * 86400))
SESSION_ABSOLUTE_SECONDS = int(os.environ.get("SESSION_ABSOLUTE_SECONDS", 90 * 86400))
ADMIN_SESSION_IDLE_SECONDS = int(os.environ.get("ADMIN_SESSION_IDLE_SECONDS", 7 * 86400))
ADMIN_SESSION_ABSOLUTE_SECONDS = int(os.environ.get("ADMIN_SESSION_ABSOLUTE_SECONDS", 30 * 86400))
ADMIN_RECENT_AUTH_SECONDS = int(os.environ.get("ADMIN_RECENT_AUTH_SECONDS", 900))
TEMPORARY_PASSWORD_SECONDS = int(os.environ.get("TEMPORARY_PASSWORD_SECONDS", 86400))
LOGIN_FAILURE_LIMIT = int(os.environ.get("LOGIN_FAILURE_LIMIT", 5))
LOGIN_WINDOW_SECONDS = int(os.environ.get("LOGIN_WINDOW_SECONDS", 900))
SESSION_EXPIRE_AT_BROWSER_CLOSE = False
SESSION_COOKIE_AGE = SESSION_IDLE_SECONDS
SESSION_COOKIE_NAME = "reserve_session"
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"
CSRF_FAILURE_VIEW = "apps.core.api.csrf_failure"
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"
DATA_UPLOAD_MAX_MEMORY_SIZE = 65536
EMAIL_BACKEND = "django.core.mail.backends.dummy.EmailBackend"
OPERATION_GENERATION = required("OPERATION_GENERATION")
OPERATION_INTENT_TTL_SECONDS = 900
OPERATION_LOCK_TIMEOUT_MS = 10000
OPERATION_STATEMENT_TIMEOUT_MS = 20000
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"json": {"()": "apps.core.logging.JsonFormatter"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "json"}},
    "loggers": {"reserve": {"handlers": ["console"], "level": "INFO", "propagate": False}},
}

# Les notifications externes sont désactivées par défaut.
NOTIFICATION_MAX_ATTEMPTS = 5
NOTIFICATION_LEASE_SECONDS = 60
NOTIFICATION_EXTERNAL_ALLOWED = os.environ.get("NOTIFICATION_EXTERNAL_ALLOWED") == "1"
NOTIFICATION_EMAIL_MODE = os.environ.get("NOTIFICATION_EMAIL_MODE", "CAPTURE")
NOTIFICATION_EMAIL_RECIPIENTS = {
    "RESPONSIBLE": os.environ.get("NOTIFICATION_RESPONSIBLE_EMAIL", ""),
    "ACCOUNTANT": os.environ.get("NOTIFICATION_ACCOUNTANT_EMAIL", ""),
}
NOTIFICATION_EMAIL_FROM = os.environ.get("NOTIFICATION_EMAIL_FROM", "")
NOTIFICATION_SMTP_HOST = os.environ.get("NOTIFICATION_SMTP_HOST", "")
NOTIFICATION_SMTP_PORT = int(os.environ.get("NOTIFICATION_SMTP_PORT", "587"))
NOTIFICATION_SMTP_USER = os.environ.get("NOTIFICATION_SMTP_USER", "")
NOTIFICATION_SMTP_PASSWORD = os.environ.get("NOTIFICATION_SMTP_PASSWORD", "")
NOTIFICATION_PUSH_ENABLED = os.environ.get("NOTIFICATION_PUSH_ENABLED") == "1"
WEBPUSH_PUBLIC_KEY = os.environ.get("WEBPUSH_PUBLIC_KEY", "")
WEBPUSH_PRIVATE_KEY_FILE = os.environ.get("WEBPUSH_PRIVATE_KEY_FILE", "")
WEBPUSH_CONTACT = os.environ.get("WEBPUSH_CONTACT", "")
# Mode de test à cible unique, désactivé par défaut.
PUSH_SINGLE_TARGET_TEST_MODE = os.environ.get("PUSH_SINGLE_TARGET_TEST_MODE") == "1"
PUSH_TEST_ALLOWED_SUBSCRIPTION_ID = os.environ.get("PUSH_TEST_ALLOWED_SUBSCRIPTION_ID", "")
PUSH_TEST_ALLOWED_USER_ID = os.environ.get("PUSH_TEST_ALLOWED_USER_ID", "")

# Limites techniques configurables ; à valider selon l’environnement.
REPORTING_MAX_ROWS = int(os.environ.get("REPORTING_MAX_ROWS", "10000"))
REPORTING_MAX_DAYS = int(os.environ.get("REPORTING_MAX_DAYS", "366"))
REPORTING_TIMEOUT_MS = int(os.environ.get("REPORTING_TIMEOUT_MS", "10000"))
REPORTING_MAX_BYTES = int(os.environ.get("REPORTING_MAX_BYTES", "20000000"))
REPORTING_EXPORT_MAX_ROWS = int(os.environ.get("REPORTING_EXPORT_MAX_ROWS", "20000"))
REPORTING_EXPORT_TIMEOUT_MS = int(os.environ.get("REPORTING_EXPORT_TIMEOUT_MS", "30000"))
REPORTING_MAX_CHARACTERS = int(os.environ.get("REPORTING_MAX_CHARACTERS", "4000000"))

REPORTING_MAX_CHOICES = int(os.environ.get("REPORTING_MAX_CHOICES", "10000"))
