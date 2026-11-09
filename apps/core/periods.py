"""Bornes calendaires des lectures de faits d'origine, sans agrégat ni export M10."""

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.db.models.functions import TruncDate

from .payloads import invalid_input

PARIS = ZoneInfo("Europe/Paris")


def origin_period(rows, field, start, end):
    """Période inclusive de dates locales ; borne haute exclusive, y compris DST."""
    if type(start) is not date or type(end) is not date or start > end or end == date.max:
        raise invalid_input()
    lower = datetime.combine(start, time.min, tzinfo=PARIS)
    upper = datetime.combine(end + timedelta(days=1), time.min, tzinfo=PARIS)
    return rows.filter(**{field + "__gte": lower, field + "__lt": upper}).annotate(
        origin_day=TruncDate(field, tzinfo=PARIS)
    )
