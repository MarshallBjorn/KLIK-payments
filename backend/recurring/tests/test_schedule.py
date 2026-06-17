"""Testy kalkulacji kalendarza (recurring/schedule.py).

Edge case'y wprost z docs/reccuring/integration/INFO.md ("Schedule i kalendarz
wykonania"): klampowanie MONTHLY do końca miesiąca bez driftu kotwicy,
29 lutego w roku przestępnym, brak catch-upu przy resume.

Godzinę execution pinujemy fixturą `pin_execution_hour` (default z env mógłby
się różnić między środowiskami).
"""

from datetime import UTC, date, datetime

import pytest

from recurring import schedule
from recurring.models import RecurringCycle

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def pin_execution_hour(settings):
    settings.RECURRING_EXECUTION_HOUR_UTC = 8


class TestFirstRunAt:
    def test_first_run_at_uses_execution_hour(self):
        assert schedule.first_run_at(date(2026, 6, 1)) == datetime(2026, 6, 1, 8, tzinfo=UTC)

    def test_execution_hour_configurable(self, settings):
        settings.RECURRING_EXECUTION_HOUR_UTC = 14
        assert schedule.first_run_at(date(2026, 6, 1)).hour == 14


class TestComputeNextRunAt:
    def _next(self, start, cycle, after):
        return schedule.compute_next_run_at(start_date=start, cycle=cycle, after=after)

    def test_daily_advances_one_day(self):
        start = date(2026, 6, 1)
        after = datetime(2026, 6, 1, 8, tzinfo=UTC)
        assert self._next(start, RecurringCycle.DAILY, after) == datetime(2026, 6, 2, 8, tzinfo=UTC)

    def test_weekly_advances_seven_days(self):
        start = date(2026, 6, 1)
        after = datetime(2026, 6, 1, 8, tzinfo=UTC)
        assert self._next(start, RecurringCycle.WEEKLY, after) == datetime(
            2026, 6, 8, 8, tzinfo=UTC
        )

    def test_monthly_advances_same_day_of_month(self):
        start = date(2026, 6, 15)
        after = datetime(2026, 6, 15, 8, tzinfo=UTC)
        assert self._next(start, RecurringCycle.MONTHLY, after) == datetime(
            2026, 7, 15, 8, tzinfo=UTC
        )

    def test_monthly_clamps_to_end_of_february(self):
        """Mandate z 31 stycznia → kolejny run 28 lutego (rok nieprzestępny)."""
        start = date(2026, 1, 31)
        after = datetime(2026, 1, 31, 8, tzinfo=UTC)
        assert self._next(start, RecurringCycle.MONTHLY, after) == datetime(
            2026, 2, 28, 8, tzinfo=UTC
        )

    def test_monthly_anchor_does_not_drift_after_clamp(self):
        """Po klampnięciu do 28 lutego kolejny slot wraca na 31. (marzec)."""
        start = date(2026, 1, 31)
        after_february = datetime(2026, 2, 28, 8, tzinfo=UTC)
        assert self._next(start, RecurringCycle.MONTHLY, after_february) == datetime(
            2026, 3, 31, 8, tzinfo=UTC
        )

    def test_monthly_leap_year_february_29(self):
        """Mandate z 31 grudnia 2027 → 29 lutego 2028 (rok przestępny)."""
        start = date(2027, 12, 31)
        after = datetime(2028, 1, 31, 8, tzinfo=UTC)
        assert self._next(start, RecurringCycle.MONTHLY, after) == datetime(
            2028, 2, 29, 8, tzinfo=UTC
        )

    def test_resume_returns_first_upcoming_slot_no_catchup(self):
        """Resume po długiej pauzie — pierwszy NADCHODZĄCY slot, nie missed."""
        start = date(2026, 6, 1)
        resumed_at = datetime(2028, 3, 15, 14, 30, tzinfo=UTC)
        assert self._next(start, RecurringCycle.MONTHLY, resumed_at) == datetime(
            2028, 4, 1, 8, tzinfo=UTC
        )
        # DAILY: jutro (dziś 8:00 już minęło o 14:30)
        assert self._next(start, RecurringCycle.DAILY, resumed_at) == datetime(
            2028, 3, 16, 8, tzinfo=UTC
        )
        # WEEKLY: kotwica na poniedziałkach (start 2026-06-01 to poniedziałek)
        weekly = self._next(start, RecurringCycle.WEEKLY, resumed_at)
        assert weekly == datetime(2028, 3, 20, 8, tzinfo=UTC)
        assert weekly.weekday() == 0

    def test_strictly_after_same_day_before_hour(self):
        """`after` przed godziną execution tego samego dnia → slot tego dnia."""
        start = date(2026, 6, 1)
        after = datetime(2026, 6, 10, 7, 59, tzinfo=UTC)
        assert self._next(start, RecurringCycle.DAILY, after) == datetime(
            2026, 6, 10, 8, tzinfo=UTC
        )

    def test_after_before_start_returns_first_slot(self):
        start = date(2026, 6, 1)
        after = datetime(2026, 1, 1, tzinfo=UTC)
        assert self._next(start, RecurringCycle.MONTHLY, after) == datetime(
            2026, 6, 1, 8, tzinfo=UTC
        )

    def test_unknown_cycle_raises(self):
        with pytest.raises(ValueError):
            self._next(date(2026, 6, 1), 'HOURLY', datetime(2026, 6, 1, tzinfo=UTC))


class TestEstimateTotalRuns:
    def test_open_ended_returns_zero(self):
        assert schedule.estimate_total_runs(date(2026, 6, 1), None, RecurringCycle.MONTHLY) == 0

    def test_monthly_year_inclusive(self):
        """Rok MONTHLY = 13 runów (start + 12 miesięcy, run w end_date się odbywa)."""
        assert (
            schedule.estimate_total_runs(date(2026, 6, 1), date(2027, 6, 1), RecurringCycle.MONTHLY)
            == 13
        )

    def test_daily_week(self):
        assert (
            schedule.estimate_total_runs(date(2026, 6, 1), date(2026, 6, 7), RecurringCycle.DAILY)
            == 7
        )

    def test_weekly_month(self):
        assert (
            schedule.estimate_total_runs(date(2026, 6, 1), date(2026, 6, 29), RecurringCycle.WEEKLY)
            == 5
        )

    def test_end_before_start_returns_zero(self):
        assert (
            schedule.estimate_total_runs(date(2026, 6, 1), date(2026, 5, 1), RecurringCycle.DAILY)
            == 0
        )
