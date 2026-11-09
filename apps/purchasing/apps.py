from django.apps import AppConfig


class PurchasingConfig(AppConfig):
    name = "apps.purchasing"

    def ready(self):
        from .services import ensure_registered

        ensure_registered()
        from apps.core.commands import register_fact_projection

        from .recommendations import stock_reactivations

        register_fact_projection(stock_reactivations)
