from django.apps import AppConfig


class InventoryConfig(AppConfig):
    name = "apps.inventory"

    def ready(self):
        from .services import ensure_registered

        ensure_registered()
