import hashlib
import time
from datetime import date, datetime
from urllib.parse import urlencode

from django.http import HttpResponse
from django.shortcuts import render
from django.utils import formats
from django.utils.translation import gettext

from apps.accounts.views import endpoint
from apps.core.api import actor_context

from . import export_jobs, exports, selectors, statistics
from .filters import PARIS, parse
from .snapshots import authorize, materialize, snapshot


def display(value):
    if value is None:
        return gettext("Non renseigné")
    if isinstance(value, datetime):
        return formats.date_format(value.astimezone(PARIS), "DATETIME_FORMAT", use_l10n=True)
    if isinstance(value, date):
        return formats.date_format(value, "DATE_FORMAT", use_l10n=True)
    return str(value)


@endpoint(access="authenticated", methods=["GET"])
def dashboard(request):
    actor = authorize(actor_context(request))
    with snapshot():
        context = statistics.dashboard(actor)
    return render(request, "reporting/dashboard.html", context)


@endpoint(access="authenticated", methods=["GET"])
def analysis(request, name="stocks", personal=False):
    actor = authorize(actor_context(request), personal)
    if personal:
        name = "history"
    title, columns = selectors.dataset(name)
    f = parse(request.GET)
    stock_indicators = None
    with snapshot() as meta:
        fields = selectors.validate_filters(actor, name, f, personal)
        if name == "history":
            selected, count = selectors.history_page(actor, f, personal)
            data = materialize(selected, max_rows=50)
        elif name == "stocks":
            selected, count, counts = selectors.stock_page(actor, f)
            data = materialize(selected, max_rows=50)
            stock_indicators = statistics.stock_indicators(counts)
        else:
            data = materialize(selectors.rows(actor, name, f, personal))
            count = len(data)
        summary = statistics.summarize(name, data)
        chart = statistics.chart(name, data, f)
    page = data if name in {"history", "stocks"} else data[(f.page - 1) * 50 : f.page * 50]
    params = f.public()
    params["page"] = f.page + 1
    context = {
        "title": title,
        "name": name,
        "datasets": [(key, selectors.dataset(key)[0]) for key in selectors.SCHEMAS],
        "columns": columns,
        "rows": [
            {"cells": [display(v) for v in row["cells"]], "url": row.get("url", "")} for row in page
        ],
        "filters": f,
        "query": urlencode(f.public()),
        "next_query": urlencode(params) if count > f.page * 50 else "",
        "count": count,
        "meta": meta,
        "summary": summary,
        "indicators": stock_indicators
        if stock_indicators is not None
        else statistics.indicators(name, data),
        "request_totals": statistics.request_totals(data) if name == "requests" else [],
        "chart": chart,
        "personal": personal,
        "filter_fields": fields,
    }
    return render(request, "reporting/analysis.html", context)


@endpoint(access="authenticated", methods=["POST"])
def export(request, name):
    started = time.monotonic()
    context = actor_context(request)
    actor = authorize(context)
    f = parse(request.GET)
    selectors.dataset(name)
    content, meta, count = export_jobs.generate(actor, name, f, started=started)
    exports.audit_generated(context, name, content, f, meta, count)
    response = HttpResponse(
        content, content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    response["Content-Disposition"] = (
        f'attachment; filename="reserve-{name}-{meta["extracted"].date()}.xlsx"'
    )
    response["X-Export-SHA256"] = hashlib.sha256(content).hexdigest()
    return response
