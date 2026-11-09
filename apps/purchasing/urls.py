from django.urls import path

from . import api, views

urlpatterns = [
    path("api/v1/purchase-products", api.purchase_product),
    path("api/v1/recommendations/pending-count", api.pending_count),
    path("api/v1/recommendations", api.needs),
    path("api/v1/recommendations/<uuid:product_id>", api.needs),
    path(
        "api/v1/recommendations/<uuid:product_id>/defer",
        api.recommendation_action,
        {"action": "defer"},
    ),
    path(
        "api/v1/recommendations/<uuid:product_id>/reactivate",
        api.recommendation_action,
        {"action": "reactivate"},
    ),
    path("api/v1/orders", api.orders),
    path("api/v1/orders/<uuid:order_id>", api.order),
    path("api/v1/orders/<uuid:order_id>/cancel-remainder", api.cancel),
]

urlpatterns += [
    path("achats/besoins", views.needs, name="purchase-needs"),
    path("achats/besoins/<uuid:product_id>", views.need_detail, name="purchase-need"),
    path(
        "achats/besoins/<uuid:product_id>/reporter",
        views.editor,
        {"mode": "defer"},
        name="purchase-defer",
    ),
    path(
        "achats/besoins/<uuid:product_id>/reactiver",
        views.editor,
        {"mode": "reactivate"},
        name="purchase-reactivate",
    ),
    path("achats", views.orders, name="purchase-orders"),
    path("achats/enregistrer", views.editor, name="purchase-create"),
    path("achats/<uuid:order_id>", views.detail, name="purchase-order"),
    path(
        "achats/<uuid:order_id>/annuler-reliquat",
        views.editor,
        {"mode": "cancel"},
        name="purchase-cancel",
    ),
]
