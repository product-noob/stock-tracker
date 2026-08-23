"""Unit tests for scripts/fetch_prices.py — pure helpers only.

No network and no database: the DB-touching paths are exercised by running
the real fetcher against the dev DB (see TODO 6 verification); here we pin
the pure logic that correctness depends on.
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import fetch_prices as fp

IST = timezone(timedelta(hours=5, minutes=30))


# --------------------------------------------------------------------- #
# observation hashing / idempotency keys
# --------------------------------------------------------------------- #

def test_observation_hash_is_deterministic_and_order_sensitive():
    a = fp.observation_hash(100.0, 101.0, 99.0, 100.5, 12345, date(2026, 8, 21))
    b = fp.observation_hash(100.0, 101.0, 99.0, 100.5, 12345, date(2026, 8, 21))
    assert a == b
    assert len(a) == 64
    # any value change must change the hash (R-YF-8 revision detection)
    changed = [
        fp.observation_hash(100.1, 101.0, 99.0, 100.5, 12345, date(2026, 8, 21)),
        fp.observation_hash(100.0, 101.1, 99.0, 100.5, 12345, date(2026, 8, 21)),
        fp.observation_hash(100.0, 101.0, 99.0, 100.6, 12345, date(2026, 8, 21)),
        fp.observation_hash(100.0, 101.0, 99.0, 100.5, 12346, date(2026, 8, 21)),
        fp.observation_hash(100.0, 101.0, 99.0, 100.5, 12345, date(2026, 8, 20)),
    ]
    assert all(a != c for c in changed)


def test_observation_hash_ignores_float_noise_beyond_6dp():
    """Same price to 6 decimal places ⇒ same hash (stable re-fetch no-op)."""
    a = fp.observation_hash(727.0499880, 732.0999760, 720.0, 728.55, 10_000_000,
                            date(2026, 8, 21))
    b = fp.observation_hash(727.0499884, 732.0999764, 720.0, 728.5500004,
                            10_000_000, date(2026, 8, 21))
    assert a == b


def test_bar_idempotency_key_format():
    h = fp.observation_hash(1, 1, 1, 1, 0, date(2026, 8, 21))
    key = fp.bar_idempotency_key(42, date(2026, 8, 21), h)
    assert key == f"42:2026-08-21:{h}"


# --------------------------------------------------------------------- #
# frame → rows mapping (data-contract §2)
# --------------------------------------------------------------------- #

def _frame(dates, opens, highs, lows, closes, volumes):
    idx = pd.DatetimeIndex(dates, tz="Asia/Kolkata", name="Date")
    return pd.DataFrame({"Open": opens, "High": highs, "Low": lows,
                         "Close": closes, "Volume": volumes}, index=idx)


def test_frame_to_rows_maps_columns_and_sorts_by_date():
    f = _frame(
        ["2026-08-21 00:00:00+05:30", "2026-08-20 00:00:00+05:30"],
        [725.5, 724.0], [732.1, 725.4], [720.1, 718.2], [728.3, 724.9],
        [1_234_567, 2_345_678])
    rows = fp.frame_to_rows(f)
    assert [r["trade_date"] for r in rows] == [date(2026, 8, 20),
                                               date(2026, 8, 21)]
    assert rows[1] == {"trade_date": date(2026, 8, 21), "open": 725.5,
                       "high": 732.1, "low": 720.1, "close": 728.3,
                       "volume": 1_234_567}


# --------------------------------------------------------------------- #
# R-YF-3 validation
# --------------------------------------------------------------------- #

def _row(td=date(2026, 8, 21), o=100.0, h=101.0, l=99.0, c=100.5, v=1000):
    return {"trade_date": td, "open": o, "high": h, "low": l, "close": c,
            "volume": v}


def test_validate_clean_frame_has_no_problems():
    hard, warnings = fp.validate_frame([_row()])
    assert hard == [] and warnings == []


@pytest.mark.parametrize("mutate,frag", [
    (lambda r: r.update(high=98.0), "OHLC inversion"),   # high below open/close
    (lambda r: r.update(low=101.5), "OHLC inversion"),   # low above close
    (lambda r: r.update(open=-1.0), "non-positive price"),
    (lambda r: r.update(close=0.0), "non-positive price"),
    (lambda r: r.update(volume=-5), "negative volume"),
    (lambda r: r.update(trade_date=date(2026, 8, 22)), "weekend"),  # Saturday
])
def test_validate_hard_violations_fail_loudly(mutate, frag):
    r = _row()
    mutate(r)
    hard, _ = fp.validate_frame([r], today=date(2026, 8, 23))
    assert len(hard) == 1 and frag in hard[0]


def test_validate_future_trade_date_rejected():
    hard, _ = fp.validate_frame([_row(td=date(2026, 8, 24))],
                                today=date(2026, 8, 21))
    assert "future" in hard[0]


def test_zero_volume_is_warning_not_hard_error():
    hard, warnings = fp.validate_frame([_row(v=0)], today=date(2026, 8, 23))
    assert hard == [] and len(warnings) == 1


# --------------------------------------------------------------------- #
# expected-session maths + gap classification
# --------------------------------------------------------------------- #

def test_expected_sessions_weekdays_only_and_today_gating():
    now = datetime(2026, 8, 19, 12, 0, tzinfo=IST)  # Wed midday, before close
    got = fp.expected_sessions(date(2026, 8, 14), date(2026, 8, 19), now_ist=now)
    # Fri 14, Mon 17, Tue 18 expected; Wed 19 excluded until after close IST
    assert got == {date(2026, 8, 14), date(2026, 8, 17), date(2026, 8, 18)}
    now_after = datetime(2026, 8, 19, 16, 0, tzinfo=IST)
    got_after = fp.expected_sessions(date(2026, 8, 19), date(2026, 8, 19),
                                     now_ist=now_after)
    assert got_after == {date(2026, 8, 19)}
    now_before_close = datetime(2026, 8, 19, 15, 29, tzinfo=IST)
    assert fp.expected_sessions(date(2026, 8, 19), date(2026, 8, 19),
                                now_ist=now_before_close) == set()


def test_classify_gaps_holiday_vs_real_gap():
    d1, d2, d3 = date(2026, 3, 4), date(2026, 3, 5), date(2026, 3, 6)
    missing = {
        1: {d1, d3},
        2: {d1},
        3: {d1, d2},
        4: {d1},
        5: {d1},
    }
    hol, gaps = fp.classify_gaps(missing, total_series=5)
    # d1 missing from all 5 series ⇒ holiday candidate for everyone;
    # d3 missing from 1/5 and d2 missing from 1/5 ⇒ genuine gaps
    assert all(set(hol[i]) == {d1} for i in missing)
    assert gaps[1] == [d3] and gaps[3] == [d2]
    assert gaps[2] == [] and gaps[4] == [] and gaps[5] == []


def test_partition_key_format_matches_spec():
    assert fp.partition_key(17) == "listing:17:daily"
