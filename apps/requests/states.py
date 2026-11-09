"""Projections M5 dérivées des quantités ; aucune réouverture automatique."""

from django.utils.translation import gettext, gettext_noop


def status_label(request):
    if request.status == "PARTIAL_CLOSED":
        reasons = {
            "SHORTAGE": gettext_noop("manque"),
            "REMAINDER_ABANDONED": gettext_noop("reliquat abandonné"),
            "ADMIN_RECONCILIATION": gettext_noop("engagement réduit après comptage"),
            "DELIVERY_CORRECTION": gettext_noop("livraison corrigée"),
        }
        if request.closure_reason == "SHORTAGE":
            return (
                gettext("Partiellement livrée — clôturée pour manque")
                if any(line.delivered for line in request.lines.all())
                else gettext("Clôturée — manque")
            )
        prefix = (
            gettext("Partiellement livrée — clôturée")
            if any(line.delivered for line in request.lines.all())
            else gettext("Clôturée")
        )
        return (
            prefix
            + " — "
            + gettext(reasons.get(request.closure_reason, gettext_noop("décision Gestionnaire")))
        )
    return request.get_status_display()


def delivered_state(lines):
    if any(line.reservation.remaining for line in lines):
        return "PARTIAL_OPEN"
    return "PARTIAL_CLOSED" if any(line.closed for line in lines) else "DELIVERED"
