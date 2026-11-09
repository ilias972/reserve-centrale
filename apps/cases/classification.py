"""A11.3–4 : une nature principale par dossier, indépendante de son traitement."""

from django.db.models import Count

from . import reading

CATEGORIES = {"TO_VERIFY": "À vérifier", "REPORTS": "Signalements"}
BY_KIND = {
    "SURPLUS": "TO_VERIFY",
    "DELIVERY_ERROR": "TO_VERIFY",
    "RECEIPT_ERROR": "TO_VERIFY",
    "STOCK_CONFLICT": "REPORTS",
    "SHORTAGE": "REPORTS",
    "NONCONFORMITY": "REPORTS",
    "UNKNOWN_ORDER": "REPORTS",
    "LATE_CANCELLED_DELIVERY": "REPORTS",
}


def primary_category(kind):
    # Une future nature devra être classée explicitement, jamais par son statut.
    return BY_KIND[kind]


def open_counts(actor):
    """Lecture du périmètre réel, un id par dossier même après une jointure."""
    counts = dict.fromkeys(CATEGORIES, 0)
    rows = (
        reading.query(actor)
        .filter(status__in=["OPEN", "UNDER_REVIEW"])
        .order_by()
        .values("kind")
        .annotate(total=Count("pk", distinct=True))
    )
    for row in rows:
        counts[primary_category(row["kind"])] += row["total"]
    return counts
