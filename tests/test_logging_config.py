"""Log records must be timestamped in the timezone they claim."""
import json
import logging
from datetime import datetime, timezone


def test_timestamps_are_utc_as_the_z_suffix_claims(capsys):
    from app.logging_config import setup_logging

    setup_logging()
    logging.getLogger("test.logger").info("hello")

    line = [l for l in capsys.readouterr().out.strip().splitlines() if l.startswith("{")][-1]
    stamp = json.loads(line)["timestamp"]
    logged = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)

    drift = abs((logged - datetime.now(timezone.utc)).total_seconds())
    assert drift < 60, f"timestamp is off by {drift}s — it is local time labelled as UTC"


def test_access_logs_are_enabled_in_debug_and_quiet_otherwise():
    from app.logging_config import setup_logging

    setup_logging(level="DEBUG")
    assert logging.getLogger("uvicorn.access").level == logging.INFO

    setup_logging(level="INFO")
    assert logging.getLogger("uvicorn.access").level == logging.WARNING
