"""Deterministic configurable schedule slots for exchange capture."""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Mapping, Optional

IST = timezone(timedelta(hours=5, minutes=30))
CAPTURE_INTERVAL_ENV = "EXCHANGE_CAPTURE_INTERVAL_HOURS"
DEFAULT_CAPTURE_INTERVAL_HOURS = 6


def capture_interval_hours(environ: Optional[Mapping[str, str]] = None) -> int:
    """Return a validated interval that partitions each IST day exactly."""
    source = os.environ if environ is None else environ
    raw = source.get(CAPTURE_INTERVAL_ENV, str(DEFAULT_CAPTURE_INTERVAL_HOURS))
    try:
        hours = int(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{CAPTURE_INTERVAL_ENV} must be an integer") from None
    if hours < 1 or hours > 24 or 24 % hours:
        raise ValueError(
            f"{CAPTURE_INTERVAL_ENV} must be a positive divisor of 24"
        )
    return hours


def capture_slot_key(
    now: Optional[datetime] = None, *, interval_hours: Optional[int] = None
) -> str:
    """Return the deterministic IST slot key containing ``now``.

    For the six-hour default the slots begin at 00:00, 06:00, 12:00 and 18:00
    IST. The interval is encoded in the key so changing configuration cannot
    collide with prior runs.
    """
    hours = capture_interval_hours() if interval_hours is None else interval_hours
    if hours < 1 or hours > 24 or 24 % hours:
        raise ValueError("interval_hours must be a positive divisor of 24")
    instant = now or datetime.now(timezone.utc)
    if instant.tzinfo is None:
        raise ValueError("capture slot clock must be timezone-aware")
    local = instant.astimezone(IST)
    start_hour = (local.hour // hours) * hours
    return f"{local:%Y%m%d}T{start_hour:02d}H{hours:02d}"
