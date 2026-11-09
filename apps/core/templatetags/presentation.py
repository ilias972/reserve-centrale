"""Formats d’affichage français, sans altération des horodatages conservés."""

from datetime import date, datetime
from zoneinfo import ZoneInfo

from django import template
from django.utils import formats, timezone
from django.utils.dateparse import parse_date, parse_datetime
from django.utils.translation import gettext

register = template.Library()
PARIS = ZoneInfo("Europe/Paris")


def parsed(value):
    if isinstance(value, (datetime, date)):
        return value
    if not isinstance(value, str):
        return None
    try:
        return parse_date(value) if len(value) == 10 else parse_datetime(value)
    except (ValueError, TypeError):
        return None


@register.filter
def ui_date(value):
    value = parsed(value)
    if value is None:
        return gettext("Date indisponible")
    if isinstance(value, datetime):
        value = timezone.localtime(value, PARIS) if timezone.is_aware(value) else value
    return formats.date_format(value, "DATE_FORMAT")


@register.filter
def ui_datetime(value):
    value = parsed(value)
    if value is None:
        return gettext("Date indisponible")
    if not isinstance(value, datetime):
        return ui_date(value)
    value = timezone.localtime(value, PARIS) if timezone.is_aware(value) else value
    return formats.date_format(value, "DATETIME_FORMAT")
