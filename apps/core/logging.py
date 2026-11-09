import json
import logging
from datetime import UTC, datetime


class JsonFormatter(logging.Formatter):
    """Liste blanche : jamais de message d'exception ou de corps de requête."""

    def format(self, record):
        result = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "event": getattr(record, "event", "technical.event"),
            "correlation_id": getattr(record, "correlation_id", None),
        }
        for field in [
            "error_type",
            "method",
            "route",
            "status",
            "duration_ms",
            "job_id",
            "attempt_id",
            "channel",
            "attempt",
            "recipient_category",
            "provider_http_status",
        ]:
            if hasattr(record, field):
                result[field] = getattr(record, field)
        return json.dumps(result, ensure_ascii=False)
