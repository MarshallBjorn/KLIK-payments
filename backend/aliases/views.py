# Create your views here.
"""
Widoki DRF dla apki aliases.

Trzy endpointy zgodnie z docs/c2b/integration/INFO.md (sekcja "Moduł Telefony"):

    POST   /aliases/register         alias_register
    GET    /aliases/lookup/<phone>   alias_lookup
    DELETE /aliases/<phone>          alias_delete

Wszystkie wymagają uwierzytelnienia banku (`X-KLIK-Api-Key`). `request.user`
to instancja `Bank` (patrz banks.authentication.XKlikApiKeyAuthentication).

Konwencje błędów (patrz docs):
    409_ALIAS_ALREADY_EXISTS   — duplikat phone
    404_ALIAS_NOT_FOUND        — phone nie istnieje
    422_ZONE_MISMATCH          — prefiks ≠ zone, lub zone aliasu ≠ zone banku
    403_INSUFFICIENT_PERMISSIONS — bank próbuje usunąć alias innego banku

Format ciała błędu jednolity (patrz INFO.md → "Error codes reference"):
    {"error": {"code": "...", "message": "...", "timestamp": "..."}}
"""

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import (
    api_view,
    authentication_classes,
    permission_classes,
)
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from aliases.exceptions import (
    AliasAlreadyExists,
    AliasNotFound,
    InsufficientPermissions,
    ZoneMismatch,
)
from aliases.models import Alias
from aliases.serializers import (
    AliasLookupResponseSerializer,
    AliasRegisterResponseSerializer,
    AliasRegisterSerializer,
)
from banks.authentication import XKlikApiKeyAuthentication


def _error_body(code: str, message: str) -> dict:
    """Format ciała błędu zgodny z INFO.md."""
    return {
        'error': {
            'code': code,
            'message': message,
            'timestamp': timezone.now().isoformat(),
        }
    }


def _is_unique_phone_violation(exc: IntegrityError) -> bool:
    """Heurystyka: czy IntegrityError dotyczy unique constraint na phone.

    Patrzymy po nazwach constraintów żeby nie złapać przypadkiem innych
    (np. CheckConstraint na zone). Postgres wkleja nazwę constraintu w args[0].
    """
    msg = str(exc).lower()
    return 'alias_phone_unique' in msg or 'phone' in msg


def _zone_mismatch_from_validation(exc: DjangoValidationError) -> bool:
    """True jeśli ValidationError dotyczy spójności strefa/telefon."""
    error_dict = getattr(exc, 'message_dict', {})
    return 'zone' in error_dict


# ---------------------------------------------------------------------------
# POST /aliases/register
# ---------------------------------------------------------------------------


@api_view(['POST'])
@authentication_classes([XKlikApiKeyAuthentication])
@permission_classes([IsAuthenticated])
def alias_register(request):
    """Rejestracja aliasu telefon → konto bankowe.

    Request body:
        {"phone": "+48501234567", "iban": "PL61...", "zone": "PL"}

    Response 201:
        {"alias_id": "...", "phone": "+48501234567", "registered_at": "..."}

    Errors:
        400_BAD_REQUEST           — malformed payload
        409_ALIAS_ALREADY_EXISTS  — phone zarejestrowany
        422_ZONE_MISMATCH         — prefiks ≠ zone lub zone ≠ bank.zone
    """
    serializer = AliasRegisterSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)

    bank = request.user  # Bank z XKlikApiKeyAuthentication
    data = serializer.validated_data

    # Konstruujemy obiekt i wołamy save() — full_clean() w środku rzuci
    # ValidationError jeśli prefiks ≠ zone lub zone ≠ bank.zone.
    alias = Alias(
        phone=data['phone'],
        bank=bank,
        account_identifier=data['account_identifier'],
        zone=data['zone'],
    )

    try:
        with transaction.atomic():
            alias.save()
    except DjangoValidationError as exc:
        # Rozróżniamy zone mismatch od innych błędów walidacji żeby zwrócić
        # specyficzny kod błędu z dokumentacji.
        if _zone_mismatch_from_validation(exc):
            raise ZoneMismatch(detail=' '.join(exc.message_dict['zone'])) from exc
        # Inne błędy walidacji — domyślny 400 z DRF (przez ValidationError DRF).
        from rest_framework.exceptions import ValidationError as DRFValidationError

        raise DRFValidationError(detail=exc.message_dict) from exc
    except IntegrityError as exc:
        # Race condition: dwa requesty z tym samym phone w tej samej chwili.
        # Pierwszy wygrywa, drugi dostaje IntegrityError → mapujemy na 409.
        if _is_unique_phone_violation(exc):
            raise AliasAlreadyExists() from exc
        raise

    response = AliasRegisterResponseSerializer(alias)
    return Response(response.data, status=status.HTTP_201_CREATED)


# ---------------------------------------------------------------------------
# GET /aliases/lookup/<phone>
# ---------------------------------------------------------------------------


@api_view(['GET'])
@authentication_classes([XKlikApiKeyAuthentication])
@permission_classes([IsAuthenticated])
def alias_lookup(request, phone: str):
    """Wyszukanie banku/IBAN po numerze telefonu.

    Wywołuje go bank nadawcy P2P żeby wiedzieć dokąd wysłać przelew (przez
    Elixir / FedNow RTP / SEPA Instant — POZA KLIK).

    Response 200:
        {"phone": "...", "bank_id": "...", "bank_code": "...", "iban": "..."}

    Errors:
        404_ALIAS_NOT_FOUND
    """
    try:
        alias = Alias.objects.select_related('bank').get(phone=phone)
    except Alias.DoesNotExist as exc:
        raise AliasNotFound() from exc

    response = AliasLookupResponseSerializer(alias)
    return Response(response.data, status=status.HTTP_200_OK)


# ---------------------------------------------------------------------------
# DELETE /aliases/<phone>
# ---------------------------------------------------------------------------


@api_view(['DELETE'])
@authentication_classes([XKlikApiKeyAuthentication])
@permission_classes([IsAuthenticated])
def alias_delete(request, phone: str):
    """Usunięcie aliasu (np. klient wyłącza funkcję lub zamyka konto).

    Bank może usuwać tylko aliasy SWOICH klientów. Próba usunięcia cudzego
    aliasu → 403_INSUFFICIENT_PERMISSIONS.

    Response 204: brak ciała.

    Errors:
        404_ALIAS_NOT_FOUND
        403_INSUFFICIENT_PERMISSIONS
    """
    try:
        alias = Alias.objects.get(phone=phone)
    except Alias.DoesNotExist as exc:
        raise AliasNotFound() from exc

    if alias.bank_id != request.user.id:
        raise InsufficientPermissions()

    alias.delete()
    return Response(status=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Custom exception handler — ujednolica format ciała błędu
# ---------------------------------------------------------------------------


def aliases_exception_handler(exc, context):
    """DRF exception handler ujednolicający format błędów do {error: {code, message, timestamp}}.

    Zarejestrowany lokalnie dla aliases (patrz aliases/urls.py — opcjonalnie
    można podpiąć globalnie w REST_FRAMEWORK['EXCEPTION_HANDLER']).
    """
    from rest_framework.views import exception_handler

    response = exception_handler(exc, context)
    if response is None:
        return None

    # Bierzemy code z atrybutu, fallback na status.
    code = getattr(exc, 'default_code', None) or str(response.status_code)
    detail = response.data
    if isinstance(detail, dict) and 'detail' in detail:
        message = str(detail['detail'])
    else:
        message = str(detail)

    response.data = _error_body(code, message)
    return response
