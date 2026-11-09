import hashlib
import json
import re
from uuid import UUID

from django.utils.translation import gettext

from .errors import BusinessError


def invalid_input():
    return BusinessError("INVALID_INPUT", gettext("Données reçues invalides."), status=400)


def parse_uuid(value):
    try:
        return UUID(str(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise invalid_input() from exc


def payload_hash(payload):
    if not isinstance(payload, dict):
        raise invalid_input()
    try:
        encoded = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise invalid_input() from exc
    if len(encoded) > 32768:
        raise invalid_input()
    return hashlib.sha256(encoded).hexdigest()


def validate_binding(key, fingerprint):
    if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", key):
        raise invalid_input()
    if not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
        raise invalid_input()


def safe_record(value):
    """Le noyau ne copie jamais automatiquement le corps HTTP dans l'audit."""
    forbidden = {"password", "secret", "token", "cookie", "authorization", "idempotency_key"}
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str) or any(word in key.casefold() for word in forbidden):
                raise ValueError("Champ sensible interdit dans une trace persistée.")
            safe_record(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            safe_record(item)
    json.dumps(value, allow_nan=False)
