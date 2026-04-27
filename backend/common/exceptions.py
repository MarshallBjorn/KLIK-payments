"""
Globalny system wyjątków API dla KLIK.

Wszystkie endpointy zwracają błędy w spójnym formacie:
{
    "error": {
        "code": "404_CODE_EXPIRED",
        "message": "...",
        "timestamp": "2026-04-26T14:00:00Z"
    }
}
"""

import logging
from datetime import UTC, datetime

from django.core.exceptions import ValidationError as DjangoValidationError
from django.http import Http404
from rest_framework import status
from rest_framework.exceptions import (
    APIException,
    AuthenticationFailed,
    NotAuthenticated,
    NotFound,
)
from rest_framework.exceptions import PermissionDenied as DRFPermissionDenied
from rest_framework.response import Response
from rest_framework.views import exception_handler

logger = logging.getLogger('klik')


class KlikAPIException(APIException):
    """
    Bazowa klasa wyjątku API w KLIK.

    Subklasy ustawiają:
        status_code: HTTP status (np. 404)
        default_code: kod błędu (np. '404_CODE_EXPIRED')
        default_detail: ludzka wiadomość
    """

    status_code = status.HTTP_400_BAD_REQUEST
    default_code = '400_BAD_REQUEST'
    default_detail = 'Bad request.'

    def __init__(self, detail=None, code=None):
        if detail is None:
            detail = self.default_detail
        if code is None:
            code = self.default_code
        self.detail = detail
        self.code = code


# ---------------------------------------------------------------------------
# Konkretne wyjątki API — używane przez views/services
# ---------------------------------------------------------------------------


class BadRequestError(KlikAPIException):
    status_code = status.HTTP_400_BAD_REQUEST
    default_code = '400_BAD_REQUEST'
    default_detail = 'Niepoprawne żądanie.'


class InvalidAmountError(KlikAPIException):
    status_code = status.HTTP_400_BAD_REQUEST
    default_code = '400_INVALID_AMOUNT'
    default_detail = 'Niepoprawna kwota.'


class UnauthorizedError(KlikAPIException):
    status_code = status.HTTP_401_UNAUTHORIZED
    default_code = '401_UNAUTHORIZED'
    default_detail = 'Brak lub niepoprawne uwierzytelnienie.'


class BankInactiveError(KlikAPIException):
    status_code = status.HTTP_403_FORBIDDEN
    default_code = '403_BANK_INACTIVE'
    default_detail = 'Bank jest nieaktywny.'


class C2BNotEnabledError(KlikAPIException):
    status_code = status.HTTP_403_FORBIDDEN
    default_code = '403_C2B_NOT_ENABLED'
    default_detail = 'Bank nie ma aktywowanego modułu C2B.'


class P2PNotEnabledError(KlikAPIException):
    status_code = status.HTTP_403_FORBIDDEN
    default_code = '403_P2P_NOT_ENABLED'
    default_detail = 'Bank nie ma aktywowanego modułu P2P.'


class InsufficientPermissionsError(KlikAPIException):
    status_code = status.HTTP_403_FORBIDDEN
    default_code = '403_INSUFFICIENT_PERMISSIONS'
    default_detail = 'Brak uprawnień do tej operacji.'


class CodeExpiredError(KlikAPIException):
    status_code = status.HTTP_404_NOT_FOUND
    default_code = '404_CODE_EXPIRED'
    default_detail = 'Kod utracił ważność lub nie istnieje.'


class CodeGoneError(KlikAPIException):
    """410 Gone — używane gdy idempotency replay celuje w wygasły zasób."""

    status_code = status.HTTP_410_GONE
    default_code = '410_CODE_GONE'
    default_detail = 'Kod wygenerowany w pierwotnym żądaniu już wygasł. Zleć nowe generate.'


class TransactionNotFoundError(KlikAPIException):
    status_code = status.HTTP_404_NOT_FOUND
    default_code = '404_TRANSACTION_NOT_FOUND'
    default_detail = 'Transakcja nie istnieje.'


class MerchantNotFoundError(KlikAPIException):
    status_code = status.HTTP_404_NOT_FOUND
    default_code = '404_MERCHANT_NOT_FOUND'
    default_detail = 'Merchant o podanym ID nie istnieje.'


class CodeAlreadyUsedError(KlikAPIException):
    status_code = status.HTTP_409_CONFLICT
    default_code = '409_CODE_ALREADY_USED'
    default_detail = 'Kod został już użyty w innej transakcji.'


class IdempotencyConflictError(KlikAPIException):
    status_code = status.HTTP_409_CONFLICT
    default_code = '409_IDEMPOTENCY_CONFLICT'
    default_detail = 'Idempotency-Key użyty z innym payloadem.'


class PrematureConfirmError(KlikAPIException):
    status_code = status.HTTP_409_CONFLICT
    default_code = '409_PREMATURE_CONFIRM'
    default_detail = 'Confirm wywołany przed zakończeniem autoryzacji.'


class ZoneMismatchError(KlikAPIException):
    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
    default_code = '422_ZONE_MISMATCH'
    default_detail = 'Niezgodność stref.'


class CurrencyMismatchError(KlikAPIException):
    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
    default_code = '422_CURRENCY_MISMATCH'
    default_detail = 'Niezgodność walut.'


