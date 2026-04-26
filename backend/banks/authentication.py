"""
Custom DRF authentication class — `XKlikBankApiKeyAuthentication`.

Banki uwierzytelniają się statycznym kluczem API w nagłówku `X-KLIK-Api-Key`
(MVP — patrz INFO.md, sekcja "Autentykacja").
"""

import warnings

from rest_framework.authentication import BaseAuthentication

from banks.models import Bank, hash_api_key
from common.exceptions import BankInactiveError, UnauthorizedError

API_KEY_HEADER = (
    'HTTP_X_KLIK_BANK_API_KEY'  # Django wystawia nagłówki jako HTTP_* pragma: allowlist secret
)


class XKlikBankApiKeyAuthentication(BaseAuthentication):
    """Uwierzytelnianie banku przez nagłówek `X-KLIK-Bank-Api-Key`.

    Zwraca instancję `Bank` w `request.user`, więc widoki mogą np. robić
    `request.user.zone` żeby sprawdzić strefę nadawcy.
    """

    keyword = 'X-KLIK-Bank-Api-Key'

    def authenticate(self, request):
        plaintext = request.META.get(API_KEY_HEADER)
        if not plaintext:
            return None

        try:
            bank = Bank.objects.get(api_key_hash=hash_api_key(plaintext))
        except Bank.DoesNotExist as exc:
            raise UnauthorizedError() from exc

        if not bank.active:
            raise BankInactiveError()

        return (bank, None)

    def authenticate_header(self, request):
        return self.keyword


# ---------------------------------------------------------------------------
# Backward-compatible alias — DEPRECATED
# ---------------------------------------------------------------------------


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
