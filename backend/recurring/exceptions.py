"""Wyjątki domenowe dla modułu recurring.

Kody zgodne z tabelą błędów w docs/reccuring/integration/INFO.md.
Wspólne kody (401, 403_BANK_INACTIVE, 403_P2P_NOT_ENABLED,
403_INSUFFICIENT_PERMISSIONS, 409_IDEMPOTENCY_CONFLICT, 422_*) siedzą
w common.exceptions.
"""

from rest_framework import status

from common.exceptions import KlikAPIException


class InvalidCycleError(KlikAPIException):
    status_code = status.HTTP_400_BAD_REQUEST
    default_code = '400_INVALID_CYCLE'
    default_detail = 'Pole cycle musi być jednym z: DAILY, WEEKLY, MONTHLY.'


class InvalidDateRangeError(KlikAPIException):
    status_code = status.HTTP_400_BAD_REQUEST
    default_code = '400_INVALID_DATE_RANGE'
    default_detail = 'Niepoprawny zakres dat zlecenia.'


class InvalidPhoneFormatError(KlikAPIException):
    status_code = status.HTTP_400_BAD_REQUEST
    default_code = '400_INVALID_PHONE_FORMAT'
    default_detail = 'recipient_phone musi być w formacie E.164 (np. +48501234567).'


class RecurringNotEnabledError(KlikAPIException):
    status_code = status.HTTP_403_FORBIDDEN
    default_code = '403_RECURRING_NOT_ENABLED'
    default_detail = 'Bank nie ma aktywowanego modułu Recurring.'


class RecurringNotFoundError(KlikAPIException):
    status_code = status.HTTP_404_NOT_FOUND
    default_code = '404_RECURRING_NOT_FOUND'
    default_detail = 'Zlecenie stałe nie istnieje.'


class RecipientAliasNotFoundError(KlikAPIException):
    status_code = status.HTTP_404_NOT_FOUND
    default_code = '404_RECIPIENT_ALIAS_NOT_FOUND'
    default_detail = 'Numer odbiorcy nie jest zarejestrowany w KLIK.'


class RecurringNotActiveError(KlikAPIException):
    status_code = status.HTTP_409_CONFLICT
    default_code = '409_RECURRING_NOT_ACTIVE'
    default_detail = 'Operacja możliwa tylko dla zlecenia w stanie ACTIVE.'


class RecurringNotPausedError(KlikAPIException):
    status_code = status.HTTP_409_CONFLICT
    default_code = '409_RECURRING_NOT_PAUSED'
    default_detail = 'Operacja resume możliwa tylko dla zlecenia w stanie PAUSED.'


class RecurringTerminatedError(KlikAPIException):
    status_code = status.HTTP_409_CONFLICT
    default_code = '409_RECURRING_TERMINATED'
    default_detail = 'Zlecenie jest w stanie terminalnym (CANCELLED/COMPLETED).'
