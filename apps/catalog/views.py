import uuid

from django import forms
from django.db import transaction
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext
from django.utils.translation import gettext_lazy as _

from apps.accounts.views import endpoint
from apps.core.api import actor_context
from apps.core.errors import BusinessError
from apps.core.policies import lock_actor
from apps.core.search import matching

from .api import perform
from .forms import (
    ActivationForm,
    ArchiveForm,
    PermissionRowForm,
    PhotoForm,
    ProductForm,
    ReferenceForm,
    SupplierRowForm,
    VersionForm,
)
from .images import upload_photo
from .models import Category, Floor, Product, Supplier
from .policies import catalog_admin, visible_products
from .selectors import (
    catalog_list,
    get_product,
    parameter_history,
    permission_data,
    product_data,
    supplier_link_data,
)
from .services import reference_data


def result_or_error(result):
    if result.status >= 400:
        raise BusinessError(
            result.body["error"]["code"], result.body["error"]["message"], status=result.status
        )
    return result


def submit(request, form, kind, payload, resource_id=None):
    return result_or_error(
        perform(
            request,
            kind,
            payload,
            resource_id,
            identifier=form.cleaned_data["operation_id"],
            key=form.cleaned_data["operation_key"],
        )
    )


def renew_form(form, error):
    form.add_error(None, error.message)
    form.data = form.data.copy()
    for key in ["operation_id", "operation_key"]:
        form.data[form.add_prefix(key)] = (
            str(uuid.uuid4()) if key == "operation_id" else uuid.uuid4().hex
        )


@endpoint(access="authenticated", methods=["GET"])
def products(request):
    with transaction.atomic():
        actor = lock_actor(actor_context(request))
        rows, next_offset = catalog_list(actor, request.GET)
        categories = Category.objects.filter(
            pk__in=visible_products(actor).values("category_id")
        ).order_by("name")
        return render(
            request,
            "catalog/list.html",
            {
                "products": [{"product": row, "data": product_data(row, actor)} for row in rows],
                "categories": categories,
                "statuses": Product.Status.choices,
                "next_offset": next_offset,
                "filters": request.GET,
            },
        )


@endpoint(access="authenticated", methods=["GET"])
def detail(request, product_id):
    with transaction.atomic():
        actor = lock_actor(actor_context(request))
        product = get_product(actor, product_id)
        context = {"product": product, "data": product_data(product, actor)}
        if actor.role == "ADMIN":
            context.update(
                photos=product.photos.order_by("-created_at"),
                permissions=product.floor_permissions.select_related("floor").order_by(
                    "floor__code"
                ),
                suppliers=product.supplier_links.select_related("supplier").order_by(
                    "supplier__name"
                ),
                archive_form=ArchiveForm(
                    initial={"expected_version": product.version}, prefix="archive"
                ),
                activation_form=ActivationForm(
                    initial={"expected_version": product.version}, prefix="activate"
                ),
                photo_form=PhotoForm(initial={"expected_version": product.version}, prefix="photo"),
            )
        return render(request, "catalog/detail.html", context)


@endpoint(access="authenticated", methods=["GET", "POST"])
def edit_product(request, product_id=None):
    catalog_admin(request.user)
    product = get_object_or_404(Product, pk=product_id) if product_id else None
    initial = (
        {
            "name": product.name,
            "category_id": product.category_id,
            "unit": product.unit,
            "minimum": product.minimum,
            "target": product.target,
            "expected_version": product.version,
        }
        if product
        else {"minimum": 0, "target": 0}
    )
    form = ProductForm(
        request.POST if request.method == "POST" else None, initial=initial, product=product
    )
    if request.method == "POST" and form.is_valid():
        values = form.cleaned_data
        payload = {field: values[field] for field in ["name", "unit", "minimum", "target"]}
        payload["category_id"] = str(values["category_id"].pk) if values["category_id"] else None
        if product:
            payload["expected_version"] = values["expected_version"]
        else:
            payload["code"] = values["operation_id"].hex.upper()
        try:
            result = submit(
                request,
                form,
                f"catalog.product.{'update' if product else 'create'}",
                payload,
                product_id,
            )
            return redirect("product-detail", product_id=result.body["resource"]["id"])
        except BusinessError as exc:
            renew_form(form, exc)
    return render(request, "catalog/edit.html", {"form": form, "product": product})


@endpoint(access="authenticated", methods=["POST"])
def upload(request, product_id):
    catalog_admin(request.user)
    product = get_object_or_404(Product, pk=product_id)
    form = PhotoForm(request.POST, request.FILES, prefix="photo")
    if form.is_valid():
        try:
            result_or_error(
                upload_photo(
                    request,
                    product_id,
                    form.cleaned_data["photo"],
                    expected_version=form.cleaned_data["expected_version"],
                    identifier=form.cleaned_data["operation_id"],
                    key=form.cleaned_data["operation_key"],
                )
            )
            return redirect("product-detail", product_id=product_id)
        except BusinessError as exc:
            renew_form(form, exc)
    return render(request, "catalog/upload.html", {"form": form, "product": product}, status=400)


