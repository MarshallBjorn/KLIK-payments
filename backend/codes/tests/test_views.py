"""Testy endpointów codes API."""

from decimal import Decimal
from unittest.mock import patch

import pytest
from django_redis import get_redis_connection
from rest_framework.test import APIClient

from agents.authentication import generate_api_key as generate_agent_key
from agents.models import Agent
from banks.models import Bank
from codes.models import Transaction
from codes.services import CodeService
from common.enums import Zone
from merchants.models import Merchant

PL_IBAN = {'type': 'iban', 'value': 'PL61109010140000071219812874'}


@pytest.fixture(autouse=True)
def flush_redis():
    redis = get_redis_connection('default')
    redis.flushdb()
    yield
    redis.flushdb()


@pytest.fixture
def bank_pl(db):
    return Bank.objects.create(
        name='Bank PL',
        api_key_hash='bank_pl_hash',  # pragma: allowlist secret
        zone=Zone.PL,
        currency='PLN',
        debt_limit=Decimal('1000000.00'),
        webhook_url='https://bank-pl.example.com/webhook',
        c2b_enabled=True,
        active=True,
    )


@pytest.fixture
def bank_with_key(db):
    """Bank z wygenerowanym kluczem API plaintext."""
    from banks.models import generate_api_key as generate_bank_key

    plaintext, hash_ = generate_bank_key()
    bank = Bank.objects.create(
        name='Bank with Key',
        api_key_hash=hash_,
        zone=Zone.PL,
        currency='PLN',
        debt_limit=Decimal('1000000.00'),
        webhook_url='https://bank.example.com/webhook',
        c2b_enabled=True,
        active=True,
    )
    return bank, plaintext


@pytest.fixture
def agent_with_key(db, bank_pl):
    plaintext, hash_ = generate_agent_key()
    agent = Agent.objects.create(
        name='Agent',
        api_key_hash=hash_,
        settlement_bank=bank_pl,
        account_identifier=PL_IBAN,
        zone=Zone.PL,
    )
    return agent, plaintext


@pytest.fixture
def merchant(db, bank_pl):
    return Merchant.objects.create(
        name='Sklep Test',
        settlement_bank=bank_pl,
        account_identifier=PL_IBAN,
        zone=Zone.PL,
    )


@pytest.fixture
def client():
    return APIClient()


@pytest.mark.django_db
class TestCodeGenerate:
    def test_happy_path(self, client, bank_with_key):
        bank, key = bank_with_key
        response = client.post(
            '/api/v1/codes/generate',
            {'user_id': 'user-123', 'zone': 'PL'},
            format='json',
            HTTP_X_KLIK_BANK_API_KEY=key,
            HTTP_IDEMPOTENCY_KEY='test-1',
        )
        assert response.status_code == 200
        assert 'code' in response.data
        assert len(response.data['code']) == 6

    def test_missing_auth_returns_401(self, client):
        response = client.post(
            '/api/v1/codes/generate',
            {'user_id': 'u', 'zone': 'PL'},
            format='json',
            HTTP_IDEMPOTENCY_KEY='test-2',
        )
        assert response.status_code == 401
        assert response.data['error']['code'] == '401_UNAUTHORIZED'

    def test_missing_idempotency_key_returns_400(self, client, bank_with_key):
        bank, key = bank_with_key
        response = client.post(
            '/api/v1/codes/generate',
            {'user_id': 'u', 'zone': 'PL'},
            format='json',
            HTTP_X_KLIK_BANK_API_KEY=key,
        )
        assert response.status_code == 400
        assert 'IDEMPOTENCY' in response.data['error']['code']

    def test_zone_mismatch_returns_422(self, client, bank_with_key):
        bank, key = bank_with_key
        response = client.post(
            '/api/v1/codes/generate',
            {'user_id': 'u', 'zone': 'UK'},  # bank jest PL
            format='json',
            HTTP_X_KLIK_BANK_API_KEY=key,
            HTTP_IDEMPOTENCY_KEY='test-3',
        )
        assert response.status_code == 422
        assert response.data['error']['code'] == '422_ZONE_MISMATCH'


