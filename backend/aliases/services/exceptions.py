"""
Service-level exceptions dla AliasService.

Te wyjątki są PYTHON, nie DRF — service nie wie nic o HTTP. Widoki łapią
i mapują na DRF APIException-y z `aliases/exceptions.py`:

    AliasAlreadyRegisteredError  → AliasAlreadyExists  (409)
    AliasDoesNotExistError       → AliasNotFound       (404)
    AliasZoneMismatchError       → ZoneMismatch        (422)
    AliasOwnershipError          → InsufficientPermissions (403)

Symetria z `codes/services/exceptions.py` — ta sama konwencja w całym repo.
"""


class AliasAlreadyRegisteredError(Exception):
    """Próba rejestracji aliasu na phone, który już istnieje (globalnie)."""

    def __init__(self, phone: str):
        self.phone = phone
        super().__init__(f'Numer {phone} jest już zarejestrowany w KLIK.')


class AliasDoesNotExistError(Exception):
    """Phone nie istnieje w rejestrze aliasów."""

    def __init__(self, phone: str):
        self.phone = phone
        super().__init__(f'Brak aliasu dla numeru {phone}.')


class AliasZoneMismatchError(Exception):
    """Strefa aliasu niezgodna: prefiks ≠ zone, lub zone aliasu ≠ zone banku.

    `reason` zawiera ludzki komunikat z modelowej walidacji (`Alias.clean()`).
    """

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class AliasOwnershipError(Exception):
    """Bank próbuje operować na aliasie należącym do innego banku.

    Używane w `unregister` — bank może usuwać tylko aliasy SWOICH klientów.
    """

    def __init__(self, phone: str, bank_id):
        self.phone = phone
        self.bank_id = bank_id
        super().__init__(f'Bank {bank_id} nie jest właścicielem aliasu dla {phone}.')
