"""PT02 : normalisation exacte, aucune correspondance approchée de références."""

import unicodedata

from apps.cases.previews import fingerprint
from apps.core.payloads import invalid_input

from .models import PurchaseOrder


def reference(value):
    if (
        not isinstance(value, str)
        or len(value) > 160
        or any(unicodedata.category(c).startswith("C") for c in value)
    ):
        raise invalid_input()
    key = unicodedata.normalize("NFC", value.strip(" "))
    if len(key) > 160:
        raise invalid_input()
    return value, key


def lines_key(lines):
    return [
        [str(r["product_id"]), r["quantity"]]
        for r in sorted(lines, key=lambda r: str(r["product_id"]))
    ]


def candidates(supplier, purchased_on, key, line_key):
    q = PurchaseOrder.objects.filter(supplier=supplier)
    q = (
        q.filter(reference_key=key)
        if key
        else q.filter(purchased_on=purchased_on, lines_key=line_key)
    )
    rows = list(q.prefetch_related("lines").order_by("pk"))
    facts = [
        {
            "id": str(o.pk),
            "reference": o.reference,
            "supplier_name": o.supplier_name,
            "purchased_on": o.purchased_on.isoformat(),
            "external_reference": o.external_reference,
            "lines": [
                {
                    "product_id": str(line.product_id),
                    "code": line.product_code,
                    "name": line.product_name,
                    "unit": line.unit,
                    "quantity": line.quantity,
                }
                for line in o.lines.all()
            ],
        }
        for o in rows
    ]
    return facts, fingerprint(facts)
