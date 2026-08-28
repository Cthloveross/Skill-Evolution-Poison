from __future__ import annotations

import os
from datetime import datetime, timezone

import watch_acceleration_trainer as watchdog


def test_process_exists_for_current_and_missing_process() -> None:
    assert watchdog.process_exists(os.getpid())
    assert not watchdog.process_exists(2**30)


def test_timestamp_age_rejects_invalid_and_accepts_current_time() -> None:
    assert watchdog.timestamp_age_seconds(None) is None
    assert watchdog.timestamp_age_seconds("invalid") is None
    age = watchdog.timestamp_age_seconds(datetime.now(timezone.utc).isoformat())
    assert age is not None
    assert age < 2
