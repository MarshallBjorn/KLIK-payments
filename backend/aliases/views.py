"""
Widoki DRF dla apki aliases.

Trzy endpointy zgodnie z docs/c2b/integration/INFO.md (sekcja "Moduł Telefony"):

    POST   /aliases/register         alias_register
    GET    /aliases/lookup/<phone>   alias_lookup
    DELETE /aliases/<phone>          alias_delete

Wszystkie wymagają:
- uwierzytelnienia banku (`X-KLIK-Api-Key` → `XKlikBankApiKeyAuthentication`),
- włączonego modułu P2P (`Bank.p2p_enabled=True` → `BankHasP2PEnabled`).

Logika domenowa siedzi w `AliasService`. Widoki:
1. walidują payload przez serializer,
2. delegują operację do service'u,
3. mapują wyjątki service-level (czyste Python) na DRF APIException-y.

Format ciała błędu ujednolicony w `common.exceptions.klik_exception_handler`
(zarejestrowany globalnie w `core/settings/base.py`):

    {"error": {"code": "...", "message": "...", "timestamp": "..."}}
"""

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
from aliases.serializers import (
    AliasLookupResponseSerializer,
    AliasRegisterResponseSerializer,
    AliasRegisterSerializer,
)
from aliases.services import AliasService
from aliases.services.exceptions import (
    AliasAlreadyRegisteredError,
    AliasDoesNotExistError,
    AliasOwnershipError,
    AliasZoneMismatchError,
)
from banks.authentication import XKlikBankApiKeyAuthentication
from banks.permissions import BankHasP2PEnabled

# ---------------------------------------------------------------------------
# POST /aliases/register
# ---------------------------------------------------------------------------


@api_view(["POST"])
@authentication_classes([XKlikBankApiKeyAuthentication])
@permission_classes([IsAuthenticated, BankHasP2PEnabled])
def alias_register(request):
    """Rejestracja aliasu telefon → konto bankowe.

    Request body:
        {"phone": "+48501234567", "iban": "PL61...", "zone": "PL"}

    Response 201:
        {"alias_id": "...", "phone": "+48501234567", "registered_at": "..."}

    Errors:
        400_BAD_REQUEST              — malformed payload
        401_UNAUTHORIZED             — brak/zły klucz API
        403_BANK_INACTIVE            — bank.active=False
        403_P2P_NOT_ENABLED          — bank.p2p_enabled=False
        409_ALIAS_ALREADY_EXISTS     — phone już zarejestrowany
        422_ZONE_MISMATCH            — prefiks ≠ zone lub zone ≠ bank.zone
    """
    serializer = AliasRegisterSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data

    service = AliasService()
    try:
        alias = service.register(
            bank=request.user,
            phone=data["phone"],
            account_identifier=data["account_identifier"],
            zone=data["zone"],
        )
    except AliasZoneMismatchError as exc:
        raise ZoneMismatch(detail=exc.reason) from exc
    except AliasAlreadyRegisteredError as exc:
        raise AliasAlreadyExists() from exc

    response = AliasRegisterResponseSerializer(alias)
    return Response(response.data, status=status.HTTP_201_CREATED)


# ---------------------------------------------------------------------------
# GET /aliases/lookup/<phone>
# ---------------------------------------------------------------------------


@api_view(["GET"])
@authentication_classes([XKlikBankApiKeyAuthentication])
@permission_classes([IsAuthenticated, BankHasP2PEnabled])
def alias_lookup(request, phone: str):
    """Wyszukanie banku/IBAN po numerze telefonu.

    Wywołuje go bank nadawcy P2P żeby wiedzieć dokąd wysłać przelew (przez
    Elixir / FedNow RTP / SEPA Instant — POZA KLIK).

    Counter w Redisie inkrementowany TYLKO przy 200 (patrz AliasService).

    Response 200:
        {"phone": "...", "bank_id": "...", "bank_code": "...", "iban": "..."}

    Errors:
        401_UNAUTHORIZED, 403_BANK_INACTIVE, 403_P2P_NOT_ENABLED
        404_ALIAS_NOT_FOUND
    """
    service = AliasService()
    try:
        alias = service.lookup_for_bank(querying_bank=request.user, phone=phone)
    except AliasDoesNotExistError as exc:
        raise AliasNotFound() from exc

    response = AliasLookupResponseSerializer(alias)
    return Response(response.data, status=status.HTTP_200_OK)


# ---------------------------------------------------------------------------
# DELETE /aliases/<phone>
# ---------------------------------------------------------------------------


@api_view(["DELETE"])
@authentication_classes([XKlikBankApiKeyAuthentication])
@permission_classes([IsAuthenticated, BankHasP2PEnabled])
def alias_delete(request, phone: str):
    """Usunięcie aliasu (np. klient wyłącza funkcję lub zamyka konto).

    Bank może usuwać tylko aliasy SWOICH klientów. Próba usunięcia cudzego
    aliasu → 403_INSUFFICIENT_PERMISSIONS.

    Response 204: brak ciała.

    Errors:
        401_UNAUTHORIZED, 403_BANK_INACTIVE, 403_P2P_NOT_ENABLED
        403_INSUFFICIENT_PERMISSIONS
        404_ALIAS_NOT_FOUND
    """
    service = AliasService()
    try:
        service.unregister(bank=request.user, phone=phone)
    except AliasDoesNotExistError as exc:
        raise AliasNotFound() from exc
    except AliasOwnershipError as exc:
        raise InsufficientPermissions() from exc

    return Response(status=status.HTTP_204_NO_CONTENT)
