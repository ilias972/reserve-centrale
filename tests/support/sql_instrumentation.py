"""Temporary instrumentation used by PostgreSQL performance tests only."""

import time
from contextvars import ContextVar

from django.db import connection
from django.db.backends.utils import CursorWrapper
from django.template.base import Template
from psycopg import ServerCursor

measure = ContextVar("test_template_time", default=None)
_original_render = Template.render
_original_getattr = CursorWrapper.__getattr__


def timed_render(template, *args, **kwargs):
    stats = measure.get()
    if stats is None:
        return _original_render(template, *args, **kwargs)
    started = time.perf_counter()
    stats["render_depth"] += 1
    try:
        return _original_render(template, *args, **kwargs)
    finally:
        stats["render_depth"] -= 1
        if stats["render_depth"] == 0:
            stats["render_ms"] += (time.perf_counter() - started) * 1000


def cursor_attribute(wrapper, attribute):
    result = _original_getattr(wrapper, attribute)
    if attribute not in {"fetchone", "fetchmany", "fetchall"}:
        return result

    def measured_fetch(*args, **kwargs):
        stats = measure.get()
        if stats is None:
            return result(*args, **kwargs)
        started = time.perf_counter()
        try:
            return result(*args, **kwargs)
        finally:
            duration = (time.perf_counter() - started) * 1000
            stats["sql_ms"] += duration
            stats["sql_fetch_ms"] += duration
            stats["fetch_calls"] += 1
            if isinstance(wrapper.cursor, ServerCursor):
                stats["queries"] += 1

    return measured_fetch


class TimingMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        stats = {
            "sql_ms": 0.0,
            "sql_execute_ms": 0.0,
            "sql_fetch_ms": 0.0,
            "fetch_calls": 0,
            "queries": 0,
            "render_ms": 0.0,
            "render_depth": 0,
        }
        token = measure.set(stats)
        started = time.perf_counter()

        def measure_execute(execute, sql, params, many, context):
            query_started = time.perf_counter()
            try:
                return execute(sql, params, many, context)
            finally:
                stats["queries"] += 1
                duration = (time.perf_counter() - query_started) * 1000
                stats["sql_ms"] += duration
                stats["sql_execute_ms"] += duration

        try:
            with connection.execute_wrapper(measure_execute):
                response = self.get_response(request)
            stats["server_ms"] = (time.perf_counter() - started) * 1000
            for key, value in stats.items():
                if key != "render_depth":
                    response["X-Test-" + key.replace("_", "-")] = str(round(value, 3))
            return response
        finally:
            measure.reset(token)
