"""Lectures des faits M8 ; mêmes fonctions SQL que les contraintes différées."""

import json

from django.db import connection
from django.utils.translation import gettext_lazy

from .models import ReceiptCorrectionLine

NONCONFORMITIES = {
    "DAMAGED": gettext_lazy("Abîmé"),
    "INCORRECT": gettext_lazy("Mauvais article"),
    "UNUSABLE": gettext_lazy("Inutilisable"),
}
CASE_KINDS = {
    "SURPLUS",
    "NONCONFORMITY",
    "UNKNOWN_ORDER",
    "LATE_CANCELLED_DELIVERY",
    "RECEIPT_ERROR",
}


def effective(line):
    if hasattr(line, "latest_corrections"):
        latest = next(iter(line.latest_corrections), None)
    else:
        latest = (
            ReceiptCorrectionLine.objects.filter(line=line)
            .order_by("-correction__revision")
            .first()
        )
    result = {
        key: getattr(latest, key + "_after") if latest else getattr(line, key)
        for key in ["conforming", "nonconforming", "regular", "surplus", "nonconformity"]
    }
    consumption = getattr(line, "source_consumption", None)
    result["source_grant"] = (
        latest.source_grant_after
        if latest
        else (consumption.reinstated_quantity if consumption else 0)
    )
    return result


def item_state(identifier):
    with connection.cursor() as cursor:
        cursor.execute("SELECT public.reserve_m8_item_state(%s)", [identifier])
        value = cursor.fetchone()[0]
        return json.loads(value) if isinstance(value, str) else value
