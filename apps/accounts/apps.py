from django.apps import AppConfig


class AccountsConfig(AppConfig):
    name = "apps.accounts"

    def ready(self):
        from . import sessions  # noqa: F401
        from .operations import ensure_registered

        ensure_registered()
