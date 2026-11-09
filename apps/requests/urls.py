from django.urls import path

from . import api, views

urlpatterns = [
    path("livraisons/signalements", views.issues_page, name="delivery-issues"),
    path("livraisons/<uuid:delivery_id>/signaler", views.report_error_page, name="delivery-error"),
    path("api/v1/delivery-issues", api.delivery_issues),
    path("api/v1/deliveries/<uuid:delivery_id>/report-error", api.report_error),
    path("api/v1/requests/<uuid:request_id>/claim", api.claim),
    path("api/v1/requests/<uuid:request_id>/refuse", api.refuse),
    path("demandes/<uuid:request_id>/refuser", views.refuse_page, name="request-refuse"),
    path("api/v1/requests/<uuid:request_id>/deliveries", api.deliveries),
    path("api/v1/request-catalog", api.catalog),
    path("api/v1/requests/pending-count", api.pending_count),
    path("demandes/nouvelle", views.create, name="request-create"),
    path("demandes/historique", views.history, name="request-history"),
    path("demandes", views.listing, name="requests"),
    path("demandes/<uuid:request_id>", views.detail, name="request-detail"),
    path("api/v1/requests", api.requests),
    path("api/v1/requests/history", api.history),
    path("api/v1/requests/<uuid:request_id>", api.detail),
    path("api/v1/requests/<uuid:request_id>/cancel", api.cancel),
    path("api/v1/requests/<uuid:request_id>/lines/<uuid:line_id>/photo", api.photo),
]
