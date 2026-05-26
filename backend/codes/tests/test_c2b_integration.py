"""Testy integracyjne C2B — pełny łańcuch przez realne endpointy.

W odróżnieniu od test_views.py / test_confirm_view.py (które mockują
`authorize_webhook_task.delay` i tworzą Transaction ręcznie), tutaj
przechodzimy CAŁY przepływ KLIK Kody przez HTTP API:

    POST /codes/generate            (bank)
        → POST /payments/initiate   (agent)  → zadanie Celery odpala się
                                                synchronicznie (eager) i woła
                                                webhook banku
        → POST /payments/confirm    (bank)
        → GET  /payments/status     (bank/agent)

Granica integracji (Poziom 1): realny Postgres + Redis + Celery eager,
a JEDYNYM mockiem jest wychodzące HTTP do banku (`requests.post`
w codes.tasks). Dzięki temu pokrywamy budowę payloadu webhooka, retry/timeout
oraz spójność ledgera — czego testy view-level nie dotykają.
"""

from decimal import Decimal

import pytest
from django.utils import timezone
from django_redis import get_redis_connection
from rest_framework.test import APIClient

from agents.authentication import generate_api_key as generate_agent_key
from agents.models import Agent, MSCAgreement
from banks.models import Bank
from banks.models import generate_api_key as generate_bank_key
from common.enums import Zone
from merchants.models import Merchant

PL_IBAN = {'type': 'iban', 'value': 'PL61109010140000071219812874'}
PL_IBAN_2 = {'type': 'iban', 'value': 'PL27114020040000300201355387'}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def celery_eager(settings):
    """Celery synchronicznie — task.delay() wykonuje się w wątku testu."""
    settings.CELERY_TASK_ALWAYS_EAGER = True
    settings.CELERY_TASK_EAGER_PROPAGATES = True
    yield


@pytest.fixture(autouse=True)
def flush_redis():
    redis = get_redis_connection('default')
    redis.flushdb()
    yield
    redis.flushdb()


@pytest.fixture
def client():
    return APIClient()


@pytest.fixture
def sender_bank(db):
    """Bank nadawcy (klienta) z plaintext kluczem API."""
    plaintext, hash_ = generate_bank_key()
    bank = Bank.objects.create(
        name='Sender Bank',
        api_key_hash=hash_,
        zone=Zone.PL,
        currency='PLN',
        debt_limit=Decimal('1000000.00'),
        webhook_url='https://sender-bank.example.com/webhook',
        c2b_enabled=True,
        active=True,
    )
    return bank, plaintext


@pytest.fixture
def acquirer_bank(db):
    """Drugi bank — bank rozliczeniowy merchanta dla scenariuszy off-us."""
    return Bank.objects.create(
        name='Acquirer Bank',
        api_key_hash='acquirer_hash',  # pragma: allowlist secret
        zone=Zone.PL,
        currency='PLN',
        debt_limit=Decimal('1000000.00'),
        webhook_url='https://acquirer-bank.example.com/webhook',
        c2b_enabled=True,
        active=True,
    )


@pytest.fixture
def agent(db, sender_bank):
    bank, _ = sender_bank
    plaintext, hash_ = generate_agent_key()
    agent_obj = Agent.objects.create(
        name='Agent Sklep',
        api_key_hash=hash_,
        settlement_bank=bank,
        account_identifier=PL_IBAN,
        zone=Zone.PL,
    )
    return agent_obj, plaintext


@pytest.fixture
def msc(db, agent):
    agent_obj, _ = agent
    return MSCAgreement.objects.create(
        agent=agent_obj,
        klik_fee_perc=Decimal('0.30'),
        agent_fee_perc=Decimal('1.00'),
        valid_from=timezone.now() - timezone.timedelta(days=1),
    )


@pytest.fixture
def merchant_off_us(db, acquirer_bank):
    return Merchant.objects.create(
        name='Sklep Off-Us',
        settlement_bank=acquirer_bank,
        account_identifier=PL_IBAN_2,
        zone=Zone.PL,
    )


@pytest.fixture
def merchant_on_us(db, sender_bank):
    bank, _ = sender_bank
    return Merchant.objects.create(
        name='Sklep On-Us',
        settlement_bank=bank,
        account_identifier=PL_IBAN,
        zone=Zone.PL,
    )


# ---------------------------------------------------------------------------
# Helpers — kroki przepływu C2B
# ---------------------------------------------------------------------------
