"""Filtres fermés et périodes locales [début, fin[ ; limites A14 provisoires."""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.conf import settings
from django.utils import timezone
from django.utils.translation import gettext

from apps.core.errors import BusinessError
from apps.core.payloads import parse_uuid

PARIS = ZoneInfo("Europe/Paris")


def limit(name, default):
    return int(getattr(settings, "REPORTING_" + name, default))


def refused(message=None):
    if message is None:
        message = gettext("Reduce the period or filters, then try again.")
    return BusinessError("REPORT_LIMIT", message, status=422)


@dataclass(frozen=True)
class Filters:
    start: date
    end: date
    product: str = ""
    floor: str = ""
    actor: str = ""
    role: str = ""
    action: str = ""
    q: str = ""
    state: str = ""
    category: str = ""
    page: int = 1

    @property
    def lower(self):
        return datetime.combine(self.start, time.min, PARIS)

    @property
    def upper(self):
        return datetime.combine(self.end, time.min, PARIS)

    def public(self):
        return {k: str(v) for k, v in vars(self).items() if k != "page" and v != ""}


def parse(params):
    allowed = set(Filters.__dataclass_fields__)
    if set(params) - allowed or any(
        len(params.getlist(k)) != 1 for k in params if hasattr(params, "getlist")
    ):
        raise BusinessError("INVALID_INPUT", gettext("Filtres inconnus ou répétés."), status=400)
    today = timezone.localdate(timezone=PARIS)
    try:
        start = date.fromisoformat(params.get("start") or today.replace(day=1).isoformat())
        end = date.fromisoformat(params.get("end") or (today + timedelta(days=1)).isoformat())
        page = int(params.get("page", 1))
        if not 1 <= page <= 2000 or not start < end or (end - start).days > limit("MAX_DAYS", 366):
            raise ValueError
    except (ValueError, TypeError, OverflowError) as exc:
        raise BusinessError(
            "INVALID_INPUT",
            gettext(
                "Choisissez une période valide : début inclus, fin exclue, au plus 366 jours par défaut."
            ),
            status=400,
        ) from exc
    values = {}
    for key in ["product", "floor", "actor"]:
        value = params.get(key, "")
        values[key] = str(parse_uuid(value)) if value else ""
    for key in ["role", "action", "q", "state", "category"]:
        values[key] = params.get(key, "")
        if len(values[key]) > 150:
            raise BusinessError("INVALID_INPUT", gettext("Filtre trop long."), status=400)
    if values["role"] not in {"", "ADMIN", "ASSISTANT", "DOCTOR", "REPLENISHER"} or values[
        "category"
    ] not in {"", "TO_VERIFY", "REPORTS"}:
        raise BusinessError("INVALID_INPUT", gettext("Filtre invalide."), status=400)
    return Filters(start, end, page=page, **values)
