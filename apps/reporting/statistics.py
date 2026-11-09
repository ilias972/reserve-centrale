"""Agrégats sans mélange de produits/unités ; aucun mouvement sur lecture."""

from collections import defaultdict

from django.db.models import Count, F, Q, Window
from django.utils.translation import gettext

from apps.cases.classification import open_counts
from apps.core.policies import check_role
from apps.purchasing import recommendations
from apps.purchasing.models import OrderLine, PurchaseOrder

from .filters import PARIS


def dashboard(actor):
    check_role(actor, {"ADMIN"})
    recs = recommendations.query()
    open_cases = open_counts(actor)
    stock_counts = recs.aggregate(
        needs=Count("pk", filter=Q(_rec_state="ACTIVE", _need__gt=0)),
        shortages=Count("pk", filter=Q(_D=0, status="ACTIVE", counted_at__isnull=False)),
    )
    incomplete = list(
        PurchaseOrder.objects.filter(
            pk__in=OrderLine.objects.filter(quantity__gt=F("received") + F("cancelled")).values(
                "order_id"
            )
        )
        .annotate(reporting_total=Window(Count("pk")))
        .order_by("purchased_on", "id")[:10]
    )
    return {
        "counts": {
            "needs": stock_counts["needs"],
            "verify": open_cases["TO_VERIFY"],
            "reports": open_cases["REPORTS"],
            "incomplete": incomplete[0].reporting_total if incomplete else 0,
            "shortages": stock_counts["shortages"],
        },
        "needs": [
            recommendations.data(p) for p in recs.filter(_rec_state="ACTIVE", _need__gt=0)[:10]
        ],
        "shortages": [
            recommendations.data(p)
            for p in recs.filter(_D=0, status="ACTIVE", counted_at__isnull=False)[:10]
        ],
        "incomplete": incomplete,
    }


def summarize(name, data):
    """Agrégats issus exactement des mêmes lignes filtrées que l'écran/export."""
    groups = defaultdict(lambda: {"quantity": 0, "initial": 0})
    if name in {"deliveries", "receipts"}:
        for row in data:
            c = row["cells"]
            key = (row["product_id"], c[4], c[6])
            groups[key]["quantity"] += c[8]
            groups[key]["initial"] += c[7]
        return [{"product": k[1], "unit": k[2], **v} for k, v in groups.items()]
    if name == "purchases":
        totals = defaultdict(lambda: {"amount": 0, "known": 0, "missing": 0})
        for row in data:
            c = row["cells"]
            if c[4] is None:
                totals[gettext("Not specified")]["missing"] += 1
            else:
                totals[c[5]]["amount"] += c[4]
                totals[c[5]]["known"] += 1
        return [{"currency": k, **v} for k, v in totals.items()]
    return []


def chart(name, data, filters):
    # Un seul produit obligatoire : aucune somme de références différentes.
    if name != "deliveries" or not filters.product:
        return []
    amounts = defaultdict(int)
    for row in data:
        c = row["cells"]
        amounts[c[0].astimezone(PARIS).date()] += c[8]
    maximum = max(amounts.values(), default=1) or 1
    return [{"day": k, "quantity": v, "maximum": maximum} for k, v in sorted(amounts.items())]


def stock_indicators(counts):
    return [
        (gettext("References inventoried"), counts["known"]),
        (gettext("References not inventoried"), counts["unknown"]),
        (gettext("Unavailable references"), counts["shortage"]),
        (gettext("References at or below the threshold"), counts["below"]),
        (gettext("References above the threshold"), counts["above"]),
    ]


def indicators(name, data):
    """Compteurs de références/documents, jamais une quantité physique hétérogène."""
    if name == "stocks":
        known = [r["cells"] for r in data if r["cells"][4] is not None]
        return stock_indicators(
            {
                "known": len(known),
                "unknown": len(data) - len(known),
                "shortage": sum(c[6] == 0 for c in known),
                "below": sum(c[6] <= c[7] for c in known),
                "above": sum(c[6] > c[7] for c in known),
            }
        )
    if name == "requests":
        ids = {r["request_id"] for r in data}
        opened = {r["request_id"] for r in data if r["cells"][11] > 0}
        partial = {r["request_id"] for r in data if r["cells"][9] > 0 and r["cells"][11] > 0}
        return [
            (gettext("Distinct requests"), len(ids)),
            (gettext("Open requests"), len(opened)),
            (gettext("Partially fulfilled and open requests"), len(partial)),
            (gettext("Closed requests"), len(ids - opened)),
        ]
    return []


def request_totals(data):
    groups = {}
    for row in data:
        c = row["cells"]
        key = (row["product_id"], c[7], row["floor_id"], c[4], c[3])
        target = groups.setdefault(
            key,
            {
                "product": c[5],
                "unit": c[7],
                "floor": c[4],
                "floor_code": row["floor_code"],
                "role": c[3],
                "requested": 0,
                "delivered": 0,
                "closed": 0,
                "reserved": 0,
            },
        )
        for name, index in [("requested", 8), ("delivered", 9), ("closed", 10), ("reserved", 11)]:
            target[name] += c[index]
    return list(groups.values())
