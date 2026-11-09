from django.apps import AppConfig


class CatalogConfig(AppConfig):
    name = "apps.catalog"

    def ready(self):
        from .services import ensure_registered

        ensure_registered()
