import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.production")
django_application = get_asgi_application()

from apps.core.upload_limits import PhotoUploadBodyLimitASGI  # noqa: E402

application = PhotoUploadBodyLimitASGI(django_application)
