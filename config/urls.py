from django.urls import include, path
from django.views.i18n import JavaScriptCatalog

from apps.core import api, i18n, search_api

javascript_catalog = JavaScriptCatalog.as_view(domain="djangojs")
javascript_catalog.m0_access = "public"
javascript_catalog.temporary_access = True

urlpatterns = [
    path("i18n/setlang/", i18n.set_language, name="set_language"),
    path("i18n/jsi18n/", javascript_catalog, name="javascript-catalog"),
    path("", include("apps.reporting.urls")),
    path("", include("apps.notifications.urls")),
    path("api/v1/search-suggestions", search_api.suggestions),
    path("manifest.webmanifest", api.web_manifest, name="web-manifest"),
    path("favicon.ico", api.favicon),
    path("", include("apps.purchasing.urls")),
    path("", include("apps.receiving.urls")),
    path("", include("apps.cases.urls")),
    path("", include("apps.requests.urls")),
    path("", include("apps.inventory.urls")),
    path("", include("apps.catalog.urls")),
    path("", include("apps.accounts.urls")),
    path("", api.live),
    path("health/live", api.live),
    path("health/ready", api.ready),
    path("api/v1/operation-intents", api.register),
    path("api/v1/operation-intents/<uuid:intent_id>", api.status),
    path("api/v1/operation-intents/<uuid:intent_id>/resolve", api.resolve),
]
