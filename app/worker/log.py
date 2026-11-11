"""Structured logs: one JSON object per line on stdout.

Only the fields passed explicitly are written. Job payloads, tokens and
exception messages from third parties are never logged.
"""

import json
import logging
import sys
from datetime import UTC, datetime

LOGGER_NAME = "fichaitor.worker"
logger = logging.getLogger(LOGGER_NAME)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "event": record.getMessage(),
        }
        entry.update(getattr(record, "fields", {}))
        return json.dumps(entry, ensure_ascii=False, default=str)


def configure() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    logger.handlers[:] = [handler]
    logger.setLevel(logging.INFO)
    logger.propagate = False
    # httpx logs every request URL at INFO. No tokens there, but it is noise.
    logging.getLogger("httpx").setLevel(logging.WARNING)


def event(name: str, level: int = logging.INFO, **fields: object) -> None:
    logger.log(level, name, extra={"fields": fields})
