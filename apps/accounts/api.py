from django.http import JsonResponse
from django.utils import timezone
from django.utils.translation import gettext

from apps.core.api import actor_context, endpoint, json_body
from apps.core.errors import BusinessError
from apps.core.idempotency import execute_intent, register_intent
from apps.core.payloads import invalid_input, payload_hash
from apps.core.policies import check_role

from . import services
from .models import User, UserSession
from .operations import ensure_registered, user_data


def temporary_access(view):
    view.temporary_access = True
    return view


def exact(body, allowed):
    if set(body) - set(allowed):
        raise invalid_input()
    return body


def perform(request, kind, payload, resource_id=None, *, identifier=None, key=None):
    ensure_registered()
    key = key or request.headers.get("Idempotency-Key")
    identifier = identifier or request.headers.get("Operation-Intent")
    context = actor_context(request)
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


@temporary_access
@endpoint(access="public", methods=["POST"])
def login(request):
    body = exact(json_body(request), {"identifier", "password", "persistent"})
    user = services.sign_in(
        request,
        body.get("identifier"),
        body.get("password"),
        persistent=body.get("persistent", False),
    )
    return JsonResponse({"user": user_data(user), "change_required": user.must_change_password})


@temporary_access
@endpoint(access="authenticated", methods=["POST"])
def change_password(request):
    body = exact(json_body(request), {"current_password", "new_password"})
    user = services.change_password(request, body.get("current_password"), body.get("new_password"))
    return JsonResponse({"user": user_data(user)})


@temporary_access
@endpoint(access="authenticated", methods=["POST"])
def logout(request):
    services.sign_out(request)
    return JsonResponse({"signed_out": True})


@endpoint(access="authenticated", methods=["POST"])
def reauthenticate(request):
    body = exact(json_body(request), {"password"})
    services.reauthenticate(request, body.get("password"))
    return JsonResponse({"authenticated": True})


@endpoint(access="authenticated", methods=["GET"])
def me(request):
    return JsonResponse(
        {
            "user": user_data(request.user),
            "permissions": {"manage_accounts": request.user.role == User.Role.ADMIN},
        }
    )


@endpoint(access="authenticated", methods=["GET", "POST"])
def users(request):
    check_role(request.user, {User.Role.ADMIN})
    if request.method == "POST":
        return perform(request, "accounts.create", json_body(request))
    try:
        offset = int(request.GET.get("offset", 0))
        if offset < 0 or offset > 100000:
            raise ValueError
    except ValueError as exc:
        raise invalid_input() from exc
    rows = list(User.objects.select_related("floor").order_by("username")[offset : offset + 51])
    return JsonResponse(
        {
            "users": [user_data(user) for user in rows[:50]],
            "next_offset": offset + 50 if len(rows) > 50 else None,
        }
    )


@endpoint(access="authenticated", methods=["PATCH"])
def update_user(request, user_id):
    return perform(request, "accounts.update", json_body(request), user_id)


@endpoint(access="authenticated", methods=["POST"])
def reset_password(request, user_id):
    return perform(request, "accounts.reset", json_body(request), user_id)


def session_data(meta):
    return {
        "id": str(meta.pk),
        "user_id": str(meta.user_id),
        "device": meta.device_label,
        "created_at": meta.created_at.isoformat(),
        "last_seen_at": meta.last_seen_at.isoformat(),
        "idle_expires_at": meta.idle_expires_at.isoformat(),
        "absolute_expires_at": meta.absolute_expires_at.isoformat(),
        "revoked": meta.revoked_at is not None,
        "expired": min(meta.idle_expires_at, meta.absolute_expires_at) <= timezone.now(),
    }


@endpoint(access="authenticated", methods=["GET"])
def sessions(request):
    user_id = request.GET.get("user_id", str(request.user.pk))
    if user_id != str(request.user.pk) and request.user.role != User.Role.ADMIN:
        raise BusinessError("NOT_FOUND", gettext("Compte introuvable."), status=404)
    from apps.core.payloads import parse_uuid

    try:
        offset = int(request.GET.get("offset", 0))
        if not 0 <= offset <= 100000:
            raise ValueError
    except ValueError as exc:
        raise invalid_input() from exc
    rows = list(
        UserSession.objects.filter(user_id=parse_uuid(user_id)).order_by("-created_at", "pk")[
            offset : offset + 101
        ]
    )
    return JsonResponse(
        {
            "sessions": [session_data(meta) for meta in rows[:100]],
            "next_offset": offset + 100 if len(rows) > 100 else None,
        }
    )


@endpoint(access="authenticated", methods=["POST"])
def revoke_session(request, session_id):
    exact(json_body(request), set())
    return perform(request, "accounts.revoke_session", {}, session_id)
