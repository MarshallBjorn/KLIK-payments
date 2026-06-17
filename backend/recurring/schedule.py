"""Kalkulacja kalendarza wykonania zleceń stałych (next_run_at).

Reguły z docs/reccuring/integration/INFO.md, sekcja "Schedule i kalendarz
wykonania":

- Wszystkie sloty wypadają o `RECURRING_EXECUTION_HOUR_UTC` (UTC).
- `start_date` jest sztywnym punktem odniesienia — brak driftu. Mandate
  startujący 15-go zawsze stara się trafić w 15-ty (slot N-ty liczymy zawsze
  OD start_date, a nie od poprzedniego runu).
- MONTHLY: `relativedelta(months=+n)` — jeśli kolejny miesiąc nie ma takiego
  dnia, używamy ostatniego dnia miesiąca (31 stycznia → 28/29 lutego),
  ale kotwica zostaje (kolejny slot to znów 31. dzień, nie 28.).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta

from dateutil.relativedelta import relativedelta
from django.conf import settings

from recurring.models import RecurringCycle


def _execution_time() -> time:
    return time(hour=settings.RECURRING_EXECUTION_HOUR_UTC, tzinfo=UTC)


def slot_at(start_date: date, cycle: str, n: int) -> datetime:
    """Zwraca datetime n-tego slotu (n=0 → pierwszy run w start_date)."""
    if cycle == RecurringCycle.DAILY:
        slot_date = start_date + timedelta(days=n)
    elif cycle == RecurringCycle.WEEKLY:
        slot_date = start_date + timedelta(weeks=n)
    elif cycle == RecurringCycle.MONTHLY:
        # relativedelta klampuje do ostatniego dnia miesiąca, ale liczona
        # zawsze od start_date — kotwica (np. 31.) nie dryfuje.
        slot_date = start_date + relativedelta(months=n)
    else:
        raise ValueError(f'Nieznany cycle: {cycle!r}')
    return datetime.combine(slot_date, _execution_time())


def first_run_at(start_date: date) -> datetime:
    """next_run_at dla świeżo utworzonego mandate: start_date + EXECUTION_HOUR."""
    return datetime.combine(start_date, _execution_time())


def compute_next_run_at(*, start_date: date, cycle: str, after: datetime) -> datetime:
    """Pierwszy slot ŚCIŚLE PO `after`, zakotwiczony w start_date.

    Dwa zastosowania:
    - advance po runie: after = scheduled_for właśnie wykonanego runu,
    - resume z pauzy: after = now (pierwszy nadchodzący slot, BEZ catch-upu
      missed runów — patrz INFO.md "Resume po pauzie").
    """
    if cycle not in RecurringCycle.values:
        raise ValueError(f'Nieznany cycle: {cycle!r}')

    # Dolne oszacowanie n żeby nie iterować od zera (mandate może być
    # wznawiany po latach — DAILY dałby tysiące iteracji).
    delta_days = (after.date() - start_date).days
    if delta_days < 0:
        n = 0
    elif cycle == RecurringCycle.DAILY:
        n = delta_days
    elif cycle == RecurringCycle.WEEKLY:
        n = delta_days // 7
    else:  # MONTHLY
        rd = relativedelta(after.date(), start_date)
        n = max(rd.years * 12 + rd.months, 0)

    while slot_at(start_date, cycle, n) <= after:
        n += 1
    return slot_at(start_date, cycle, n)


def estimate_total_runs(start_date: date, end_date: date | None, cycle: str) -> int:
    """Orientacyjna liczba planowanych runów do end_date włącznie.

    0 dla mandate open-ended (end_date=NULL). Używane tylko w
    `executions_summary.scheduled` (GET /recurring/{id}).
    """
    if end_date is None:
        return 0
    if end_date < start_date:
        return 0
    delta_days = (end_date - start_date).days
    if cycle == RecurringCycle.DAILY:
        return delta_days + 1
    if cycle == RecurringCycle.WEEKLY:
        return delta_days // 7 + 1
    # MONTHLY — liczba pełnych miesięcy + run startowy. Klampowanie do końca
    # miesiąca nie zmienia liczności slotów.
    rd = relativedelta(end_date, start_date)
    return rd.years * 12 + rd.months + 1
