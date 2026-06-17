"""
Wspólne fixturki dla testów apki recurring.

Konwencja jak w aliases/tests/conftest.py — factory `make_recurring_bank`
zwraca tuple (Bank, plaintext_api_key). Recurring wymaga p2p_enabled=True
(lookup aliasu jest mechanizmem P2P), więc factory ustawia obie flagi.
"""

import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone
from django_redis import get_redis_connection
from rest_framework.test import APIClient

from aliases.models import Alias
from banks.models import Bank
from common.enums import Zone
from recurring.models import RecurringCycle, RecurringTransfer

PL_IBAN = {'type': 'iban', 'value': 'PL61109010140000071219812874'}
RECIPIENT_PHONE = '+48501234567'


@pytest.fixture(autouse=True)
def flush_redis():
    """Czyszczenie Redisa przed i po każdym teście.

    Counter lookupów (naliczanie per execution) oraz cache idempotency
    żyją między testami — bez flusha testy są od siebie zależne.
    """
    redis = get_redis_connection('default')
    redis.flushdb()
    yield
    redis.flushdb()


def _make_bank(
    *,
    name: str,
    zone: str,
    currency: str,
    active: bool = True,
    p2p_enabled: bool = True,
    recurring_enabled: bool = True,
    webhook_url: str = 'https://bank.example.com/webhook',
    recurring_webhook_url: str = '',
) -> tuple[Bank, str]:
    bank = Bank(
        name=name,
        zone=zone,
        currency=currency,
        active=active,
        debt_limit=Decimal('100000.00'),
        webhook_url=webhook_url,
        p2p_enabled=p2p_enabled,
        recurring_enabled=recurring_enabled,
        recurring_webhook_url=recurring_webhook_url,
    )
    plaintext = bank.rotate_api_key()
    bank.save()
    return bank, plaintext


@pytest.fixture
def make_recurring_bank(db):
    """Factory — bank z włączonym Recurring+P2P. Zwraca (Bank, plaintext_key)."""
    counter = {'i': 0}

    def _make(**kwargs) -> tuple[Bank, str]:
        counter['i'] += 1
        kwargs.setdefault('name', f'Recurring Bank #{counter["i"]}')
        kwargs.setdefault('zone', Zone.PL)
        kwargs.setdefault('currency', 'PLN')
        return _make_bank(**kwargs)

    return _make


@pytest.fixture
def sender_bank(make_recurring_bank):
    """Aktywny bank PL z recurring_enabled+p2p_enabled. (Bank, plaintext_key)."""
    return make_recurring_bank(name='Bank Nadawca')


@pytest.fixture
def recipient_bank(make_recurring_bank):
    """Bank odbiorcy aliasu (sam nie musi mieć recurring)."""
    return make_recurring_bank(name='Bank Odbiorca', recurring_enabled=False)


@pytest.fixture
def recipient_alias(recipient_bank):
    """Alias odbiorcy zarejestrowany w KLIK (strefa PL)."""
    bank, _ = recipient_bank
    return Alias.objects.create(
        phone=RECIPIENT_PHONE,
        bank=bank,
        account_identifier=PL_IBAN,
        zone=Zone.PL,
    )


@pytest.fixture
def api_client():
    return APIClient()


@pytest.fixture
def auth_client(api_client, sender_bank):
    """APIClient z nagłówkiem auth banku nadawcy."""
    _, plaintext = sender_bank
    api_client.credentials(HTTP_X_KLIK_BANK_API_KEY=plaintext)
    return api_client


@pytest.fixture
def create_payload():
    """Poprawny payload POST /recurring/create (factory z override-ami)."""

    def _make(**over):
        today = timezone.now().date()
        payload = {
            'payer_user_id': 'client-1',
            'recipient_phone': RECIPIENT_PHONE,
            'amount': '50.00',
            'currency': 'PLN',
            'zone': 'PL',
            'cycle': 'MONTHLY',
            'start_date': str(today),
            'end_date': str(today + timedelta(days=365)),
            'mandate_signed_at': '2026-05-03T14:00:00Z',
        }
        payload.update(over)
        return payload

    return _make


@pytest.fixture
def make_mandate(sender_bank):
    """Factory mandate-a wprost w DB (z pominięciem API) — do testów tasków."""

    def _make(**over):
        bank, _ = sender_bank
        today = timezone.now().date()
        defaults = {
            'payer_bank': bank,
            'payer_user_id': 'client-1',
            'recipient_phone': RECIPIENT_PHONE,
            'amount': Decimal('50.00'),
            'currency': 'PLN',
            'zone': Zone.PL,
            'cycle': RecurringCycle.MONTHLY,
            'start_date': today,
            'end_date': None,
            'next_run_at': timezone.now() - timedelta(minutes=1),
            'mandate_signed_at': timezone.now(),
            'idempotency_key': str(uuid.uuid4()),
        }
        defaults.update(over)
        return RecurringTransfer.objects.create(**defaults)

    return _make


def post_idem(client, url, payload=None, key=None):
    """POST z nagłówkiem Idempotency-Key (wymagany dla operacji mutujących)."""
    return client.post(
        url,
        payload or {},
        format='json',
        headers={'Idempotency-Key': key or str(uuid.uuid4())},
    )