@endpoint(access="authenticated", methods=["POST"])
def transition(request, product_id, action):
    catalog_admin(request.user)
    form_type = ArchiveForm if action == "archive" else ActivationForm
    form = form_type(request.POST, prefix=action)
    if not form.is_valid():
        return render(
            request,
            "catalog/action.html",
            {"form": form, "action": action, "product_id": product_id},
            status=400,
        )
    payload = {
        key: value
        for key, value in form.cleaned_data.items()
        if key not in {"operation_id", "operation_key"}
    }
    try:
        submit(request, form, f"catalog.product.{action}", payload, product_id)
        return redirect("product-detail", product_id=product_id)
    except BusinessError as exc:
        renew_form(form, exc)
        return render(
            request,
            "catalog/action.html",
            {"form": form, "action": action, "product_id": product_id},
            status=exc.status,
        )


REFERENCES = {
    "floor": (Floor, _("Unités"), _("unité")),
    "category": (Category, _("Catégories"), _("catégorie")),
    "supplier": (Supplier, _("Fournisseurs"), _("fournisseur")),
}


@endpoint(access="authenticated", methods=["GET"])
def references(request, kind):
    catalog_admin(request.user)
    try:
        model, title, singular = REFERENCES[kind]
    except KeyError as exc:
        raise Http404 from exc
    try:
        offset = int(request.GET.get("offset", 0))
        if not 0 <= offset <= 100000:
            raise ValueError
    except ValueError:
        raise Http404 from None
    fields = ("name",) if kind == "category" else ("name", "code")
    query = matching(model.objects.all(), request.GET.get("q", ""), fields).order_by(
        "_search_rank", "name", "id"
    )
    rows = list(query[offset : offset + 51])
    return render(
        request,
        "catalog/references.html",
        {
            "references": rows[:50],
            "title": title,
            "singular": singular,
            "kind": kind,
            "next_offset": offset + 50 if len(rows) > 50 else None,
            "query": request.GET.get("q", "")[:150],
        },
    )


@endpoint(access="authenticated", methods=["GET", "POST"])
def edit_reference(request, kind, reference_id=None):
    catalog_admin(request.user)
    try:
        model, title, singular = REFERENCES[kind]
    except KeyError as exc:
        raise Http404 from exc
    reference = get_object_or_404(model, pk=reference_id) if reference_id else None
    initial = (
        {**reference_data(reference), "expected_version": reference.version}
        if reference
        else {"active": True}
    )
    form = ReferenceForm(
        request.POST if request.method == "POST" else None,
        initial=initial,
        reference=reference,
        kind=kind,
    )
    if request.method == "POST" and form.is_valid():
        payload = {
            key: value
            for key, value in form.cleaned_data.items()
            if key not in {"operation_id", "operation_key"}
        }
        if kind in {"supplier", "floor"} and reference is None:
            payload["code"] = form.cleaned_data["operation_id"].hex
        try:
            submit(
                request,
                form,
                f"catalog.{kind}.{'update' if reference else 'create'}",
                payload,
                reference_id,
            )
            return redirect("catalog-references", kind=kind)
        except BusinessError as exc:
            renew_form(form, exc)
    return render(
        request,
        "catalog/reference_edit.html",
        {"form": form, "reference": reference, "title": title, "singular": singular, "kind": kind},
    )


@endpoint(access="authenticated", methods=["GET", "POST"])
def permissions(request, product_id):
    catalog_admin(request.user)
    product = get_object_or_404(Product, pk=product_id)
    floors = list(Floor.objects.order_by("code"))
    current = {row.floor_id: row for row in product.floor_permissions.all()}
    initial = [
        permission_data(current[floor.pk]) if floor.pk in current else {"floor_id": floor.pk}
        for floor in floors
    ]
    rowset = forms.formset_factory(PermissionRowForm, extra=0, max_num=1000, validate_max=True)(
        request.POST if request.method == "POST" else None, initial=initial, prefix="rights"
    )
    form = VersionForm(
        request.POST if request.method == "POST" else None,
        initial={"expected_version": product.version},
    )
    if request.method == "POST" and form.is_valid() and rowset.is_valid():
        try:
            rows = [
                {**row.cleaned_data, "floor_id": str(row.cleaned_data["floor_id"])}
                for row in rowset
            ]
            submit(
                request,
                form,
                "catalog.product.permissions",
                {"expected_version": form.cleaned_data["expected_version"], "permissions": rows},
                product_id,
            )
            return redirect("product-detail", product_id=product_id)
        except BusinessError as exc:
            renew_form(form, exc)
    names = {str(floor.pk): floor for floor in floors}
    rows = [
        {
            "form": row,
            "reference": names.get(str(row["floor_id"].value()))
            if row["floor_id"].value()
            else None,
        }
        for row in rowset
    ]
    return render(
        request,
        "catalog/permissions.html",
        {"product": product, "form": form, "rowset": rowset, "rows": rows},
    )


