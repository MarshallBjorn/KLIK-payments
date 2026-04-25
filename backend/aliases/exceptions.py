"""
Custom DRF exceptions dla apki aliases.

Mapowanie na error codes z `docs/c2b/integration/INFO.md`:

    AliasAlreadyExists       → 409 / 409_ALIAS_ALREADY_EXISTS
    AliasNotFound            → 404 / 404_ALIAS_NOT_FOUND
    ZoneMismatch             → 422 / 422_ZONE_MISMATCH
    InsufficientPermissions  → 403 / 403_INSUFFICIENT_PERMISSIONS

Wszystkie dziedziczą z `rest_framework.exceptions.APIException`, więc DRF
automatycznie zwróci je z odpowiednim status code i ciałem JSON. Format ciała
ujednolicimy w widokach (zgodnie z konwencją `{"error": {"code": ..., "message": ...}}`
opisaną w INFO.md).
"""

from rest_framework import status
from rest_framework.exceptions import APIException


class AliasAlreadyExists(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_detail = 'Numer telefonu już zarejestrowany w KLIK.'
    default_code = '409_ALIAS_ALREADY_EXISTS'


class AliasNotFound(APIException):
    status_code = status.HTTP_404_NOT_FOUND
    default_detail = 'Alias dla podanego numeru nie istnieje.'
    default_code = '404_ALIAS_NOT_FOUND'


class ZoneMismatch(APIException):
    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
    default_detail = 'Strefa nie zgadza się z prefiksem telefonu lub strefą banku.'
    default_code = '422_ZONE_MISMATCH'


class InsufficientPermissions(APIException):
    status_code = status.HTTP_403_FORBIDDEN
    default_detail = 'Bank nie ma uprawnień do operacji na tym aliasie.'
    default_code = '403_INSUFFICIENT_PERMISSIONS'
