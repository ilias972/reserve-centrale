from django.apps import AppConfig


class ReportingConfig(AppConfig):
    name = "apps.reporting"

    def ready(self):
        from . import exports

        exports.ensure_registered()
