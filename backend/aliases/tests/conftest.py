"""
Wspólne fixturki dla testów apki aliases.

Korzystamy z `make_bank` z `banks/tests/conftest.py` — pytest automatycznie
podciąga go z parent conftest, ale `make_bank` nie ustawia P2P. Dlatego
dorzucamy własne fixturki `bank_pl_p2p`, `bank_uk_p2p` które tworzą bank z
flagą `p2p_enabled=True`.
"""

from decimal import Decimal

import pytest
from django_redis import get_redis_connection
from rest_framework.test import APIClient

from banks.models import Bank
from common.enums import Zone

PL_IBAN = {"type": "iban", "value": "PL61109010140000071219812874"}
UK_IBAN = {"type": "iban", "value": "GB82WEST12345698765432"}


@pytest.fixture(autouse=True)
def flush_redis():
    """Czyszczenie Redisa przed i po każdym teście — counter lookupów żyje
    między testami i bez tego seria testów daje fałszywie zawyżone liczniki.

    autouse=True bo każdy test E2E z lookup-em dotyka counter-a, więc lepiej
    domyślnie czyścić niż zapominać per test.
    """
    redis = get_redis_connection("default")
    redis.flushdb()
    yield
    redis.flushdb()


def _make_bank_with_p2p(
    *,
    name: str,
    zone: str,
    currency: str,
    active: bool = True,
    p2p_enabled: bool = True,
    c2b_enabled: bool = True,
) -> tuple[Bank, str]:
    """Helper — tworzy bank z plaintext kluczem do nagłówka."""
    bank = Bank(
        name=name,
        zone=zone,
        currency=currency,
        active=active,
        debt_limit=Decimal("100000.00"),
        webhook_url="https://bank.example.com/webhook",
        c2b_enabled=c2b_enabled,
        p2p_enabled=p2p_enabled,
    )
    plaintext = bank.rotate_api_key()
    bank.save()
    return bank, plaintext


@pytest.fixture
def make_p2p_bank(db):
    """Factory — bank z włączonym P2P, gotowy do auth.

    Zwraca tuple (Bank, plaintext_api_key).
    """
    counter = {"i": 0}

    def _make(
        *,
        name: str | None = None,
        zone: str = Zone.PL,
        currency: str = "PLN",
        active: bool = True,
        p2p_enabled: bool = True,
        c2b_enabled: bool = True,
    ) -> tuple[Bank, str]:
        # Auto-incrementing nazwa żeby unikać kolizji unique=True.
        counter["i"] += 1
        return _make_bank_with_p2p(
            name=name or f'P2P Bank {zone} #{counter["i"]}',
            zone=zone,
            currency=currency,
            active=active,
            p2p_enabled=p2p_enabled,
            c2b_enabled=c2b_enabled,
        )

    return _make


@pytest.fixture
def bank_pl_p2p(make_p2p_bank):
    """Aktywny bank PL z P2P=True. Zwraca tuple (Bank, plaintext_key)."""
    return make_p2p_bank(zone=Zone.PL, currency="PLN")


@pytest.fixture
def bank_uk_p2p(make_p2p_bank):
    """Aktywny bank UK z P2P=True."""
    return make_p2p_bank(zone=Zone.UK, currency="GBP", name="UK P2P Bank")


@pytest.fixture
def api_client():
    return APIClient()


@pytest.fixture
def auth_client(api_client, bank_pl_p2p):
    """APIClient z ustawionym nagłówkiem dla bank_pl_p2p — najczęstszy use case."""
    _, plaintext = bank_pl_p2p
    api_client.credentials(HTTP_X_KLIK_BANK_API_KEY=plaintext)
    return api_client
