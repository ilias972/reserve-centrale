from django.apps import AppConfig


class ReceivingConfig(AppConfig):
    name = "apps.receiving"

    def ready(self):
        from .services import ensure_registered

        ensure_registered()
