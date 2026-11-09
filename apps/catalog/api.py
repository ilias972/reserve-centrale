from django.db import transaction
from django.http import FileResponse, JsonResponse

from apps.core.api import actor_context, endpoint, json_body
from apps.core.idempotency import execute_intent, register_intent
from apps.core.payloads import invalid_input, parse_uuid, payload_hash
from apps.core.policies import lock_actor

from .images import upload_photo
from .models import Category, Floor, ProductPhoto, Supplier
from .policies import catalog_admin
from .selectors import (
    catalog_list,
    get_product,
    parameter_history,
    permission_data,
    product_data,
    supplier_link_data,
)
from .services import ensure_registered, reference_data
from .storage import photo_storage


def perform(request, kind, payload, resource_id=None, *, identifier=None, key=None):
    ensure_registered()
    context = actor_context(request)
    identifier = identifier or request.headers.get("Operation-Intent")
    key = key or request.headers.get("Idempotency-Key")
    register_intent(
        context,
        intent_id=identifier,
        key=key,
        fingerprint=payload_hash(payload),
        command_type=kind,
        resource_id=resource_id,
    )
    return execute_intent(
        context,
        intent_id=identifier,
        key=key,
        payload=payload,
        correlation_id=request.correlation_id,
    )


@endpoint(access="authenticated", methods=["GET"])
def catalog(request, product_id=None):
    with transaction.atomic():
        actor = lock_actor(actor_context(request))
        if product_id:
            return JsonResponse({"product": product_data(get_product(actor, product_id), actor)})
        rows, next_offset = catalog_list(actor, request.GET)
        return JsonResponse(
            {
                "products": [product_data(row, actor) for row in rows],
                "next_offset": next_offset,
                "request_module_available": True,
                "stock_module_available": True,
            }
        )


@endpoint(access="authenticated", methods=["GET", "POST"])
def products(request):
    catalog_admin(request.user)
    if request.method == "POST":
        return perform(request, "catalog.product.create", json_body(request))
    return catalog(request)


@endpoint(access="authenticated", methods=["GET", "PATCH"])
def product(request, product_id):
    catalog_admin(request.user)
    if request.method == "PATCH":
        return perform(request, "catalog.product.update", json_body(request), product_id)
    with transaction.atomic():
        actor = lock_actor(actor_context(request))
        catalog_admin(actor)
        row = get_product(actor, product_id)
        return JsonResponse(
            {
                "product": product_data(row),
                "permissions": [
                    permission_data(p) for p in row.floor_permissions.order_by("floor_id")
                ],
                "suppliers": [
                    supplier_link_data(p) for p in row.supplier_links.order_by("supplier_id")
                ],
            }
        )


@endpoint(access="authenticated", methods=["POST"])
def activate(request, product_id):
    return perform(request, "catalog.product.activate", json_body(request), product_id)


@endpoint(access="authenticated", methods=["POST"])
def archive(request, product_id):
    return perform(request, "catalog.product.archive", json_body(request), product_id)


@endpoint(access="authenticated", methods=["PUT"])
def permissions(request, product_id):
    return perform(request, "catalog.product.permissions", json_body(request), product_id)


@endpoint(access="authenticated", methods=["PUT"])
def suppliers_for_product(request, product_id):
    return perform(request, "catalog.product.suppliers", json_body(request), product_id)


def reference_view(model, kind):
    @endpoint(access="authenticated", methods=["GET", "POST", "PATCH"])
    def view(request, reference_id=None):
        catalog_admin(request.user)
        if request.method == "GET":
            with transaction.atomic():
                actor = lock_actor(actor_context(request))
                catalog_admin(actor)
                query = model.objects.order_by("name", "id")
                if reference_id:
                    from django.shortcuts import get_object_or_404

                    return JsonResponse(
                        {"reference": reference_data(get_object_or_404(query, pk=reference_id))}
                    )
                try:
                    offset = int(request.GET.get("offset", 0))
                    if not 0 <= offset <= 100000:
                        raise ValueError
                except ValueError as exc:
                    raise invalid_input() from exc
                rows = list(query[offset : offset + 51])
                return JsonResponse(
                    {
                        "references": [reference_data(row) for row in rows[:50]],
                        "next_offset": offset + 50 if len(rows) > 50 else None,
                    }
                )
        if (request.method == "POST" and reference_id) or (
            request.method == "PATCH" and reference_id is None
        ):
            raise invalid_input()
        return perform(
            request,
            f"catalog.{kind}.{'create' if request.method == 'POST' else 'update'}",
            json_body(request),
            reference_id,
        )

    return view


floors = reference_view(Floor, "floor")
categories = reference_view(Category, "category")
suppliers = reference_view(Supplier, "supplier")


@endpoint(access="authenticated", methods=["POST"])
def upload(request, product_id):
    catalog_admin(request.user)
    if set(request.POST) - {"expected_version"} or set(request.FILES) != {"photo"}:
        raise invalid_input()
    try:
        version = int(request.POST["expected_version"])
    except (KeyError, ValueError) as exc:
        raise invalid_input() from exc
    return upload_photo(
        request,
        product_id,
        request.FILES["photo"],
        expected_version=version,
        identifier=request.headers.get("Operation-Intent"),
        key=request.headers.get("Idempotency-Key"),
    )


@endpoint(access="authenticated", methods=["GET"])
def photo(request, product_id):
    with transaction.atomic():
        actor = lock_actor(actor_context(request))
        row = get_product(actor, product_id)
        return serve_photo(request, actor, row)


def serve_photo(request, actor, row):
    identifier = (
        parse_uuid(request.GET["version"]) if request.GET.get("version") else row.current_photo_id
    )
    if identifier is None or (actor.role != "ADMIN" and identifier != row.current_photo_id):
        from django.http import Http404

        raise Http404
    from django.shortcuts import get_object_or_404

    version = get_object_or_404(
        ProductPhoto.objects.select_related("asset"), pk=identifier, product=row
    )
    key = (
        version.asset.thumbnail_key
        if request.GET.get("thumbnail") == "1"
        else version.asset.object_key
    )
    try:
        stream = photo_storage().open(key, "rb")
    except FileNotFoundError:
        from django.http import Http404

        raise Http404 from None
    response = FileResponse(
        stream, content_type="image/jpeg", filename="produit.jpg", as_attachment=False
    )
    response["X-Content-Type-Options"] = "nosniff"
    return response


@endpoint(access="authenticated", methods=["GET"])
def history(request, product_id):
    with transaction.atomic():
        actor = lock_actor(actor_context(request))
        catalog_admin(actor)
        row = get_product(actor, product_id)
        try:
            offset = int(request.GET.get("offset", 0))
            if not 0 <= offset <= 100000:
                raise ValueError
        except ValueError as exc:
            raise invalid_input() from exc
        items = list(parameter_history(row)[offset : offset + 51])
        return JsonResponse(
            {
                "history": [
                    {
                        "operation_id": str(item.operation_id),
                        "action": item.operation.command_type,
                        "actor": item.operation.actor.display_name,
                        "at": item.operation.confirmed_at.isoformat(),
                        "before": item.before,
                        "after": item.after,
                        "details": item.details,
                    }
                    for item in items[:50]
                ],
                "next_offset": offset + 50 if len(items) > 50 else None,
            }
        )
