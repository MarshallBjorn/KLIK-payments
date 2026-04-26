"""Wspólne fixturki dla testów apki banks."""

import pytest

from banks.models import Bank


@pytest.fixture
def make_bank(db):
    """Factory fixture — tworzy bank z ustawionym kluczem API.

    Zwraca tuple (Bank, plaintext_api_key) — plaintext potrzebny w testach
    auth, gdzie symulujemy nagłówek `X-KLIK-Api-Key`.
    """

    def _make(
        name: str = "PKO BP",
        zone: str = "PL",
        currency: str = "PLN",
        active: bool = True,
        debt_limit: str = "100000.00",
        webhook_url: str = "https://bank.example.com/webhook",
    ) -> tuple[Bank, str]:
        bank = Bank(
            name=name,
            zone=zone,
            currency=currency,
            active=active,
            debt_limit=debt_limit,
            webhook_url=webhook_url,
        )
        plaintext = bank.rotate_api_key()
        bank.save()
        return bank, plaintext

    return _make
