"""
Celery Beat schedule dla KLIK.

Zadania periodyczne:

1. **Settlement session per strefa** — co `SESSION_INTERVAL_MINUTES_{zone}` minut
   (z `.env`). Default 1440 (24h) na produkcji. Dla demo można ustawić 2-15 min.

2. **P2P daily fee accrual** — codziennie o 23:55 UTC. Agreguje countery lookupów
   z Redisa, tworzy LedgerEntry per bank, czyści countery.

Schedule używa `django_celery_beat.DatabaseScheduler` (zdefiniowany
w base.py CELERY_BEAT_SCHEDULER) — to znaczy że *te* schedule poniżej trafią do
DB przy `python manage.py migrate` + restart `beat`. Operator może też dodać
ad-hoc taski przez Django Admin → "Periodic tasks" bez restartu.

Trzymamy schedule jako Python dict (zamiast `IntervalSchedule.objects.create`
w migracji) bo:
- Schedule jest częścią konfiguracji aplikacji, nie danych.
- Łatwo dostosować przez `.env` per środowisko.
- DatabaseScheduler robi sync ze schedule przy starcie beat-a.
"""

from __future__ import annotations

from celery.schedules import crontab


def build_beat_schedule(
    interval_pl: int,
    interval_eu: int,
    interval_uk: int,
    interval_us: int,
) -> dict:
    """Buduje słownik CELERY_BEAT_SCHEDULE.

    Args:
        interval_*: interwał w minutach per strefa (z `.env`).

    Returns:
        Dict gotowy do podłożenia jako `CELERY_BEAT_SCHEDULE` w settings.
    """
    return {
        # ----------------------------------------------------------------------
        # Settlement sessions — jedna entry per strefa, każda z własnym interwałem.
        # Beat per strefa pozwala mieć różne częstotliwości (np. PL co 5min na demo,
        # US co 24h w produkcji) bez zmian w kodzie.
        # ----------------------------------------------------------------------
        'settlement-pl': {
            'task': 'ledger.run_settlement_session',
            'schedule': interval_pl * 60,  # sekundy
            'args': ('PL',),
            'options': {
                # queue/expires: gdyby beat się przyspał i wystrzelił 5x naraz,
                # ekspirujemy stare na poziomie brokera.
                'expires': interval_pl * 60,
            },
        },
        'settlement-eu': {
            'task': 'ledger.run_settlement_session',
            'schedule': interval_eu * 60,
            'args': ('EU',),
            'options': {'expires': interval_eu * 60},
        },
        'settlement-uk': {
            'task': 'ledger.run_settlement_session',
            'schedule': interval_uk * 60,
            'args': ('UK',),
            'options': {'expires': interval_uk * 60},
        },
        'settlement-us': {
            'task': 'ledger.run_settlement_session',
            'schedule': interval_us * 60,
            'args': ('US',),
            'options': {'expires': interval_us * 60},
        },
        # ----------------------------------------------------------------------
        # P2P daily fee accrual — codziennie o 23:55 UTC.
        # Wybór 23:55 (a nie 00:05 następnego dnia): chcemy zamknąć dzień
        # PRZED północą żeby data agregacji była jednoznaczna.
        # ----------------------------------------------------------------------
        'p2p-daily-fee-accrual': {
            'task': 'ledger.accrue_p2p_lookup_fees',
            'schedule': crontab(hour=23, minute=55),
            'options': {'expires': 60 * 60},  # 1h — jeśli przegapimy okno, lepiej skip
        },
    }
