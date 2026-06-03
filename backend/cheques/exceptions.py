"""Wyjątki domenowe dla modułu cheques."""

from rest_framework import status

from common.exceptions import KlikAPIException


class ChequesNotEnabledError(KlikAPIException):
    status_code = status.HTTP_403_FORBIDDEN
    default_code = '403_CHEQUES_NOT_ENABLED'
    default_detail = 'Bank nie ma aktywowanego modułu Cheques.'


class ChequeNotFoundError(KlikAPIException):
    status_code = status.HTTP_404_NOT_FOUND
    default_code = '404_CHEQUE_NOT_FOUND'
    default_detail = 'Czek nie istnieje.'


class ChequeExpiredError(KlikAPIException):
    status_code = status.HTTP_404_NOT_FOUND
    default_code = '404_CHEQUE_EXPIRED'
    default_detail = 'Czek wygasł lub nie istnieje.'


class ChequeAlreadyRedeemedError(KlikAPIException):
    status_code = status.HTTP_409_CONFLICT
    default_code = '409_CHEQUE_ALREADY_REDEEMED'
    default_detail = 'Czek został już zrealizowany.'


class ChequeAlreadyCancelledError(KlikAPIException):
    status_code = status.HTTP_409_CONFLICT
    default_code = '409_CHEQUE_ALREADY_CANCELLED'
    default_detail = 'Czek został już anulowany.'


class ChequeNotActiveError(KlikAPIException):
    status_code = status.HTTP_409_CONFLICT
    default_code = '409_CHEQUE_NOT_ACTIVE'
    default_detail = 'Operacja możliwa tylko dla czeku w stanie ACTIVE.'
