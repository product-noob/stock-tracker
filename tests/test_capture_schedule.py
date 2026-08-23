"""Offline tests for configurable exchange-capture schedule slots."""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from capture_schedule import capture_interval_hours, capture_slot_key  # noqa: E402


class TestCaptureSchedule(unittest.TestCase):
    def test_default_is_six_hours(self):
        self.assertEqual(capture_interval_hours({}), 6)

    def test_default_slots_are_ist_anchored(self):
        # 06:29 UTC = 11:59 IST; 06:30 UTC = 12:00 IST.
        before = datetime(2026, 8, 23, 6, 29, tzinfo=timezone.utc)
        after = datetime(2026, 8, 23, 6, 30, tzinfo=timezone.utc)
        self.assertEqual(capture_slot_key(before), "20260823T06H06")
        self.assertEqual(capture_slot_key(after), "20260823T12H06")

    def test_interval_is_configurable_and_encoded_in_key(self):
        self.assertEqual(capture_interval_hours(
            {"EXCHANGE_CAPTURE_INTERVAL_HOURS": "4"}), 4)
        instant = datetime(2026, 8, 23, 6, 30, tzinfo=timezone.utc)
        self.assertEqual(
            capture_slot_key(instant, interval_hours=4), "20260823T12H04"
        )

    def test_invalid_interval_fails_closed(self):
        for value in ("", "0", "5", "25", "six"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                capture_interval_hours({"EXCHANGE_CAPTURE_INTERVAL_HOURS": value})

    def test_naive_clock_is_rejected(self):
        with self.assertRaises(ValueError):
            capture_slot_key(datetime(2026, 8, 23), interval_hours=6)


if __name__ == "__main__":
    unittest.main()