@pytest.mark.django_db
class TestPaymentInitiate:
    def test_happy_path(self, client, bank_with_key, agent_with_key, merchant):
        bank, _ = bank_with_key
        agent, agent_key = agent_with_key

        # Najpierw wygeneruj kod
        service = CodeService()
        code_result = service.generate_code(
            bank_id=str(bank.id),
            user_id='user-1',
            zone='PL',
        )

        # Mockujemy Celery task żeby nie próbował wywołać webhooka
        with patch('codes.tasks.authorize_webhook_task.delay') as mock_task:
            response = client.post(
                '/api/v1/payments/initiate',
                {
                    'code': code_result['code'],
                    'amount': '150.00',
                    'currency': 'PLN',
                    'merchant_id': str(merchant.id),
                },
                format='json',
                HTTP_X_KLIK_AGENT_API_KEY=agent_key,
                HTTP_IDEMPOTENCY_KEY='order-1',
            )

        assert response.status_code == 202
        assert 'transaction_id' in response.data
        assert response.data['status'] == 'PENDING'
        mock_task.assert_called_once()

    def test_expired_code_returns_404(self, client, agent_with_key, merchant):
        agent, key = agent_with_key
        with patch('codes.tasks.authorize_webhook_task.delay'):
            response = client.post(
                '/api/v1/payments/initiate',
                {
                    'code': '999999',  # nieistniejący
                    'amount': '150.00',
                    'currency': 'PLN',
                    'merchant_id': str(merchant.id),
                },
                format='json',
                HTTP_X_KLIK_AGENT_API_KEY=key,
                HTTP_IDEMPOTENCY_KEY='order-2',
            )
        assert response.status_code == 404
        assert response.data['error']['code'] == '404_CODE_EXPIRED'


@pytest.mark.django_db
class TestPaymentStatus:
    def test_bank_can_check_own_transaction(
        self,
        client,
        bank_with_key,
        agent_with_key,
        merchant,
    ):
        bank, key = bank_with_key
        agent, _ = agent_with_key

        tx = Transaction.objects.create(
            sender_bank=bank,
            agent=agent,
            merchant=merchant,
            code_snapshot='123456',
            amount_gross=Decimal('150.00'),
            currency='PLN',
            zone='PL',
            is_on_us=True,
            idempotency_key='test-status',
        )

        response = client.get(
            f'/api/v1/payments/status/{tx.id}',
            HTTP_X_KLIK_BANK_API_KEY=key,
        )
        assert response.status_code == 200
        assert response.data['status'] == 'PENDING'

    def test_other_bank_gets_404(self, client, agent_with_key, merchant, db):
        from banks.models import generate_api_key as generate_bank_key

        # Bank A — sender
        bank_a = Bank.objects.create(
            name='Bank A',
            api_key_hash='hash_a',  # pragma: allowlist secret
            zone=Zone.PL,
            currency='PLN',
            debt_limit=Decimal('1000000'),
            webhook_url='https://a.example.com',
            active=True,
        )
        # Bank B — nie jest stroną
        plaintext_b, hash_b = generate_bank_key()
        Bank.objects.create(
            name='Bank B',
            api_key_hash=hash_b,
            zone=Zone.PL,
            currency='PLN',
            debt_limit=Decimal('1000000'),
            webhook_url='https://b.example.com',
            active=True,
        )

        agent, _ = agent_with_key
        tx = Transaction.objects.create(
            sender_bank=bank_a,  # transaction Bank A
            agent=agent,
            merchant=merchant,
            code_snapshot='123456',
            amount_gross=Decimal('100'),
            currency='PLN',
            zone='PL',
            is_on_us=False,
            idempotency_key='test-other',
        )

        response = client.get(
            f'/api/v1/payments/status/{tx.id}',
            HTTP_X_KLIK_BANK_API_KEY=plaintext_b,  # Bank B pyta
        )
        # 404 zamiast 403 — nie ujawniamy istnienia
        assert response.status_code == 404
