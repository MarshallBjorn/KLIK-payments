"""
Custom DRF authentication class — `XKlikApiKeyAuthentication`.

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
"""

from rest_framework import exceptions
from rest_framework.authentication import BaseAuthentication

from banks.models import Bank, hash_api_key

API_KEY_HEADER = 'HTTP_X_KLIK_API_KEY'  # Django wystawia nagłówki jako HTTP_*


class BankInactive(exceptions.AuthenticationFailed):
    """Bank istnieje ale ma `active=False`.

    Dziedziczy po AuthenticationFailed (nie PermissionDenied), żeby DRF
    nadal próbował innych klas auth jeśli są skonfigurowane. Status code
    ustawiamy ręcznie na 403, zgodnie z error code `403_BANK_INACTIVE`.
    """

    status_code = 403
    default_detail = 'Bank zablokowany (active=False).'
    default_code = '403_BANK_INACTIVE'


class XKlikApiKeyAuthentication(BaseAuthentication):
    """Uwierzytelnianie banku przez nagłówek `X-KLIK-Api-Key`.

    Zwraca instancję `Bank` w `request.user`, więc widoki mogą np. robić
    `request.user.zone` żeby sprawdzić strefę nadawcy.
    """

    keyword = 'X-KLIK-Api-Key'

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