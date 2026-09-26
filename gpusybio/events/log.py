"""Structured logging (spec 13 §1)."""

from __future__ import annotations

import logging
import sys

import structlog


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        stream=sys.stdout,
        format="%(message)s",
        level=getattr(logging, level.upper(), logging.INFO),
    )
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.KeyValueRenderer(
                key_order=["event", "level", "application_id", "gateway_id", "request_id"]
            ),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None, **initial):
    logger = structlog.get_logger(name) if name else structlog.get_logger()
    if initial:
        logger = logger.bind(**initial)
    return logger


# Convenience module loggers (created before setup_logging is fine; structlog
# rebinds processors at configure time).
log = get_logger()
