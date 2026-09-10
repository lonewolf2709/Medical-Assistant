"""Structured logging configuration for MediBuddy."""
import logging
import sys
import time
from pythonjsonlogger import jsonlogger


def setup_logging(level: str = "INFO") -> None:
    """Configure structured JSON logging for the entire application."""
    log_level = getattr(logging, level.upper(), logging.INFO)

    # JSON formatter — every log line is a JSON object
    formatter = jsonlogger.JsonFormatter(
        fmt="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
        rename_fields={"asctime": "timestamp", "levelname": "level", "name": "logger"},
    )

    # asctime renders in local time by default, but the format above claims "Z".
    # Emit real UTC so log timestamps can be compared with the database.
    formatter.converter = time.gmtime

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)
    root_logger.handlers.clear()
    root_logger.addHandler(handler)

    # Suppress noisy third-party loggers
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    # Access logs are useful while debugging and noise in production.
    logging.getLogger("uvicorn.access").setLevel(
        logging.INFO if log_level <= logging.DEBUG else logging.WARNING
    )
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
