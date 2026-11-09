from django.http import JsonResponse
from django.urls import path

from apps.core import api, idempotency
from config.urls import urlpatterns as production_urls
from tests.probes import commands


@api.endpoint(access="authenticated", methods=["POST"])
def confirm(request, intent_id):
    outcome = idempotency.execute_intent(
        api.actor_context(request),
        intent_id=intent_id,
        key=request.headers.get("Idempotency-Key"),
        payload=api.json_body(request),
        correlation_id=request.correlation_id,
    )
    if outcome.status == 200:
        commands.committed.set()
    return outcome


def undeclared(request):
    return JsonResponse({"must_not_be_visible": True})


urlpatterns = [
    *production_urls,
    path("_test/confirm/<uuid:intent_id>", confirm),
    path("_test/undeclared", undeclared),
]
