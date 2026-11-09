from django.urls import path

from . import api, views

urlpatterns = [
    path("api/v1/catalog", api.catalog),
    path("api/v1/catalog/<uuid:product_id>", api.catalog),
    path("api/v1/products", api.products),
    path("api/v1/products/<uuid:product_id>", api.product),
    path("api/v1/products/<uuid:product_id>/activate", api.activate),
    path("api/v1/products/<uuid:product_id>/archive", api.archive),
    path("api/v1/products/<uuid:product_id>/floor-permissions", api.permissions),
    path("api/v1/products/<uuid:product_id>/supplier-links", api.suppliers_for_product),
    path("api/v1/products/<uuid:product_id>/photos", api.upload),
    path("api/v1/products/<uuid:product_id>/photo", api.photo),
    path("api/v1/products/<uuid:product_id>/history", api.history),
]
for resource, view in [
    ("floors", api.floors),
    ("categories", api.categories),
    ("suppliers", api.suppliers),
]:
    urlpatterns.extend(
        [path(f"api/v1/{resource}", view), path(f"api/v1/{resource}/<uuid:reference_id>", view)]
    )

urlpatterns.extend(
    [
        path("catalogue", views.products, name="catalog-products"),
        path("catalogue/creer", views.edit_product, name="product-create"),
        path("catalogue/<uuid:product_id>", views.detail, name="product-detail"),
        path("catalogue/<uuid:product_id>/modifier", views.edit_product, name="product-edit"),
        path("catalogue/<uuid:product_id>/photographie", views.upload, name="product-upload"),
        path("catalogue/<uuid:product_id>/droits", views.permissions, name="product-permissions"),
        path(
            "catalogue/<uuid:product_id>/fournisseurs",
            views.supplier_links,
            name="product-suppliers",
        ),
        path("catalogue/<uuid:product_id>/historique", views.history, name="product-history"),
        path(
            "catalogue/<uuid:product_id>/archiver",
            views.transition,
            {"action": "archive"},
            name="product-archive",
        ),
        path(
            "catalogue/<uuid:product_id>/activer",
            views.transition,
            {"action": "activate"},
            name="product-activate",
        ),
        path("configuration/<str:kind>", views.references, name="catalog-references"),
        path(
            "configuration/<str:kind>/creer", views.edit_reference, name="catalog-reference-create"
        ),
        path(
            "configuration/<str:kind>/<uuid:reference_id>",
            views.edit_reference,
            name="catalog-reference-edit",
        ),
    ]
)
