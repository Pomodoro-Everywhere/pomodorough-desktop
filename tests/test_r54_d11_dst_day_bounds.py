"""R54-D11: generated-break day bounds follow local midnights on DST days.

The bounds run from the first instant of the source's local date to the
first instant of the next date. Each bound uses the zone rules in force at
its own midnight, so a daylight-saving change day is 23 or 25 hours long.
Android atStartOfDay(zone) and Apple Calendar.dateInterval(of: .day) give
the same bounds.

Each case pins the process zone with TZ plus time.tzset. POSIX rule strings
need no zone database, and each one matches its named zone for 2026. The
skipped-day case names an IANA zone and reads the system zone database.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import pomodorough.storage_canonical_reconciliation as reconciliation
from pomodorough.shared_core import SharedCore
from pomodorough.storage_canonical_reconciliation import generated_break_day_bounds
from pomodorough.storage_completion import TimerCompletionPolicy

LONDON = "GMT0BST,M3.5.0/1,M10.5.0"
LOS_ANGELES = "PST8PDT,M3.2.0,M11.1.0"
# America/Havana changes clocks at local midnight, so one midnight is skipped
# and one is repeated.
HAVANA = "CST5CDT,M3.2.0/0,M11.1.0/1"
# Asia/Beirut skips midnight east of UTC, where midnight read as UTC falls
# after the change.
BEIRUT = "EET-2EEST,M3.5.0/0,M10.5.0/0"
# Havana's rules mirrored east of UTC. No current zone repeats midnight on
# that side, but the zone format allows it, and the earlier pass must win.
EAST_MIRROR = "<+02>-2<+03>,M3.2.0/0,M11.1.0/1"
# America/Santiago ends daylight saving time at midnight west of UTC. The
# offset at midnight read as UTC is the old one, so only the probe at the
# first guess finds the next day's start.
SANTIAGO = "<-04>4<-03>,M9.1.6/24,M4.1.6/24"
# Samoa moved west of the date line by skipping 2011-12-30. A POSIX rule
# cannot express a one-off change, so this zone needs the zone database.
APIA = "Pacific/Apia"
PLUS_FIVE = "<+05>-5"
UTC_ZONE = "UTC0"

# Offsets in hours at 12:00 UTC on each date. The fixture checks them, because
# a zone the platform cannot load falls back to UTC silently, and the DST
# cases would then pass against the fixed-offset bug.
ZONE_OFFSETS = {
    LONDON: {date(2026, 1, 15): 0, date(2026, 7, 15): 1},
    LOS_ANGELES: {date(2026, 1, 15): -8, date(2026, 7, 15): -7},
    HAVANA: {date(2026, 1, 15): -5, date(2026, 7, 15): -4},
    BEIRUT: {date(2026, 1, 15): 2, date(2026, 7, 15): 3},
    EAST_MIRROR: {date(2026, 1, 15): 2, date(2026, 7, 15): 3},
    SANTIAGO: {date(2026, 1, 15): -3, date(2026, 7, 15): -4},
    APIA: {date(2011, 12, 29): -10, date(2011, 12, 31): 14},
    "AAA0BBB-2,M3.5.0/23,M10.5.0/2": {date(2026, 1, 15): 0, date(2026, 7, 15): 2},
    "Antarctica/Casey": {date(2010, 3, 3): 11, date(2010, 3, 6): 8},
    "AAA-1BBB-3,M3.5.0/2,M10.5.0/1": {date(2026, 1, 15): 1, date(2026, 7, 15): 3},
    PLUS_FIVE: {date(2026, 1, 15): 5, date(2026, 7, 15): 5},
    UTC_ZONE: {date(2026, 1, 15): 0, date(2026, 7, 15): 0},
}

# Each case: zone, expected start, expected end, and sources inside that local
# day. Sources cover the first instant, both passes of a repeated hour, both
# sides of a gap, and the last millisecond.
DAYS = [
    pytest.param(
        LONDON,
        "2026-10-24T23:00:00.000Z",
        "2026-10-26T00:00:00.000Z",
        (
            "2026-10-24T23:00:00.000Z",
            "2026-10-25T00:30:00.000Z",
            "2026-10-25T01:30:00.000Z",
            "2026-10-25T12:00:00.000Z",
            "2026-10-25T23:59:59.999Z",
        ),
        id="london-2026-10-25-fall-back",
    ),
    pytest.param(
        LONDON,
        "2026-03-29T00:00:00.000Z",
        "2026-03-29T23:00:00.000Z",
        (
            "2026-03-29T00:00:00.000Z",
            "2026-03-29T00:59:59.999Z",
            "2026-03-29T01:00:00.000Z",
            "2026-03-29T12:00:00.000Z",
            "2026-03-29T22:59:59.999Z",
        ),
        id="london-2026-03-29-spring-forward",
    ),
    pytest.param(
        LONDON,
        "2026-05-31T23:00:00.000Z",
        "2026-06-01T23:00:00.000Z",
        (
            "2026-05-31T23:00:00.000Z",
            "2026-06-01T12:00:00.000Z",
            "2026-06-01T22:59:59.999Z",
        ),
        id="london-2026-06-01-normal",
    ),
    pytest.param(
        LOS_ANGELES,
        "2026-11-01T07:00:00.000Z",
        "2026-11-02T08:00:00.000Z",
        (
            "2026-11-01T07:00:00.000Z",
            "2026-11-01T08:30:00.000Z",
            "2026-11-01T09:30:00.000Z",
            "2026-11-01T20:00:00.000Z",
            "2026-11-02T07:59:59.999Z",
        ),
        id="los-angeles-2026-11-01-fall-back",
    ),
    pytest.param(
        LOS_ANGELES,
        "2026-03-08T08:00:00.000Z",
        "2026-03-09T07:00:00.000Z",
        (
            "2026-03-08T08:00:00.000Z",
            "2026-03-08T09:59:59.999Z",
            "2026-03-08T10:00:00.000Z",
            "2026-03-08T20:00:00.000Z",
            "2026-03-09T06:59:59.999Z",
        ),
        id="los-angeles-2026-03-08-spring-forward",
    ),
    pytest.param(
        LOS_ANGELES,
        "2026-01-31T08:00:00.000Z",
        "2026-02-01T08:00:00.000Z",
        (
            "2026-01-31T08:00:00.000Z",
            "2026-01-31T20:00:00.000Z",
            "2026-02-01T07:59:59.999Z",
        ),
        id="los-angeles-2026-01-31-normal-month-end",
    ),
    pytest.param(
        HAVANA,
        "2026-03-08T05:00:00.000Z",
        "2026-03-09T04:00:00.000Z",
        (
            "2026-03-08T05:00:00.000Z",
            "2026-03-08T05:30:00.000Z",
            "2026-03-08T17:00:00.000Z",
            "2026-03-09T03:59:59.999Z",
        ),
        id="havana-2026-03-08-skipped-midnight",
    ),
    pytest.param(
        HAVANA,
        "2026-11-01T04:00:00.000Z",
        "2026-11-02T05:00:00.000Z",
        (
            "2026-11-01T04:00:00.000Z",
            "2026-11-01T04:30:00.000Z",
            "2026-11-01T05:30:00.000Z",
            "2026-11-01T17:00:00.000Z",
            "2026-11-02T04:59:59.999Z",
        ),
        id="havana-2026-11-01-repeated-midnight",
    ),
    pytest.param(
        BEIRUT,
        "2026-03-28T22:00:00.000Z",
        "2026-03-29T21:00:00.000Z",
        (
            "2026-03-28T22:00:00.000Z",
            "2026-03-29T10:00:00.000Z",
            "2026-03-29T20:59:59.999Z",
        ),
        id="beirut-2026-03-29-skipped-midnight",
    ),
    pytest.param(
        BEIRUT,
        "2026-10-23T21:00:00.000Z",
        "2026-10-24T22:00:00.000Z",
        (
            "2026-10-23T21:00:00.000Z",
            "2026-10-24T20:30:00.000Z",
            "2026-10-24T21:30:00.000Z",
            "2026-10-24T21:59:59.999Z",
        ),
        id="beirut-2026-10-24-repeated-last-hour",
    ),
    pytest.param(
        EAST_MIRROR,
        "2026-03-07T22:00:00.000Z",
        "2026-03-08T21:00:00.000Z",
        (
            "2026-03-07T22:00:00.000Z",
            "2026-03-08T10:00:00.000Z",
            "2026-03-08T20:59:59.999Z",
        ),
        id="east-mirror-2026-03-08-skipped-midnight",
    ),
    pytest.param(
        EAST_MIRROR,
        "2026-10-31T21:00:00.000Z",
        "2026-11-01T22:00:00.000Z",
        (
            "2026-10-31T21:00:00.000Z",
            "2026-10-31T21:30:00.000Z",
            "2026-10-31T22:30:00.000Z",
            "2026-11-01T12:00:00.000Z",
            "2026-11-01T21:59:59.999Z",
        ),
        id="east-mirror-2026-11-01-repeated-midnight",
    ),
    pytest.param(
        SANTIAGO,
        "2026-04-04T03:00:00.000Z",
        "2026-04-05T04:00:00.000Z",
        (
            "2026-04-04T03:00:00.000Z",
            "2026-04-05T02:30:00.000Z",
            "2026-04-05T03:30:00.000Z",
            "2026-04-05T03:59:59.999Z",
        ),
        id="santiago-2026-04-04-repeated-last-hour",
    ),
    pytest.param(
        SANTIAGO,
        "2026-04-05T04:00:00.000Z",
        "2026-04-06T04:00:00.000Z",
        (
            "2026-04-05T04:00:00.000Z",
            "2026-04-05T16:00:00.000Z",
            "2026-04-06T03:59:59.999Z",
        ),
        id="santiago-2026-04-05-after-repeated-hour",
    ),
    pytest.param(
        SANTIAGO,
        "2026-09-06T04:00:00.000Z",
        "2026-09-07T03:00:00.000Z",
        (
            "2026-09-06T04:00:00.000Z",
            "2026-09-06T16:00:00.000Z",
            "2026-09-07T02:59:59.999Z",
        ),
        id="santiago-2026-09-06-skipped-midnight",
    ),
    pytest.param(
        APIA,
        "2011-12-29T10:00:00.000Z",
        "2011-12-30T10:00:00.000Z",
        (
            "2011-12-29T10:00:00.000Z",
            "2011-12-29T22:00:00.000Z",
            "2011-12-30T09:59:59.999Z",
        ),
        id="apia-2011-12-29-next-date-skipped",
    ),
    pytest.param(
        APIA,
        "2011-12-30T10:00:00.000Z",
        "2011-12-31T10:00:00.000Z",
        (
            "2011-12-30T10:00:00.000Z",
            "2011-12-30T22:00:00.000Z",
            "2011-12-31T09:59:59.999Z",
        ),
        id="apia-2011-12-31-after-skipped-date",
    ),
    pytest.param(
        UTC_ZONE,
        "2026-03-29T00:00:00.000Z",
        "2026-03-30T00:00:00.000Z",
        ("2026-03-29T23:00:00.000Z",),
        id="utc-gap-control",
    ),
    pytest.param(
        "AAA0BBB-2,M3.5.0/23,M10.5.0/2",
        "2026-03-29T00:00:00.000Z",
        "2026-03-29T23:00:00.000Z",
        ("2026-03-29T22:59:59.999Z",),
        id="forward-gap-crosses-midnight-before",
    ),
    pytest.param(
        "AAA0BBB-2,M3.5.0/23,M10.5.0/2",
        "2026-03-29T23:00:00.000Z",
        "2026-03-30T22:00:00.000Z",
        ("2026-03-29T23:00:00.000Z", "2026-03-29T23:30:00.000Z"),
        id="forward-gap-crosses-midnight-after",
    ),
    pytest.param(
        UTC_ZONE,
        "2026-10-24T00:00:00.000Z",
        "2026-10-25T00:00:00.000Z",
        ("2026-10-24T12:00:00.000Z",),
        id="utc-control-before-multi-hour-fallback",
    ),
    pytest.param(
        "Antarctica/Casey",
        "2010-03-04T13:00:00.000Z",
        "2010-03-05T16:00:00.000Z",
        ("2010-03-04T14:30:00.000Z", "2010-03-04T16:30:00.000Z"),
        id="casey-three-hour-midnight-fallback",
    ),
    pytest.param(
        "AAA-1BBB-3,M3.5.0/2,M10.5.0/1",
        "2026-10-24T21:00:00.000Z",
        "2026-10-25T23:00:00.000Z",
        ("2026-10-24T21:30:00.000Z", "2026-10-24T23:30:00.000Z"),
        id="posix-two-hour-midnight-fallback",
    ),
    pytest.param(
        UTC_ZONE,
        "2026-10-25T00:00:00.000Z",
        "2026-10-26T00:00:00.000Z",
        (
            "2026-10-25T00:00:00.000Z",
            "2026-10-25T12:00:00.000Z",
            "2026-10-25T23:59:59.999Z",
        ),
        id="utc-2026-10-25-no-regression",
    ),
]


def _ms(stamp: str) -> int:
    return int(datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp() * 1000)


def _offset_hours(day: date) -> float:
    noon = datetime(day.year, day.month, day.day, 12, tzinfo=UTC)
    offset = noon.astimezone().utcoffset()
    assert offset is not None
    return offset / timedelta(hours=1)


@pytest.fixture
def local_zone(request) -> Iterator[str]:
    zone = request.param
    if not hasattr(time, "tzset"):
        pytest.skip(
            "time.tzset is POSIX-only; Windows cannot pin the local zone in-process"
        )
    previous = os.environ.get("TZ")
    os.environ["TZ"] = zone
    time.tzset()
    try:
        for day, hours in ZONE_OFFSETS[zone].items():
            assert _offset_hours(day) == hours, (zone, day)
        yield zone
    finally:
        if previous is None:
            del os.environ["TZ"]
        else:
            os.environ["TZ"] = previous
        time.tzset()


@pytest.mark.parametrize(
    ("local_zone", "start", "end", "sources"), DAYS, indirect=["local_zone"]
)
def test_day_bounds_run_between_local_midnights(
    local_zone: str, start: str, end: str, sources: tuple[str, ...]
) -> None:
    for source in sources:
        assert generated_break_day_bounds(_ms(source)) == (start, end), source
        assert _ms(start) <= _ms(source) < _ms(end)


@pytest.mark.parametrize(
    ("local_zone", "start", "end", "sources"), DAYS, indirect=["local_zone"]
)
def test_neighbouring_days_share_their_boundary(
    local_zone: str, start: str, end: str, sources: tuple[str, ...]
) -> None:
    assert generated_break_day_bounds(_ms(start) - 1)[1] == start
    assert generated_break_day_bounds(_ms(end))[0] == end


@pytest.mark.parametrize(
    ("local_zone", "expected"),
    [
        pytest.param(
            UTC_ZONE,
            ("1970-01-01T00:00:00.000Z", "1970-01-02T00:00:00.000Z"),
            id="utc",
        ),
        pytest.param(
            PLUS_FIVE,
            ("1969-12-31T19:00:00.000Z", "1970-01-01T19:00:00.000Z"),
            id="utc-plus-five",
        ),
    ],
    indirect=["local_zone"],
)
def test_epoch_day_never_reads_localtime_before_the_epoch(
    local_zone: str, expected: tuple[str, str]
) -> None:
    # Windows localtime rejects negative timestamps, and the unix-epoch bounds
    # test in test_unit_positive runs on Windows CI. The record also proves
    # that the bounds read the zone through the patched module.
    seconds_read: list[float] = []

    def windows_localtime(seconds: float) -> time.struct_time:
        seconds_read.append(seconds)
        if seconds < 0:
            raise OSError(22, "Invalid argument")
        return time.localtime(seconds)

    windows_time = SimpleNamespace(localtime=windows_localtime)
    with patch.object(reconciliation, "time", windows_time, create=True):
        assert generated_break_day_bounds(0) == expected
    assert seconds_read


@pytest.fixture(scope="module")
def core() -> SharedCore:
    return SharedCore()


def _completed_focus(index: int, stamp: str) -> dict:
    return {
        "id": f"session-{index}",
        "timerId": f"timer-{index}",
        "phase": "focus",
        "status": "completed",
        "plannedDurationMs": 1_500_000,
        "completedAt": stamp,
        "endedAt": stamp,
    }


@pytest.mark.parametrize(
    ("local_zone", "finished_at", "completed_minute", "expected"),
    [
        # The first local hour of 2026-10-25 is 23:00Z to 00:00Z on 10-24.
        pytest.param(
            LONDON,
            "2026-10-25T23:45:00.000Z",
            "2026-10-24T23:{minute}:00.000Z",
            "long_break",
            id="fall-back-day-keeps-its-first-hour",
        ),
        # 23:00Z to 00:00Z on 03-28 is still 2026-03-28 in London.
        pytest.param(
            LONDON,
            "2026-03-29T22:30:00.000Z",
            "2026-03-28T23:{minute}:00.000Z",
            "short_break",
            id="spring-forward-day-excludes-the-previous-evening",
        ),
    ],
    indirect=["local_zone"],
)
def test_core_counts_sessions_inside_the_local_day(
    core: SharedCore,
    local_zone: str,
    finished_at: str,
    completed_minute: str,
    expected: str,
) -> None:
    policy = TimerCompletionPolicy(
        lambda: core, lambda: "device-a", lambda: "offline", lambda _k, d: d
    )
    history = [
        _completed_focus(index, completed_minute.format(minute=minute))
        for index, minute in enumerate(("05", "15", "25", "35"))
    ]
    timer = {"id": "timer-now", "phase": "focus", "status": "running"}

    # Core starts a long break after the fourth focus session of the day.
    assert policy.preview_selected_phase(timer, history, False, finished_at) == expected
