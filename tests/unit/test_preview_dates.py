"""format_preview_datetime -- the one date format for approval-card message
tables."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from privacyfence.preview_dates import format_preview_datetime


class TestFormatPreviewDatetime:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            # Slack's message ts: epoch seconds with a sequence suffix.
            ("1757926320.000100", "2025-09-15 08:52 UTC"),
            (1757926320, "2025-09-15 08:52 UTC"),
            (1757926320.5, "2025-09-15 08:52 UTC"),
            # Telegram: ISO with seconds and an offset.
            ("2026-09-16 18:02:00+00:00", "2026-09-16 18:02 UTC"),
            ("2026-09-16T18:02:00Z", "2026-09-16 18:02 UTC"),
            # Jira: milliseconds and a basic-format offset, converted to UTC.
            ("2026-09-16T20:02:00.000+0200", "2026-09-16 18:02 UTC"),
            # Naive is taken as UTC.
            ("2026-09-16T18:02:00", "2026-09-16 18:02 UTC"),
            (datetime(2026, 9, 16, 18, 2, 59), "2026-09-16 18:02 UTC"),
            (datetime(2026, 9, 16, 20, 2, tzinfo=timezone(timedelta(hours=2))), "2026-09-16 18:02 UTC"),
        ],
    )
    def test_formats_every_connector_shape_the_same_way(self, value, expected):
        assert format_preview_datetime(value) == expected

    @pytest.mark.parametrize("value", ["2026-07-01", date(2026, 7, 1)])
    def test_a_date_without_a_time_gains_no_invented_midnight(self, value):
        assert format_preview_datetime(value) == "2026-07-01"

    @pytest.mark.parametrize("value", [None, ""])
    def test_missing_is_blank(self, value):
        assert format_preview_datetime(value) == ""

    @pytest.mark.parametrize("value", ["yesterday", "nan", "inf", "2026-13-45", "1e400"])
    def test_unparseable_is_shown_raw_rather_than_dropped(self, value):
        assert format_preview_datetime(value) == value
