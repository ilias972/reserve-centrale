from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.translation import gettext

from apps.core.api import endpoint as api_endpoint
from apps.core.errors import BusinessError
from apps.core.policies import check_role
from apps.core.search import matching

from . import services
from .api import perform, temporary_access
from .forms import (
    AdminOperationForm,
    ChangePasswordForm,
    CreateUserForm,
    LoginForm,
    OperationForm,
    ResetAccessForm,
    UpdateUserForm,
)
from .models import User, UserSession
from .operations import user_data


def endpoint(**options):
    def decorate(view):
        result = api_endpoint(**options)(view)
        result.m1_html = True
        return result

    return decorate


def show(request, template, context=None, status=200):
    return render(request, template, context or {}, status=status)


def form_error(form, error):
    form.add_error(None, error.message)


@temporary_access
@endpoint(access="public", methods=["GET", "POST"])
def login(request):
    form = LoginForm(request.POST or None)
    status = 200
    if request.method == "POST" and form.is_valid():
        try:
            user = services.sign_in(request, **form.cleaned_data)
            return redirect("change-password" if user.must_change_password else "account")
        except BusinessError as exc:
            form_error(form, exc)
            status = exc.status
    return show(request, "auth/login.html", {"form": form}, status)


@temporary_access
@endpoint(access="authenticated", methods=["GET", "POST"])
def change_password(request):
    form = ChangePasswordForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            services.change_password(
                request, form.cleaned_data["current_password"], form.cleaned_data["new_password"]
            )
            return redirect("account")
        except BusinessError as exc:
            form_error(form, exc)
    return show(request, "auth/change_password.html", {"form": form})


@temporary_access
@endpoint(access="authenticated", methods=["POST"])
def logout(request):
    services.sign_out(request)
    return redirect("login")


@endpoint(access="authenticated", methods=["GET"])
def account(request):
    return show(request, "auth/account.html", session_context(request, request.user))


def session_context(request, target):
    try:
        page = int(request.GET.get("sessions_page", 1))
        if not 1 <= page <= 100000:
            raise ValueError
    except ValueError as exc:
        raise Http404 from exc
    now = timezone.now()
    sessions = list(
        UserSession.objects.filter(
            user=target,
            revoked_at__isnull=True,
            idle_expires_at__gt=now,
            absolute_expires_at__gt=now,
        ).order_by("-created_at", "pk")[(page - 1) * 50 : page * 50 + 1]
    )
    form_type = OperationForm if target.pk == request.user.pk else AdminOperationForm
    rows = [
        {
            "meta": meta,
            "current": meta.pk == request.user_session_id,
            "form": form_type(prefix=f"session-{meta.pk}"),
        }
        for meta in sessions[:50]
    ]
    return {"session_rows": rows, "next_sessions_page": page + 1 if len(sessions) > 50 else None}


def admin_form_action(request, form, kind, payload, resource_id=None):
    if form.cleaned_data.get("admin_password"):
        services.reauthenticate(request, form.cleaned_data["admin_password"])
    result = perform(
        request,
        kind,
        payload,
        resource_id,
        identifier=form.cleaned_data["operation_id"],
        key=form.cleaned_data["operation_key"],
    )
    if result.status >= 400:
        raise BusinessError(
            result.body["error"]["code"], result.body["error"]["message"], status=result.status
        )
    return result


@endpoint(access="authenticated", methods=["GET"])
def users(request):
    check_role(request.user, {User.Role.ADMIN})
    query = request.GET.get("q", "")[:150]
    try:
        page = max(1, int(request.GET.get("page", 1)))
        if page > 2000:
            raise ValueError
    except ValueError as exc:
        raise Http404 from exc
    accounts = matching(
        User.objects.select_related("floor"), query, ("username", "display_name")
    ).order_by("_search_rank", "username")
    rows = list(accounts[(page - 1) * 50 : page * 50 + 1])
    return show(
        request,
        "admin/users/list.html",
        {"accounts": rows[:50], "query": query, "next_page": page + 1 if len(rows) > 50 else None},
    )


