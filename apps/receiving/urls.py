from django.urls import path

from . import api, views

urlpatterns = [
    path("api/v1/receiving/products", api.product),
    path("api/v1/receiving/orders", api.orders),
    path("api/v1/receiving/orders/<uuid:order_id>", api.orders),
    path("api/v1/receiving/orders/<uuid:order_id>/lines/<uuid:line_id>/photo", api.photo),
    path("api/v1/receipts", api.receipts),
    path("api/v1/receipts/<uuid:receipt_id>", api.receipts),
    path("api/v1/receipts/<uuid:receipt_id>/reports", api.report_error),
    path("api/v1/orders/<uuid:order_id>/receipts", api.receive),
    path("api/v1/cases/unknown-order", api.unknown),
    path("api/v1/cases/<uuid:case_id>/link-order", api.link_order),
]
for action in ["late-delivery-reports", "surplus-reports", "reinstate-remainder", "reminders"]:
    urlpatterns.append(
        path(f"api/v1/orders/<uuid:order_id>/{action}", api.order_action, {"action": action})
    )

for action in ["accept-surplus", "refuse"]:
    urlpatterns.append(
        path(f"api/v1/quarantine/<uuid:item_id>/{action}", api.quarantine, {"action": action})
    )


urlpatterns += [
    path("receptions", views.listing, name="receiving-orders"),
    path("receptions/historique", views.records, name="receiving-records"),
    path("receptions/signalements", views.cases, name="receiving-cases"),
    path(
        "receptions/commande-introuvable",
        views.editor,
        {"mode": "unknown"},
        name="receiving-unknown",
    ),
    path("receptions/commandes/<uuid:order_id>", views.order_detail, name="receiving-order"),
    path("receptions/recus/<uuid:receipt_id>", views.receipt_detail, name="receiving-receipt"),
    path("receptions/dossiers/<uuid:case_id>", views.case_detail, name="receiving-case"),
]
for action, mode in [
    ("recevoir", "receive"),
    ("reliquat-arrive", "late"),
    ("surplus", "extra"),
    ("rappel", "reminder"),
]:
    urlpatterns.append(
        path(
            f"receptions/commandes/<uuid:order_id>/{action}",
            views.editor,
            {"mode": mode},
            name=f"receiving-{mode}",
        )
    )
for mode in ["link", "resolve", "reinstate", "correction", "review"]:
    urlpatterns.append(
        path(
            f"receptions/dossiers/<uuid:case_id>/{mode}",
            views.editor,
            {"mode": mode},
            name=f"receiving-{mode}",
        )
    )
for mode in ["report_error", "open_receipt_case"]:
    urlpatterns.append(
        path(
            f"receptions/recus/<uuid:receipt_id>/{mode}",
            views.editor,
            {"mode": mode},
            name=f"receiving-{mode}",
        )
    )
for mode in ["accept", "refuse"]:
    urlpatterns.append(
        path(
            f"receptions/isolation/<uuid:item_id>/{mode}",
            views.editor,
            {"mode": mode},
            name=f"receiving-{mode}",
        )
    )