@endpoint(access="authenticated", methods=["GET", "POST"])
def supplier_links(request, product_id):
    catalog_admin(request.user)
    product = get_object_or_404(Product, pk=product_id)
    suppliers = list(Supplier.objects.order_by("name"))
    current = {row.supplier_id: row for row in product.supplier_links.all()}
    initial = []
    for supplier in suppliers:
        values = (
            supplier_link_data(current[supplier.pk])
            if supplier.pk in current
            else {"supplier_id": supplier.pk}
        )
        values["included"] = values.pop("active", False)
        initial.append(values)
    rowset = forms.formset_factory(SupplierRowForm, extra=0, max_num=1000, validate_max=True)(
        request.POST if request.method == "POST" else None, initial=initial, prefix="links"
    )
    form = VersionForm(
        request.POST if request.method == "POST" else None,
        initial={"expected_version": product.version},
    )
    if request.method == "POST" and form.is_valid() and rowset.is_valid():
        try:
            planned = []
            for row in rowset:
                values = row.cleaned_data.copy()
                if not values.pop("included"):
                    continue
                values["supplier_id"] = str(values["supplier_id"])
                values["indicative_price"] = (
                    str(values["indicative_price"])
                    if values["indicative_price"] is not None
                    else None
                )
                planned.append(values)
            submit(
                request,
                form,
                "catalog.product.suppliers",
                {"expected_version": form.cleaned_data["expected_version"], "suppliers": planned},
                product_id,
            )
            return redirect("product-detail", product_id=product_id)
        except BusinessError as exc:
            renew_form(form, exc)
    names = {str(supplier.pk): supplier for supplier in suppliers}
    rows = [
        {
            "form": row,
            "reference": names.get(str(row["supplier_id"].value()))
            if row["supplier_id"].value()
            else None,
        }
        for row in rowset
    ]
    return render(
        request,
        "catalog/supplier_links.html",
        {"product": product, "form": form, "rowset": rowset, "rows": rows},
    )


def history_text(key, value):
    if value is None:
        return ["—"]
    if key == "permissions":
        return [
            gettext("%(unit)s (%(code)s): %(visibility)s, %(request)s, limit %(maximum)s")
            % {
                "unit": row.get("floor_name", gettext("Unit")),
                "code": row.get("floor_code", gettext("reference retained")),
                "visibility": gettext("visible") if row["visible"] else gettext("not visible"),
                "request": gettext("requests allowed")
                if row["demandable"]
                else gettext("requests not allowed"),
                "maximum": row.get("maximum") or gettext("not configured"),
            }
            for row in value
        ] or [gettext("No permissions configured")]
    if key == "suppliers":
        lines = []
        for row in value:
            association = (
                gettext("association removed")
                if not row["active"]
                else (gettext("preferred") if row["preferred"] else gettext("alternative"))
            )
            line = gettext("%(supplier)s : %(association)s") % {
                "supplier": row.get("supplier_name", gettext("Supplier")),
                "association": association,
            }
            if row.get("reference"):
                line += gettext(", ref. %(reference)s") % {"reference": row["reference"]}
            if row.get("indicative_price") is not None:
                line += gettext(", indicative price %(price)s %(currency)s") % {
                    "price": row["indicative_price"],
                    "currency": row["currency"],
                }
            if row.get("website"):
                line += gettext(", link %(website)s") % {"website": row["website"]}
            lines.append(line)
        return lines or [gettext("No supplier configured")]
    return [str(value)]


@endpoint(access="authenticated", methods=["GET"])
def history(request, product_id):
    catalog_admin(request.user)
    product = get_object_or_404(Product, pk=product_id)
    try:
        offset = int(request.GET.get("offset", 0))
        if not 0 <= offset <= 100000:
            raise ValueError
    except ValueError:
        raise Http404 from None
    items = list(parameter_history(product)[offset : offset + 51])
    labels = {
        "code": gettext("Reference"),
        "name": gettext("Name"),
        "unit": gettext("Unit"),
        "minimum": gettext("Low stock at"),
        "target": gettext("Target quantity"),
        "status_label": gettext("Status"),
        "category_name": gettext("Category"),
        "photo_id": gettext("Photo version"),
        "permissions": gettext("Permissions and limits"),
        "suppliers": gettext("Supplier links"),
    }
    rows = []
    for item in items[:50]:
        changes = [
            {
                "label": label,
                "before": history_text(key, item.before.get(key)),
                "after": history_text(key, item.after.get(key)),
                "before_photo": f"/api/v1/products/{product.pk}/photo?version={item.before[key]}"
                if key == "photo_id" and item.before.get(key)
                else None,
                "after_photo": f"/api/v1/products/{product.pk}/photo?version={item.after[key]}"
                if key == "photo_id" and item.after.get(key)
                else None,
            }
            for key, label in labels.items()
            if item.before.get(key) != item.after.get(key)
        ]
        rows.append({"item": item, "changes": changes})
    return render(
        request,
        "catalog/history.html",
        {"product": product, "rows": rows, "next_offset": offset + 50 if len(items) > 50 else None},
    )