@endpoint(access="authenticated", methods=["GET", "POST"])
def create_user(request):
    check_role(request.user, {User.Role.ADMIN})
    form = CreateUserForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        values = form.cleaned_data
        payload = {field: values[field] for field in ["username", "display_name", "role"]}
        payload["floor_id"] = str(values["floor_id"].pk) if values["floor_id"] else None
        try:
            result = admin_form_action(request, form, "accounts.create", payload)
            return show(
                request,
                "admin/users/access_result.html",
                {"result": result.body["resource"], "once": result.once},
            )
        except BusinessError as exc:
            form_error(form, exc)
            # Un refus est terminal ; une nouvelle confirmation est une nouvelle
            # intention. Le formulaire ne retransmet jamais un ancien succès.
            form.data = form.data.copy()
            for field in ["operation_id", "operation_key"]:
                form.data[field] = str(form.fields[field].initial())
    return show(request, "admin/users/edit.html", {"form": form, "creating": True})


@endpoint(access="authenticated", methods=["GET", "POST"])
def update_user(request, user_id):
    check_role(request.user, {User.Role.ADMIN})
    user = get_object_or_404(User, pk=user_id)
    initial = {**user_data(user), "floor_id": user.floor_id, "expected_version": user.version}
    form = UpdateUserForm(request.POST or None, initial=initial, user=user)
    if request.method == "POST" and form.is_valid():
        values = form.cleaned_data
        payload = {
            field: values[field]
            for field in ["display_name", "role", "is_active", "expected_version"]
        }
        payload["floor_id"] = str(values["floor_id"].pk) if values["floor_id"] else None
        try:
            admin_form_action(request, form, "accounts.update", payload, user.pk)
            return redirect("users")
        except BusinessError as exc:
            form_error(form, exc)
            form.data = form.data.copy()
            for field in ["operation_id", "operation_key"]:
                form.data[field] = str(form.fields[field].initial())
    return show(
        request,
        "admin/users/edit.html",
        {"form": form, "target": user, **session_context(request, user)},
    )


@endpoint(access="authenticated", methods=["GET", "POST"])
def reset_access(request, user_id):
    check_role(request.user, {User.Role.ADMIN})
    user = get_object_or_404(User, pk=user_id)
    form = ResetAccessForm(request.POST or None, initial={"expected_version": user.version})
    if request.method == "POST" and form.is_valid():
        try:
            result = admin_form_action(
                request,
                form,
                "accounts.reset",
                {
                    "expected_version": form.cleaned_data["expected_version"],
                    "identity_verified": form.cleaned_data["identity_verified"],
                },
                user.pk,
            )
            return show(
                request,
                "admin/users/access_result.html",
                {"result": result.body["resource"], "once": result.once},
            )
        except BusinessError as exc:
            form_error(form, exc)
            form.data = form.data.copy()
            for field in ["operation_id", "operation_key"]:
                form.data[field] = str(form.fields[field].initial())
    return show(request, "admin/users/reset.html", {"form": form, "target": user})


@endpoint(access="authenticated", methods=["POST"])
def revoke_session(request, session_id):
    form = AdminOperationForm(request.POST, prefix=f"session-{session_id}")
    if not form.is_valid():
        raise BusinessError("INVALID_INPUT", gettext("Confirmation invalide."), status=400)
    sessions = UserSession.objects.all()
    if request.user.role != User.Role.ADMIN:
        sessions = sessions.filter(user=request.user)
    target = get_object_or_404(sessions, pk=session_id)
    # Le service recontrôle la propriété avant d'écrire ; ce contrôle précoce
    # empêche également une réauthentification inutile d'un acteur non autorisé.
    if target.user_id != request.user.pk:
        check_role(request.user, {User.Role.ADMIN})
    try:
        admin_form_action(request, form, "accounts.revoke_session", {}, session_id)
    except BusinessError as exc:
        return show(request, "auth/action_error.html", {"message": exc.message}, status=exc.status)
    if session_id == request.user_session_id:
        request.session.flush()
        return redirect("login")
    return (
        redirect("account")
        if target.user_id == request.user.pk
        else redirect("update-user", user_id=target.user_id)
    )
