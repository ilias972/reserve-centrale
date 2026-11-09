"""Réservations fictives pour éprouver le registre M3, jamais chargées en production.
Aucune demande, affectation ou route de M4 : contrat interne uniquement.
"""

from apps.core.commands import Command, Effect
from apps.core.policies import check_version
from apps.inventory.primitives import append_change, lock_stocks


def lock(actor, identifier, payload):
    return lock_stocks({identifier})[identifier]


def apply(execution, stock, payload):
    check_version(stock.balance.version, payload["version"])
    delta = payload["delta"]
    entry = append_change(
        execution,
        stock,
        kind="RESERVE" if delta > 0 else "RELEASE",
        physical=stock.balance.physical,
        delta_r=delta,
        sequence=1,
    )
    return Effect({"entry_id": str(entry.pk)})


COMMAND = Command(
    "m3.test.reservation",
    frozenset({"ASSISTANT"}),
    lambda a, i: None,
    lambda a, i: None,
    apply,
    lock_payload_resources=lock,
)
