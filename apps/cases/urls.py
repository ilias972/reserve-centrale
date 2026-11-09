from django.urls import path

from . import api, views

urlpatterns = [
    *[
        path(
            "api/v1/requests/<uuid:request_id>/" + a + "/preview",
            api.request_preview,
            {"action": a},
        )
        for a in ["close-remainder", "reopen", "reassign"]
    ],
    path("dossiers", views.listing, name="case-list"),
    path("dossiers/livraison/<uuid:delivery_id>", views.from_delivery, name="case-from-delivery"),
    path("dossiers/<uuid:case_id>", views.detail, name="case-detail"),
    path(
        "dossiers/<uuid:case_id>/verifier-manque",
        views.verify_shortage,
        name="case-verify-shortage",
    ),
    path(
        "dossiers/<uuid:case_id>/corriger",
        views.editor,
        {"mode": "correction"},
        name="case-correct",
    ),
    path(
        "dossiers/<uuid:case_id>/regulariser",
        views.editor,
        {"mode": "reconciliation"},
        name="case-reconcile",
    ),
    path(
        "demandes/<uuid:request_id>/administrer/<str:action>",
        views.request_editor,
        name="request-admin",
    ),
    path("api/v1/cases/<uuid:case_id>/review", api.review),
    path("api/v1/cases/<uuid:case_id>/resolve", api.resolve),
    path("api/v1/corrections/preview", api.correction, {"action": "preview"}),
    path("api/v1/corrections/confirm", api.correction, {"action": "confirm"}),
    path("api/v1/stock-reconciliations/preview", api.reconciliation, {"action": "preview"}),
    path("api/v1/stock-reconciliations/confirm", api.reconciliation, {"action": "confirm"}),
    *[
        path("api/v1/requests/<uuid:request_id>/" + a, api.request_action, {"action": a})
        for a in ["close-remainder", "reopen", "reassign"]
    ],
]
