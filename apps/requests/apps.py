from django.apps import AppConfig


class RequestsConfig(AppConfig):
    name = "apps.requests"

    def ready(self):
        from .services import ensure_registered

        ensure_registered()
