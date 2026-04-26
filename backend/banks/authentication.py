"""
Custom DRF authentication class — `XKlikBankApiKeyAuthentication`.

Banki uwierzytelniają się statycznym kluczem API w nagłówku `X-KLIK-Api-Key`
(MVP — patrz INFO.md, sekcja "Autentykacja").

Flow:
1. Wyciągamy nagłówek z requesta.
2. Hashujemy plaintext (SHA-256) i wyszukujemy w `Bank.api_key_hash`.
3. Jeśli bank znaleziony i `active=True` → zwracamy `(bank, None)`,
   bank trafia do `request.user`.
4. Jeśli bank znaleziony ale nieaktywny → 403_BANK_INACTIVE.
5. Jeśli nie znaleziony → 401_UNAUTHORIZED.

Zwracamy obiekt Bank zamiast User-a. To celowe — w MVP banki nie mają
encji User w sensie auth.User, są to byty domenowe. Permission classes
mogą czytać `request.user` jako Bank.

Naming: klasa to `XKlikBankApiKeyAuthentication` (symetria z
`XKlikAgentApiKeyAuthentication` w apce agents). Stara nazwa
`XKlikApiKeyAuthentication` jest backward-compatible aliasem z
DeprecationWarning — do usunięcia po migracji wszystkich call sites.
"""

import warnings

from rest_framework import exceptions
from rest_framework.authentication import BaseAuthentication

from banks.models import Bank, hash_api_key

API_KEY_HEADER = (
    'HTTP_X_KLIK_BANK_API_KEY'  # Django wystawia nagłówki jako HTTP_* pragma: allowlist secret
)


class BankInactive(exceptions.AuthenticationFailed):
    """Bank istnieje ale ma `active=False`.

    Dziedziczy po AuthenticationFailed (nie PermissionDenied), żeby DRF
    nadal próbował innych klas auth jeśli są skonfigurowane. Status code
    ustawiamy ręcznie na 403, zgodnie z error code `403_BANK_INACTIVE`.
    """

    status_code = 403
    default_detail = 'Bank zablokowany (active=False).'
    default_code = '403_BANK_INACTIVE'


class XKlikBankApiKeyAuthentication(BaseAuthentication):
    """Uwierzytelnianie banku przez nagłówek `X-KLIK-Bank-Api-Key`.

    Zwraca instancję `Bank` w `request.user`, więc widoki mogą np. robić
    `request.user.zone` żeby sprawdzić strefę nadawcy.
    """

    keyword = 'X-KLIK-Bank-Api-Key'

    def authenticate(self, request):
        plaintext = request.META.get(API_KEY_HEADER)
        if not plaintext:
            # Brak nagłówka = "nie próbowałeś się uwierzytelnić".
            # Zwracamy None żeby DRF wpadł w permission_denied dopiero gdy
            # widok faktycznie wymaga auth.
            return None

        try:
            bank = Bank.objects.get(api_key_hash=hash_api_key(plaintext))
        except Bank.DoesNotExist as exc:
            raise exceptions.AuthenticationFailed(
                detail='Nieprawidłowy klucz API.',
                code='401_UNAUTHORIZED',
            ) from exc

        if not bank.active:
            raise BankInactive()

        return (bank, None)

    def authenticate_header(self, request):
        """Zwracane w nagłówku `WWW-Authenticate` przy 401.

        DRF wymaga implementacji jeśli auth class ma generować 401.
        """
        return self.keyword


# ---------------------------------------------------------------------------
# Backward-compatible alias — DEPRECATED
# ---------------------------------------------------------------------------
# Stara nazwa (przed wydzieleniem P2P i symetrii z XKlikAgentApiKey...).
# Ostrzeżenie pokazujemy przy *imporcie* przez stary alias, nie przy każdym
# wywołaniu — żeby nie zaśmiecać logów. Do usunięcia po przepisaniu wszystkich
# call sites (grep `XKlikApiKeyAuthentication`).


def __getattr__(name: str):
    if name == 'XKlikApiKeyAuthentication':
        warnings.warn(
            'XKlikApiKeyAuthentication zostało zmienione na '
            'XKlikBankApiKeyAuthentication. Stary alias zostanie usunięty.',
            DeprecationWarning,
            stacklevel=2,
        )
        return XKlikBankApiKeyAuthentication
    raise AttributeError(f'module {__name__!r} has no attribute {name!r}')
