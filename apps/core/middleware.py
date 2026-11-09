import logging
import time
from uuid import uuid4

from django.core.exceptions import PermissionDenied, RequestDataTooBig
from django.db import DatabaseError
from django.http import Http404, JsonResponse
from django.shortcuts import redirect, render
from django.utils.deprecation import MiddlewareMixin
from django.utils.translation import gettext

from .errors import BusinessError

logger = logging.getLogger("reserve")


class ApiErrorsMiddleware(MiddlewareMixin):
    def process_request(self, request):
        request.correlation_id = uuid4()
        request.m0_started = time.monotonic()

    def process_exception(self, request, exception):
        if isinstance(exception, BusinessError):
            response = exception.response()
            status = exception.status
        elif isinstance(exception, (PermissionDenied, Http404, RequestDataTooBig)):
            status = 404 if isinstance(exception, Http404) else 403
            if isinstance(exception, RequestDataTooBig):
                status = 413
            response = {
                "error": {"code": "REQUEST_REFUSED", "message": gettext("Requête refusée.")}
            }
        else:
            status = 503 if isinstance(exception, DatabaseError) else 500
            response = {
                "error": {
                    "code": "SERVICE_UNAVAILABLE" if status == 503 else "TECHNICAL_ERROR",
                    "message": gettext(
                        "Enregistrement non confirmé. Vérifiez le statut de l'opération."
                    ),
                    "retryable": True,
                }
            }
            # Pas de corps de requête, message d'exception, cookie ou secret dans les logs.
            logger.error(
                "request.failed",
                extra={
                    "event": "request.failed",
                    "error_type": type(exception).__name__,
                    "correlation_id": str(request.correlation_id),
                },
            )
        response["error"]["correlation_id"] = str(request.correlation_id)
        view = getattr(getattr(request, "resolver_match", None), "func", None)
        if getattr(view, "m1_html", False):
            return render(
                request,
                "auth/action_error.html",
                {
                    "message": response["error"]["message"],
                    "correlation_id": str(request.correlation_id),
                },
                status=status,
            )
        return JsonResponse(response, status=status)

    def process_response(self, request, response):
        response["Cache-Control"] = "no-store"
        response["X-Correlation-ID"] = str(request.correlation_id)
        # Le lecteur natif Chromium génère ses propres styles pour les documents
        # image. Exception par MIME raster ; les photos M2 actuelles sont réencodées
        # en JPEG avant stockage.
        # HTML/JSON gardent la CSP stricte, scripts et réseaux tiers restent refusés.
        image_document = response.get("Content-Type", "").split(";", 1)[0] in {
            "image/jpeg",
            "image/png",
            "image/webp",
        }
        style_sources = "'self' 'unsafe-inline'" if image_document else "'self'"
        response["Content-Security-Policy"] = (
            f"default-src 'none'; manifest-src 'self'; style-src {style_sources}; font-src 'self'; script-src 'self'; connect-src 'self'; worker-src 'self'; img-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
        )
        route = getattr(getattr(request, "resolver_match", None), "route", "unmatched")
        logger.info(
            "http.completed",
            extra={
                "event": "http.completed",
                "correlation_id": str(request.correlation_id),
                "method": request.method
                if request.method in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
                else "OTHER",
                "route": route,
                "status": response.status_code,
                "duration_ms": round((time.monotonic() - request.m0_started) * 1000, 3),
            },
        )
        return response


class DefaultDenyMiddleware(MiddlewareMixin):
    def process_view(self, request, view, view_args, view_kwargs):
        access = getattr(view, "m0_access", None)
        if access == "public":
            return None
        if access != "authenticated":
            return JsonResponse(
                {"error": {"code": "PERMISSION_DENIED", "message": gettext("Accès non autorisé.")}},
                status=403,
            )
        if not request.user.is_authenticated:
            if getattr(view, "m1_html", False):
                return redirect("login")
            return JsonResponse(
                {
                    "error": {
                        "code": "AUTHENTICATION_REQUIRED",
                        "message": gettext("Connexion requise."),
                    }
                },
                status=401,
            )
        return None


class ExplicitLanguageMiddleware:
    """Honor a validated explicit language cookie; new visitors use English."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        from django.conf import settings
        from django.utils import translation
        from django.utils.cache import patch_vary_headers

        allowed = {code for code, _label in settings.LANGUAGES}
        language = request.COOKIES.get(settings.LANGUAGE_COOKIE_NAME, "")
        if language not in allowed:
            language = settings.LANGUAGE_CODE
        translation.activate(language)
        request.LANGUAGE_CODE = language
        # Keep the selected language active until Django has rendered any
        # TemplateResponse. The handler renders it after the middleware chain.
        response = self.get_response(request)
        response.setdefault("Content-Language", language)
        patch_vary_headers(response, ("Cookie",))
        return response
