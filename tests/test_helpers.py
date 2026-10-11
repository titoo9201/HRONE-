"""Unit tests for the pure helpers (R1-R5, R8). No database needed."""
from datetime import datetime, timezone

import pytest

from app.main import (
    IST,
    attendance_date,
    compute_half_day,
    compute_late_minutes,
    compute_overtime,
    compute_work_hours,
    dt_to_ms,
    ms_to_dt,
    round_half_up,
)


def ist(y, mo, d, h, mi, s=0, us=0):
    """Build a tz-aware instant from an IST wall-clock time."""
    return datetime(y, mo, d, h, mi, s, us, tzinfo=IST)


# ---- instants / R1 truncation ---------------------------------------------- #
def test_ms_roundtrip_and_truncation():
    ms = 1_783_312_500_900  # ...500.900 -> .900 s must be dropped
    dt = ms_to_dt(ms)
    assert dt.tzinfo is not None and dt.microsecond == 0
    assert dt_to_ms(dt) == 1_783_312_500_000


def test_naive_datetime_treated_as_utc():
    naive = datetime(2026, 7, 6, 3, 58)
    aware = datetime(2026, 7, 6, 3, 58, tzinfo=timezone.utc)
    assert dt_to_ms(naive) == dt_to_ms(aware)


# ---- R1 attendance date ------------------------------------------------------ #
def test_date_is_ist_not_utc():
    # 00:30 IST on the 7th is 19:00 UTC on the 6th; the IST date wins.
    assert attendance_date(ist(2026, 7, 7, 0, 30), "09:30", "18:30") == "2026-07-07"


def test_overnight_punch_in_before_shift_end_belongs_to_previous_day():
    assert attendance_date(ist(2026, 7, 7, 0, 30), "22:00", "06:00") == "2026-07-06"
    assert attendance_date(ist(2026, 7, 7, 5, 59), "22:00", "06:00") == "2026-07-06"


def test_overnight_punch_in_at_or_after_shift_end_is_same_day():
    assert attendance_date(ist(2026, 7, 7, 6, 0), "22:00", "06:00") == "2026-07-07"
    assert attendance_date(ist(2026, 7, 6, 21, 55), "22:00", "06:00") == "2026-07-06"


def test_shift_end_equal_to_start_is_overnight():
    assert attendance_date(ist(2026, 7, 7, 1, 0), "09:00", "09:00") == "2026-07-06"


# ---- R2 late ---------------------------------------------------------------- #
@pytest.mark.parametrize(
    "h,m,s,expected",
    [
        (9, 40, 0, 0),    # exactly 10:00 after 09:30 is still grace
        (9, 40, 1, 10),   # 1 second over -> floor(10:01 / 1 min) = 10
        (10, 15, 59, 45),
        (9, 29, 0, 0),    # early
        (9, 50, 0, 20),
    ],
)
def test_late_minutes(h, m, s, expected):
    assert compute_late_minutes(ist(2026, 7, 6, h, m, s), "2026-07-06", "09:30") == expected


def test_late_overnight_after_midnight_counts_from_previous_evening():
    # Shift 22:00-06:00, punched in 00:30 next day -> 150 min after 22:00.
    p = ist(2026, 7, 7, 0, 30)
    assert compute_late_minutes(p, "2026-07-06", "22:00") == 150


def test_late_overnight_early_punch_is_zero():
    assert compute_late_minutes(ist(2026, 7, 6, 21, 55), "2026-07-06", "22:00") == 0


# ---- R3 overtime ------------------------------------------------------------ #
@pytest.mark.parametrize(
    "h,m,s,expected",
    [
        (18, 59, 59, 0),    # 29 min 59 s -> floor 29 -> below 30 -> 0
        (19, 0, 0, 30),     # exactly 30
        (19, 10, 0, 40),
        (19, 10, 59, 40),   # floor, not round
        (18, 45, 0, 0),
        (17, 0, 0, 0),      # left early
    ],
)
def test_overtime(h, m, s, expected):
    out = ist(2026, 7, 6, h, m, s)
    assert compute_overtime(out, "2026-07-06", "09:30", "18:30") == expected


