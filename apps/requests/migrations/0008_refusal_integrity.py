"""Étendre les gardes M5 au refus explicite, sans réécrire les faits existants."""

from importlib import import_module

from django.db import migrations

base = import_module("apps.requests.migrations.0005_delivery_integrity")
OLD_ASSERT = base.SQL.split("\nCREATE OR REPLACE FUNCTION public.reserve_assert_reservations", 1)[0]
NEW_ASSERT = OLD_ASSERT
for old, new in [
    ("claims bigint; nd bigint;", "claims bigint; refusals bigint; nd bigint;"),
    (
        "INTO n,v,cancels,claims FROM",
        ",count(*) FILTER(WHERE kind='REFUSED') INTO n,v,cancels,claims,refusals FROM",
    ),
    ("OR cancels>1 OR claims>1", "OR cancels+refusals>1 OR claims>1 OR (refusals=1 AND claims<>1)"),
    ("IF cancels=1 THEN", "IF cancels+refusals=1 THEN"),
    (
        "expected:='CANCELLED';",
        "expected:=CASE WHEN refusals=1 THEN 'REFUSED' ELSE 'CANCELLED' END;",
    ),
    (
        "WHEN expected='PARTIAL_CLOSED' THEN 'SHORTAGE' ELSE ''",
        "WHEN expected='PARTIAL_CLOSED' THEN 'SHORTAGE' WHEN expected='REFUSED' THEN 'REFUSAL' ELSE ''",
    ),
    ("WHEN cancels=1 THEN l.quantity", "WHEN cancels+refusals=1 THEN l.quantity"),
    ("e.kind IN ('CLAIMED','DELIVERED')", "e.kind IN ('CLAIMED','DELIVERED','REFUSED')"),
    (
        "WHEN 'CLAIMED' THEN 'requests.claim' ELSE",
        "WHEN 'CLAIMED' THEN 'requests.claim' WHEN 'REFUSED' THEN 'requests.refuse' ELSE",
    ),
    ("e.kind='CANCELLED' AND e.revision", "e.kind IN ('CANCELLED','REFUSED') AND e.revision"),
    (
        "WHEN 'CLAIMED' THEN 'request.claimed' ELSE",
        "WHEN 'CLAIMED' THEN 'request.claimed' WHEN 'REFUSED' THEN 'request.refused' ELSE",
    ),
    ("a.kind='CANCELLED' AND", "a.kind IN ('CANCELLED','REFUSED') AND"),
]:
    if NEW_ASSERT.count(old) != 1:
        raise RuntimeError(f"La garde M5 figée ne correspond pas au remplacement : {old}")
    NEW_ASSERT = NEW_ASSERT.replace(old, new, 1)

OLD_GUARD = base.guard
NEW_GUARD = OLD_GUARD.replace(
    "('requests.claim','requests.deliver')",
    "('requests.claim','requests.deliver','requests.refuse')",
)


class Migration(migrations.Migration):
    dependencies = [("requests", "0007_request_refusal")]
    operations = [migrations.RunSQL(NEW_GUARD + NEW_ASSERT, OLD_GUARD + OLD_ASSERT)]
