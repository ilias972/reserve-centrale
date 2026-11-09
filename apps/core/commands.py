"""Registre explicite, vide en M0 hors tests. Aucun type de commande fourni par le client
ne peut charger un module, une fonction ou une permission dynamiquement.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from uuid import UUID

from django.utils.translation import gettext

from .errors import BusinessError


@dataclass(frozen=True)
class Event:
    event_type: str
    aggregate_id: UUID
    revision: int
    payload: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Effect:
    resource: dict
    before: dict = field(default_factory=dict)
    after: dict = field(default_factory=dict)
    details: list = field(default_factory=list)
    events: tuple[Event, ...] = ()
    http_status: int = 200
    # Remise volatile (création/reset d'accès), jamais copiée dans le résultat
    # idempotent, l'audit ou l'outbox. Absente des rejeux après perte réseau.
    once: dict | None = None


@dataclass(frozen=True)
class Command:
    name: str
    roles: frozenset[str]
    authorize: Callable
    lock_resources: Callable
    apply: Callable
    lock_references: Callable | None = None
    record_rejection: Callable | None = None
    lock_accounts: Callable | None = None
    lock_payload_accounts: Callable | None = None
    lock_payload_references: Callable | None = None
    lock_payload_resources: Callable | None = None


_registry: dict[str, Command] = {}
_fact_projections: list[Callable] = []


def register_fact_projection(project):
    """Extensions explicites, en lecture métier, exécutées avant le commit commun."""
    if project not in _fact_projections:
        _fact_projections.append(project)


def projected_events(operation):
    return tuple(event for project in _fact_projections for event in project(operation))


def register_command(command):
    if command.name in _registry:
        raise ValueError("Commande déjà enregistrée.")
    _registry[command.name] = command


def get_command(name):
    if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_.]{0,79}", name):
        raise BusinessError("INVALID_INPUT", gettext("Type d'opération invalide."), status=400)
    try:
        return _registry[name]
    except KeyError as exc:
        raise BusinessError(
            "UNKNOWN_COMMAND", gettext("Cette action n'est pas disponible."), status=403
        ) from exc
