import json
from functools import wraps

from django.db import connection
from django.http import HttpResponseRedirect, JsonResponse
from django.templatetags.static import static
from django.utils.translation import gettext
from django.views.decorators.http import require_http_methods

from . import idempotency
from .errors import BusinessError
from .payloads import invalid_input
from .policies import ActorContext


def endpoint(*, access, methods):
    def decorate(view):
        @wraps(view)
        def call(request, *args, **kwargs):
            response = view(request, *args, **kwargs)
            if isinstance(response, idempotency.Outcome):
                result = JsonResponse(response.body, status=response.status)
                if response.once:
                    result = JsonResponse(
                        {**response.body, "one_time": response.once}, status=response.status
                    )
                if response.replayed:
                    result["Idempotency-Replayed"] = "true"
                return result
            return response

        decorated = require_http_methods(methods)(call)
        decorated.m0_access = access
        return decorated

    return decorate


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise invalid_input()
        result[key] = value
    return result


def _invalid_constant(value):
    raise invalid_input()


def json_body(request):
    if request.content_type != "application/json":
        raise invalid_input()
    try:
        body = json.loads(request.body, object_pairs_hook=_object, parse_constant=_invalid_constant)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise invalid_input() from exc
    if not isinstance(body, dict):
        raise invalid_input()
    return body


def actor_context(request):
    return ActorContext(
        request.user.pk, request.user.auth_version, getattr(request, "user_session_id", None)
    )


@endpoint(access="authenticated", methods=["POST"])
def register(request):
    body = json_body(request)
    if set(body) - {"intent_id", "key", "payload_hash", "command_type", "resource_id"}:
        raise invalid_input()
    try:
        return idempotency.register_intent(
            actor_context(request),
            intent_id=body["intent_id"],
            key=body["key"],
            fingerprint=body["payload_hash"],
            command_type=body["command_type"],
            resource_id=body.get("resource_id"),
        )
    except KeyError as exc:
        raise invalid_input() from exc


@endpoint(access="authenticated", methods=["GET"])
def status(request, intent_id):
    return idempotency.read_intent(actor_context(request), intent_id)


@endpoint(access="authenticated", methods=["POST"])
def resolve(request, intent_id):
    return idempotency.resolve_intent(actor_context(request), intent_id)


@endpoint(access="public", methods=["GET"])
def live(request):
    return JsonResponse({"status": "ok", "module": "M0", "prototype": True})


@endpoint(access="public", methods=["GET"])
def ready(request):
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
        cursor.fetchone()
    return JsonResponse({"status": "ready"})


def csrf_failure(request, reason=""):
    return JsonResponse(
        BusinessError(
            "CSRF_REJECTED", gettext("Confirmation de sécurité invalide."), status=403
        ).response(),
        status=403,
    )


@endpoint(access="public", methods=["GET", "HEAD"])
def favicon(request):
    """Le lecteur d'images natif cherche aussi l'icône à la racine."""
    return HttpResponseRedirect(static("ui/app-icon-192.png"))


# Ressource visuelle publique, également autorisée pendant le remplacement initial.
favicon.temporary_access = True


@endpoint(access="public", methods=["GET"])
def web_manifest(request):
    """Métadonnées publiques uniquement : aucun cache transactionnel."""
    return JsonResponse(
        {
            "id": "/",
            "name": gettext("Gestion des stocks"),
            "short_name": gettext("Réserve"),
            "lang": request.LANGUAGE_CODE,
            "start_url": "/mon-compte",
            "scope": "/",
            "display": "standalone",
            "background_color": "#faf9f6",
            "theme_color": "#315A54",
            "icons": [
                {
                    "src": static(f"ui/app-icon-{size}.png"),
                    "sizes": f"{size}x{size}",
                    "type": "image/png",
                }
                for size in (192, 512)
            ],
        },
        content_type="application/manifest+json",
    )


web_manifest.temporary_access = True
