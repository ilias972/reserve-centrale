from datetime import timedelta

from django.contrib.auth import logout
from django.http import JsonResponse
from django.shortcuts import redirect
from django.utils import timezone
from django.utils.deprecation import MiddlewareMixin
from django.utils.translation import gettext

from apps.core.errors import BusinessError

from .sessions import checked_session, durations


class AccountSessionMiddleware(MiddlewareMixin):
    def process_request(self, request):
        if not request.user.is_authenticated:
            return None
        try:
            meta = checked_session(
                request.user,
                request.session.get("reserve_session_id"),
                key=request.session.session_key,
            )
        except (BusinessError, ValueError) as exc:
            if isinstance(exc, BusinessError):
                request.session_error = exc
            logout(request)
            return None
        request.user_session_id = meta.pk
        # Une lecture automatique vérifie toujours l'accès, sans entretenir
        # artificiellement une session inactive. Ce drapeau ne confère aucun droit.
        if request.method == "GET" and (
            request.headers.get("X-Reserve-Background") == "1"
            or request.GET.get("_live") == "1"
            or (request.path.startswith("/api/v1/") and request.path.endswith("/photo"))
        ):
            return None
        now = timezone.now()
        # Une ancienne requête ne peut retirer une révocation ni repousser
        # l'échéance absolue ; seules ces deux colonnes sont mises à jour.
        expiry = min(now + timedelta(seconds=durations(request.user)[0]), meta.absolute_expires_at)
        type(meta).objects.filter(pk=meta.pk, revoked_at__isnull=True).update(
            last_seen_at=now, idle_expires_at=expiry
        )
        request.session.set_expiry(
            max(1, int((expiry - now).total_seconds())) if meta.persistent else 0
        )
        return None

    def process_view(self, request, view, view_args, view_kwargs):
        error = getattr(request, "session_error", None)
        if (
            error
            and not getattr(view, "m1_html", False)
            and getattr(view, "m0_access", None) == "authenticated"
        ):
            return JsonResponse(error.response(), status=error.status)
        if not request.user.is_authenticated:
            return None
        if request.user.must_change_password and not getattr(view, "temporary_access", False):
            if not getattr(view, "m1_html", False):
                return JsonResponse(
                    BusinessError(
                        "PASSWORD_CHANGE_REQUIRED",
                        gettext("Remplacez votre mot de passe temporaire."),
                        status=403,
                    ).response(),
                    status=403,
                )
            return redirect("change-password")
        return None
