"""
Globalny DRF exception handler dla KLIK.

Ujednolica format ciała błędu zgodny z `docs/c2b/integration/INFO.md`:

    {
        "error": {
            "code": "404_ALIAS_NOT_FOUND",
            "message": "Alias dla podanego numeru nie istnieje.",
            "timestamp": "2026-04-26T12:00:00Z"
        }
    }

Rejestrowany globalnie w `core/settings/base.py`:

    REST_FRAMEWORK = {
        'EXCEPTION_HANDLER': 'common.exceptions.klik_exception_handler',
        ...
    }

Wcześniej żył lokalnie w `aliases/views.py` (`aliases_exception_handler`).
Przeniesiony tutaj bo dotyczy całego API, nie tylko aliasów — codes/payments
też powinny zwracać ten sam format.
"""

from django.utils import timezone
from rest_framework.views import exception_handler


def _error_body(code: str, message: str) -> dict:
    """Format ciała błędu zgodny z INFO.md."""
    return {
        'error': {
            'code': code,
            'message': message,
            'timestamp': timezone.now().isoformat(),
        }
    }


def klik_exception_handler(exc, context):
    """DRF exception handler ujednolicający format błędów.

    Dla wyjątków DRF (APIException i subklasy) zwraca:
        {"error": {"code": "...", "message": "...", "timestamp": "..."}}

    Dla wyjątków poza DRF (np. nieobsłużony Python error) zwraca None →
    Django sam zwróci 500 (z DEBUG=False — generyczny komunikat).

    Source of `code`:
    1. `exc.default_code` (jeśli ustawione na klasie wyjątku — nasze custom
       APIException-y mają to ustawione, np. '404_ALIAS_NOT_FOUND').
    2. fallback: numer status code jako string ('404', '500').

    Source of `message`:
    1. `response.data['detail']` (typowy format DRF).
    2. `str(response.data)` jeśli to nie dict.
    """
    response = exception_handler(exc, context)
    if response is None:
        # Wyjątek nieobsługiwany przez DRF (np. Python error). Niech Django
        # zwróci standardową 500-tkę — nie próbujemy się tu mądrzyć.
        return None

    code = getattr(exc, 'default_code', None) or str(response.status_code)

    detail = response.data
    if isinstance(detail, dict) and 'detail' in detail:
        message = str(detail['detail'])
    elif isinstance(detail, list) and detail:
        # DRF czasem zwraca listę (np. lista błędów walidacji). Bierzemy pierwszy
        # czytelny — jak ktoś chce wszystkie, niech sięgnie do `response.data`.
        message = str(detail[0])
    else:
        message = str(detail)

    response.data = _error_body(code, message)
    return response