def test_overtime_overnight_shift_end_is_next_day():
    # Sample doc 7: out 06:40 on the 7th, shift 22:00-06:00 started on the 6th -> 40.
    out = ist(2026, 7, 7, 6, 40)
    assert compute_overtime(out, "2026-07-06", "22:00", "06:00") == 40


# ---- R4 work hours (half-up) ------------------------------------------------ #
def test_work_hours_half_up_not_bankers():
    p_in = ist(2026, 7, 6, 9, 0)
    # 4050 s = 1.125 h exactly. round(1.125, 2) == 1.12 (banker's / binary); half-up is 1.13.
    p_out = ist(2026, 7, 6, 10, 7, 30)
    assert compute_work_hours(p_in, p_out) == 1.13


def test_work_hours_sample_docs():
    assert compute_work_hours(ist(2026, 7, 6, 9, 28), ist(2026, 7, 6, 18, 35)) == 9.12
    assert compute_work_hours(ist(2026, 7, 6, 10, 5), ist(2026, 7, 6, 19, 10)) == 9.08
    assert compute_work_hours(ist(2026, 7, 6, 21, 55), ist(2026, 7, 7, 6, 40)) == 8.75


def test_work_hours_repeating_decimal():
    # 1 s -> 0.000277... -> 0.00 ; 6 min -> 0.1
    assert compute_work_hours(ist(2026, 7, 6, 9, 0, 0), ist(2026, 7, 6, 9, 0, 1)) == 0.0
    assert compute_work_hours(ist(2026, 7, 6, 9, 0), ist(2026, 7, 6, 9, 6)) == 0.1


# ---- R5 half day ------------------------------------------------------------ #
def test_half_day_uses_rounded_hours():
    assert compute_half_day(3.5) is True
    assert compute_half_day(4.49) is True
    assert compute_half_day(4.50) is False
    assert compute_half_day(None) is False


def test_half_day_boundary_via_rounding():
    # 4h29m57s = 4.499166.. h -> rounds to 4.50 -> NOT a half day, even though the raw value is < 4.5.
    p_in = ist(2026, 7, 6, 9, 0)
    p_out = ist(2026, 7, 6, 13, 29, 57)
    wh = compute_work_hours(p_in, p_out)
    assert wh == 4.5 and compute_half_day(wh) is False


# ---- R8 ---------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "value,places,expected",
    [(2.675, 2, 2.68), (1.125, 2, 1.13), (0.00005, 4, 0.0001), (0.12345, 4, 0.1235), (2.5, 0, 3.0)],
)
def test_round_half_up(value, places, expected):
    assert round_half_up(value, places) == expected


# ---- weekdays_between (R7 working days) -------------------------------------------------------- #
from datetime import date as _date  # noqa: E402

from app.main import month_bounds, weekdays_between  # noqa: E402


def test_weekdays_between_hand_values():
    assert weekdays_between(_date(2031, 3, 1), _date(2031, 3, 31)) == 21   # 1st is a Saturday
    assert weekdays_between(_date(2031, 3, 17), _date(2031, 3, 31)) == 11  # mid-month joiner
    assert weekdays_between(_date(2031, 3, 1), _date(2031, 3, 2)) == 0     # a weekend
    assert weekdays_between(_date(2031, 3, 3), _date(2031, 3, 3)) == 1
    assert weekdays_between(_date(2031, 4, 5), _date(2031, 3, 31)) == 0    # starts after the end


def test_weekdays_between_matches_brute_force():
    from datetime import timedelta
    start = _date(2026, 1, 1)
    for a in range(0, 40):
        for b in range(a - 2, 60):
            s, e = start + timedelta(days=a), start + timedelta(days=b)
            expected = sum(1 for i in range((e - s).days + 1) if (s + timedelta(days=i)).weekday() < 5)
            assert weekdays_between(s, e) == expected, (s, e)


def test_month_bounds():
    assert month_bounds("2026-02") == ("2026-02-01", "2026-02-28")
    assert month_bounds("2028-02") == ("2028-02-01", "2028-02-29")
    assert month_bounds("2026-12") == ("2026-12-01", "2026-12-31")
