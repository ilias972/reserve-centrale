import hashlib

from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.utils.translation import gettext

from apps.accounts.views import endpoint
from apps.core.api import actor_context, json_body
from apps.core.api import endpoint as api_endpoint
from apps.core.errors import BusinessError
from apps.core.policies import check_role, lock_actor

from . import push, routing
from .models import Activation, Attempt, PushSubscription, WorkItem


def navigation_count(request):
    if not request.user.is_authenticated or request.user.must_change_password:
        return {}
    return {
        "unread_notification_count": routing.visible(request.user)
        .filter(read_at__isnull=True)
        .count()
    }


def localized_message(notification):
    """Render a concise translated inbox summary; the linked record holds the details."""
    if notification.target_kind == "REQUEST":
        return gettext(
            "Une demande a changé. Consultez son suivi pour voir son état et ses quantités."
        )
    if notification.target_kind == "CASE":
        return gettext(
            "Un dossier nécessite votre attention. Consultez son état et les actions possibles."
        )
    if notification.target_kind == "ORDER":
        return gettext(
            "Une commande fournisseur nécessite un suivi. Consultez les quantités encore attendues."
        )
    if notification.target_kind == "PRODUCT":
        return gettext(
            "Un produit nécessite votre attention. Consultez le besoin et les quantités encore attendues."
        )
    return gettext("Une information vous attend dans l’application.")


@endpoint(access="authenticated", methods=["GET"])
def inbox(request):
    state = request.GET.get("vue", "toutes")
    if state not in {"toutes", "non-lues"}:
        raise Http404
    query = routing.visible(request.user)
    if state == "non-lues":
        query = query.filter(read_at__isnull=True)
    page = Paginator(query, 30).get_page(request.GET.get("page"))
    rows = []
    for row in page:
        url, label = routing.destination(row, request.user)
        rows.append(
            {
                "notification": row,
                "url": url,
                "label": gettext(label),
                "title": gettext(row.title),
                "message": localized_message(row),
            }
        )
    return render(
        request,
        "notifications/inbox.html",
        {
            "rows": rows,
            "list_revision": hashlib.sha256(
                "|".join(
                    f"{r['notification'].pk}:{r['notification'].read_at}:{r['url']}" for r in rows
                ).encode()
            ).hexdigest(),
            "page": page,
            "vue": state,
            "cutoff": timezone.now().isoformat(),
            "push_configured": push.enabled(),
            "push_subscriptions": PushSubscription.objects.filter(
                user=request.user, session_id=request.user_session_id, revoked_at__isnull=True
            ),
            "initialized": Activation.objects.filter(pk=1).exists(),
        },
    )


@endpoint(access="authenticated", methods=["GET"])
def open_notification(request, notification_id):
    row = routing.visible(request.user).filter(pk=notification_id).first()
    if row is None:
        raise Http404
    url, _ = routing.destination(row, request.user)
    if url is None:
        raise BusinessError(
            "TARGET_FORBIDDEN",
            gettext("Cet élément n'est plus accessible avec vos droits actuels."),
            status=403,
        )
    return redirect(url)


@endpoint(access="authenticated", methods=["POST"])
@transaction.atomic
def read(request, notification_id=None):
    actor = lock_actor(actor_context(request))
    query = routing.visible(actor)
    if notification_id:
        if not query.filter(pk=notification_id).exists():
            raise Http404
        query = query.filter(pk=notification_id)
    else:
        try:
            cutoff = parse_datetime(request.POST.get("cutoff", ""))
        except ValueError:
            cutoff = None
        if cutoff is None or timezone.is_naive(cutoff) or cutoff > timezone.now():
            raise BusinessError(
                "INVALID_INPUT",
                gettext("Rechargez la liste avant de marquer les notifications comme lues."),
                status=400,
            )
        query = query.filter(created_at__lte=cutoff)
    query.filter(read_at__isnull=True).update(read_at=timezone.now())
    return redirect("notifications")


@api_endpoint(access="authenticated", methods=["GET"])
def count(request):
    return JsonResponse(
        {"unread": routing.visible(request.user).filter(read_at__isnull=True).count()}
    )


@api_endpoint(access="authenticated", methods=["GET", "POST"])
def subscriptions(request):
    check_role(request.user, {"REPLENISHER"})
    if request.method == "GET":
        return JsonResponse(
            {
                "enabled": push.enabled(),
                "public_key": settings_key(),
                "subscriptions": [
                    str(s)
                    for s in PushSubscription.objects.filter(
                        user=request.user,
                        session_id=request.user_session_id,
                        revoked_at__isnull=True,
                    ).values_list("id", flat=True)
                ],
            }
        )
    sub = push.subscribe(actor_context(request), json_body(request))
    return JsonResponse({"id": str(sub.pk)}, status=201)


def settings_key():
    from django.conf import settings

    return settings.WEBPUSH_PUBLIC_KEY if push.enabled() else ""


@api_endpoint(access="authenticated", methods=["POST"])
def revoke_subscription(request, subscription_id):
    push.revoke(actor_context(request), subscription_id)
    return JsonResponse({"revoked": True})


@api_endpoint(access="public", methods=["GET"])
def service_worker(request):
    from django.conf import settings

    response = HttpResponse(
        (settings.BASE_DIR / "static" / "notifications" / "service-worker.js").read_text(),
        content_type="text/javascript",
    )
    response["Service-Worker-Allowed"] = "/"
    return response


service_worker.temporary_access = True


@endpoint(access="authenticated", methods=["GET"])
def diagnostics(request):
    check_role(request.user, {"ADMIN"})
    counts = list(
        WorkItem.objects.values("kind", "state")
        .annotate(total=Count("id"))
        .order_by("kind", "state")
    )
    attempts = Attempt.objects.select_related("work", "attemptresult").order_by("-started_at")[:50]
    return render(
        request, "notifications/diagnostics.html", {"counts": counts, "attempts": attempts}
    )