class NoActiveMSCError(KlikAPIException):
    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
    default_code = '422_NO_ACTIVE_MSC'
    default_detail = 'Agent nie ma aktywnej umowy MSC. Skontaktuj się z operatorem.'


class TransactionAlreadyClosedError(KlikAPIException):
    status_code = status.HTTP_409_CONFLICT
    default_code = '409_TRANSACTION_ALREADY_CLOSED'
    default_detail = 'Transakcja jest już zamkniętanie i nie może być potwierdzona ani odrzucona.'


class ConfirmDecisionConflictError(KlikAPIException):
    status_code = status.HTTP_409_CONFLICT
    default_code = '409_CONFIRM_DECISION_CONFLICT'
    default_detail = (
        'Transakcja została wcześniej potwierdzona z inną decyzją. '
        'Replay z odmienną decyzją jest niedozwolony.'
    )


# ---------------------------------------------------------------------------
# Custom DRF exception handler
# ---------------------------------------------------------------------------


def klik_exception_handler(exc, context):
    """
    Handler wywoływany przez DRF dla wszystkich wyjątków w views.
    Zwraca response w spójnym formacie KLIK:

    {
        "error": {
            "code": "...",
            "message": "...",
            "timestamp": "..."
        }
    }
    """
    # Konwersja Django ValidationError → DRF ValidationError przed obsługą
    if isinstance(exc, DjangoValidationError):
        return _handle_django_validation_error(exc)

    # Nasze wyjątki KlikAPIException
    if isinstance(exc, KlikAPIException):
        return _build_response(exc.code, str(exc.detail), exc.status_code)

    # Wyjątki autentykacji DRF
    if isinstance(exc, NotAuthenticated | AuthenticationFailed):
        return _build_response(
            '401_UNAUTHORIZED',
            str(exc.detail) if hasattr(exc, 'detail') else 'Brak uwierzytelnienia.',
            status.HTTP_401_UNAUTHORIZED,
        )

    # PermissionDenied DRF
    if isinstance(exc, DRFPermissionDenied):
        return _build_response(
            '403_INSUFFICIENT_PERMISSIONS',
            str(exc.detail) if hasattr(exc, 'detail') else 'Brak uprawnień.',
            status.HTTP_403_FORBIDDEN,
        )

    # 404 z Http404 lub NotFound
    if isinstance(exc, Http404 | NotFound):
        return _build_response(
            '404_NOT_FOUND',
            'Zasób nie istnieje.',
            status.HTTP_404_NOT_FOUND,
        )

    # Standardowy DRF handler — łapie ValidationError i inne
    response = exception_handler(exc, context)
    if response is not None:
        # Przepakuj na nasz format
        message = _extract_message(response.data)
        code = _http_status_to_code(response.status_code)
        return _build_response(code, message, response.status_code)

    # Niespodziewany wyjątek — log i 500
    logger.exception('Unhandled exception in API: %s', exc)
    return _build_response(
        '500_INTERNAL_ERROR',
        'Nieoczekiwany błąd serwera.',
        status.HTTP_500_INTERNAL_SERVER_ERROR,
    )


def _build_response(code: str, message: str, http_status: int) -> Response:
    """Buduje response w formacie KLIK."""
    return Response(
        {
            'error': {
                'code': code,
                'message': message,
                'timestamp': datetime.now(UTC).isoformat(),
            }
        },
        status=http_status,
    )


def _handle_django_validation_error(exc: DjangoValidationError) -> Response:
    """Mapowanie Django ValidationError na format KLIK."""
    if hasattr(exc, 'message_dict'):
        # Walidacja per-pole
        messages = []
        for field, errors in exc.message_dict.items():
            if isinstance(errors, list):
                for err in errors:
                    messages.append(f'{field}: {err}')
            else:
                messages.append(f'{field}: {errors}')
        message = '; '.join(messages)
    elif hasattr(exc, 'messages'):
        message = '; '.join(exc.messages)
    else:
        message = str(exc)

    return _build_response(
        '400_BAD_REQUEST',
        message,
        status.HTTP_400_BAD_REQUEST,
    )


def _extract_message(data) -> str:
    """Wyciąga ludzką wiadomość z DRF response data."""
    if isinstance(data, dict):
        if 'detail' in data:
            return str(data['detail'])
        # Walidacja serializerem — łapie {'field': ['error1', 'error2']}
        messages = []
        for field, errors in data.items():
            if isinstance(errors, list):
                for err in errors:
                    messages.append(f'{field}: {err}')
            else:
                messages.append(f'{field}: {errors}')
        return '; '.join(messages) if messages else 'Niepoprawne żądanie.'
    if isinstance(data, list):
        return '; '.join(str(x) for x in data)
    return str(data)


def _http_status_to_code(http_status: int) -> str:
    """Maps HTTP status na default code."""
    mapping = {
        400: '400_BAD_REQUEST',
        401: '401_UNAUTHORIZED',
        403: '403_INSUFFICIENT_PERMISSIONS',
        404: '404_NOT_FOUND',
        409: '409_CONFLICT',
        410: '410_GONE',
        422: '422_UNPROCESSABLE_ENTITY',
        500: '500_INTERNAL_ERROR',
        503: '503_SERVICE_UNAVAILABLE',
    }
    return mapping.get(http_status, f'{http_status}_ERROR')
