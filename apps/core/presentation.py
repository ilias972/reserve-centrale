"""Navigation de présentation ; destinations fixes et objets déjà autorisés."""

from urllib.parse import urlencode

from django.urls import reverse
from django.utils.translation import gettext


def case_navigation(request, case):
    origin = request.GET.get("from", "")
    choices = {
        "deliveries": ("delivery-issues", gettext("Retour aux problèmes de remise")),
        "cases": ("case-list", gettext("Retour aux dossiers")),
        "stock": ("stock-cases", gettext("Retour aux signalements de stock")),
    }
    if origin == "request" and case.delivery_id:
        url = reverse("request-detail", args=[case.delivery.request_id])
        label = gettext("Retour à la demande")
    else:
        if origin not in choices:
            origin = (
                "deliveries" if case.delivery_id else "stock" if case.stock_case_id else "cases"
            )
        route, label = choices[origin]
        url = reverse(route)
    params = {"from": origin}
    if origin in {"stock", "deliveries"}:
        state = request.GET.get("state")
        if state in {"pending", "resolved", "all"}:
            params["state"] = state
        if request.GET.get("q"):
            params["q"] = request.GET["q"][:150]
        offset = request.GET.get("offset", "")
        if offset.isascii() and offset.isdecimal() and int(offset) <= 100000:
            params["offset"] = offset
        if len(params) > 1:
            url += "?" + urlencode({k: v for k, v in params.items() if k != "from"})
    return {
        "case_back_url": url,
        "case_back_label": label,
        "case_origin": origin,
        "case_context_query": "?" + urlencode(params),
    }
