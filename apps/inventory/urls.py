from django.urls import path

from apps.cases import api as cases_api

from . import api, views

urlpatterns = [
    path("api/v1/stocks", api.stocks),
    path("api/v1/inventories/initial", api.initial),
    path("api/v1/stocks/adjustments", api.adjust),
    path("api/v1/stocks/<uuid:product_id>/photo", api.photo),
    path("api/v1/stock-movements", api.activity),
    path("api/v1/stock-movements/<uuid:entry_id>", api.activity),
    path("api/v1/cases", cases_api.cases),
    path("api/v1/cases/<uuid:case_id>", cases_api.cases),
    path("stocks", views.stocks, name="stocks"),
    path("stocks/inventaire-initial", views.stocks, {"initial": True}, name="initial-inventory"),
    path("stocks/mouvements", views.activity, name="stock-activity"),
    path("stocks/signalements", views.cases, name="stock-cases"),
    path("stocks/signalements/<uuid:case_id>", views.cases, name="stock-case"),
]
