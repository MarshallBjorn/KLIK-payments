"""Wspólne fixturki dla testów ledger.

Konfigurujemy Celery na sync execution (`task_always_eager`) — wszystkie
`task.delay()` / `task.apply_async()` w testach wykonają się synchronicznie
w tym samym wątku, bez brokera. To standardowa praktyka dla testów Django+Celery.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def celery_eager(settings):
    """Wymusza synchroniczne wykonanie Celery tasków w testach.

    `task_always_eager=True` → task.delay() = task() — żaden broker.
    `task_eager_propagates=True` → wyjątki w taskach są wyrzucane do testu
    zamiast cicho zjadane.
    """
    settings.CELERY_TASK_ALWAYS_EAGER = True
    settings.CELERY_TASK_EAGER_PROPAGATES = True
    yield
