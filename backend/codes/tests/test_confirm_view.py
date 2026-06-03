"""Testy endpointu POST /api/v1/payments/confirm."""

from decimal import Decimal

import pytest
from django.utils import timezone
from django_redis import get_redis_connection
from rest_framework.test import APIClient

from agents.models import Agent, MSCAgreement
from banks.models import Bank
from banks.models import generate_api_key as generate_bank_key
from codes.enums import RejectReason, TransactionStatus
from codes.models import Transaction
from common.enums import Zone
from ledger.enums import LedgerEntryType
from ledger.models import LedgerEntry
from merchants.models import Merchant

PL_IBAN = {'type': 'iban', 'value': 'PL61109010140000071219812874'}
PL_IBAN_2 = {'type': 'iban', 'value': 'PL27114020040000300201355387'}


@pytest.fixture(autouse=True)
def flush_redis():
    redis = get_redis_connection('default')
    redis.flushdb()
    yield
    redis.flushdb()


@pytest.fixture
def bank_with_key(db):
    plaintext, hash_ = generate_bank_key()
    bank = Bank.objects.create(
        name='Sender Bank',
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
def bank_pl_2(db):
    return Bank.objects.create(
        name='Bank PL 2',
        api_key_hash='other_bank_hash',  # pragma: allowlist secret
        zone=Zone.PL,
        currency='PLN',
        debt_limit=Decimal('1000000.00'),
        webhook_url='https://bank-pl-2.example.com/webhook',
        c2b_enabled=True,
        active=True,
    )


@pytest.fixture
def agent(db, bank_with_key):
    bank, _ = bank_with_key
    return Agent.objects.create(
        name='Agent',
        api_key_hash='agent_hash',  # pragma: allowlist secret
        settlement_bank=bank,
        account_identifier=PL_IBAN,
        zone=Zone.PL,
    )


@pytest.fixture
def msc(db, agent):
    return MSCAgreement.objects.create(
        agent=agent,
        klik_fee_perc=Decimal('0.30'),
        agent_fee_perc=Decimal('1.00'),
        valid_from=timezone.now() - timezone.timedelta(days=1),
    )


@pytest.fixture
def merchant_off_us(db, bank_pl_2):
    return Merchant.objects.create(
        name='Sklep Off-Us',
        settlement_bank=bank_pl_2,
        account_identifier=PL_IBAN_2,
        zone=Zone.PL,
    )


@pytest.fixture
def merchant_on_us(db, bank_with_key):
    bank, _ = bank_with_key
    return Merchant.objects.create(
        name='Sklep On-Us',
        settlement_bank=bank,
        account_identifier=PL_IBAN,
        zone=Zone.PL,
    )


@pytest.fixture
def client():
    return APIClient()


def _create_pending_tx(*, sender_bank, agent, merchant, is_on_us, idempotency_key='order-1'):
    """Pomocnik: tworzy PENDING transakcję (jak po /payments/initiate)."""
    return Transaction.objects.create(
        sender_bank=sender_bank,
        agent=agent,
        merchant=merchant,
        code_snapshot='123456',
        amount_gross=Decimal('150.00'),
        currency='PLN',
        zone=Zone.PL,
        is_on_us=is_on_us,
        idempotency_key=idempotency_key,
        status=TransactionStatus.PENDING,
    )


@pytest.mark.django_db
class TestPaymentConfirmAccepted:
    def test_off_us_accepted_creates_three_ledger_entries(
        self,
        client,
        bank_with_key,
        agent,
        msc,
        merchant_off_us,
    ):
        bank, key = bank_with_key
        tx = _create_pending_tx(
            sender_bank=bank,
            agent=agent,
            merchant=merchant_off_us,
            is_on_us=False,
            idempotency_key='order-off-us',
        )

        response = client.post(
            '/api/v1/payments/confirm',
            {'transaction_id': str(tx.id), 'status': 'ACCEPTED'},
            format='json',
            HTTP_X_KLIK_BANK_API_KEY=key,
        )

        assert response.status_code == 200
        assert response.data['status'] == 'COMPLETED'
        assert response.data['klik_fee'] == '0.45'
        assert response.data['agent_fee'] == '1.50'
        assert response.data['merchant_net'] == '148.05'

        # 3 ledger entries
        entries = LedgerEntry.objects.filter(source_ref='order-off-us')
        assert entries.count() == 3
        types = set(entries.values_list('entry_type', flat=True))
        assert types == {
            LedgerEntryType.BANK_TO_BANK,
            LedgerEntryType.KLIK_FEE_C2B,
            LedgerEntryType.AGENT_FEE,
        }

    def test_on_us_accepted_creates_two_ledger_entries(
        self,
        client,
        bank_with_key,
        agent,
        msc,
        merchant_on_us,
    ):
        bank, key = bank_with_key
        tx = _create_pending_tx(
            sender_bank=bank,
            agent=agent,
            merchant=merchant_on_us,
            is_on_us=True,
            idempotency_key='order-on-us',
        )

        response = client.post(
            '/api/v1/payments/confirm',
            {'transaction_id': str(tx.id), 'status': 'ACCEPTED'},
            format='json',
            HTTP_X_KLIK_BANK_API_KEY=key,
        )

        assert response.status_code == 200
        assert response.data['status'] == 'COMPLETED'

        entries = LedgerEntry.objects.filter(source_ref='order-on-us')
        assert entries.count() == 2  # bez BANK_TO_BANK
        types = set(entries.values_list('entry_type', flat=True))
        assert LedgerEntryType.BANK_TO_BANK not in types

    def test_accepted_idempotent_replay(
        self,
        client,
        bank_with_key,
        agent,
        msc,
        merchant_off_us,
    ):
        bank, key = bank_with_key
        tx = _create_pending_tx(
            sender_bank=bank,
            agent=agent,
            merchant=merchant_off_us,
            is_on_us=False,
            idempotency_key='order-replay',
        )

        # Pierwsze
        response1 = client.post(
            '/api/v1/payments/confirm',
            {'transaction_id': str(tx.id), 'status': 'ACCEPTED'},
            format='json',
            HTTP_X_KLIK_BANK_API_KEY=key,
        )
        # Drugie — replay
        response2 = client.post(
            '/api/v1/payments/confirm',
            {'transaction_id': str(tx.id), 'status': 'ACCEPTED'},
            format='json',
            HTTP_X_KLIK_BANK_API_KEY=key,
        )

        assert response1.status_code == 200
        assert response2.status_code == 200
        # Brak duplikacji entries
        assert LedgerEntry.objects.filter(source_ref='order-replay').count() == 3


@pytest.mark.django_db
class TestPaymentConfirmRejected:
    def test_rejected_no_ledger_entries(
        self,
        client,
        bank_with_key,
        agent,
        merchant_off_us,
    ):
        bank, key = bank_with_key
        tx = _create_pending_tx(
            sender_bank=bank,
            agent=agent,
            merchant=merchant_off_us,
            is_on_us=False,
            idempotency_key='order-rejected',
        )

        response = client.post(
            '/api/v1/payments/confirm',
            {
                'transaction_id': str(tx.id),
                'status': 'REJECTED',
                'reject_reason': 'USER_DECLINED',
            },
            format='json',
            HTTP_X_KLIK_BANK_API_KEY=key,
        )

        assert response.status_code == 200
        assert response.data['status'] == 'REJECTED'
        assert response.data['reject_reason'] == 'USER_DECLINED'
        # Brak entries
        assert LedgerEntry.objects.filter(source_ref='order-rejected').count() == 0

    def test_rejected_without_reason_returns_400(
        self,
        client,
        bank_with_key,
        agent,
        merchant_off_us,
    ):
        bank, key = bank_with_key
        tx = _create_pending_tx(
            sender_bank=bank,
            agent=agent,
            merchant=merchant_off_us,
            is_on_us=False,
            idempotency_key='order-no-reason',
        )

        response = client.post(
            '/api/v1/payments/confirm',
            {'transaction_id': str(tx.id), 'status': 'REJECTED'},
            format='json',
            HTTP_X_KLIK_BANK_API_KEY=key,
        )
        assert response.status_code == 400


@pytest.mark.django_db
class TestPaymentConfirmEdgeCases:
    def test_other_bank_gets_404(
        self,
        client,
        bank_with_key,
        bank_pl_2,
        agent,
        merchant_off_us,
    ):
        bank, _ = bank_with_key
        tx = _create_pending_tx(
            sender_bank=bank,
            agent=agent,
            merchant=merchant_off_us,
            is_on_us=False,
            idempotency_key='order-other-bank',
        )

        # Inny bank
        plaintext_other, hash_other = generate_bank_key()
        Bank.objects.create(
            name='Other Bank',
            api_key_hash=hash_other,
            zone=Zone.PL,
            currency='PLN',
            debt_limit=Decimal('1000000'),
            webhook_url='https://other.example.com',
            c2b_enabled=True,
            active=True,
        )

        response = client.post(
            '/api/v1/payments/confirm',
            {'transaction_id': str(tx.id), 'status': 'ACCEPTED'},
            format='json',
            HTTP_X_KLIK_BANK_API_KEY=plaintext_other,
        )
        # 404 zamiast 403 — security
        assert response.status_code == 404

    def test_nonexistent_transaction_returns_404(self, client, bank_with_key):
        bank, key = bank_with_key
        response = client.post(
            '/api/v1/payments/confirm',
            {
                'transaction_id': '00000000-0000-0000-0000-000000000000',
                'status': 'ACCEPTED',
            },
            format='json',
            HTTP_X_KLIK_BANK_API_KEY=key,
        )
        assert response.status_code == 404

    def test_accepted_after_rejected_returns_409(
        self,
        client,
        bank_with_key,
        agent,
        merchant_off_us,
    ):
        bank, key = bank_with_key
        tx = _create_pending_tx(
            sender_bank=bank,
            agent=agent,
            merchant=merchant_off_us,
            is_on_us=False,
            idempotency_key='order-conflict',
        )

        # Najpierw REJECTED
        client.post(
            '/api/v1/payments/confirm',
            {
                'transaction_id': str(tx.id),
                'status': 'REJECTED',
                'reject_reason': 'USER_DECLINED',
            },
            format='json',
            HTTP_X_KLIK_BANK_API_KEY=key,
        )

        # Potem ACCEPTED — konflikt
        response = client.post(
            '/api/v1/payments/confirm',
            {'transaction_id': str(tx.id), 'status': 'ACCEPTED'},
            format='json',
            HTTP_X_KLIK_BANK_API_KEY=key,
        )
        assert response.status_code == 409
        assert 'CONFLICT' in response.data['error']['code']

    def test_confirm_on_timeout_returns_409(
        self,
        client,
        bank_with_key,
        agent,
        merchant_off_us,
    ):
        bank, key = bank_with_key
        tx = _create_pending_tx(
            sender_bank=bank,
            agent=agent,
            merchant=merchant_off_us,
            is_on_us=False,
            idempotency_key='order-timeout',
        )
        tx.status = TransactionStatus.TIMEOUT
        tx.reject_reason = RejectReason.OTHER
        tx.save()

        response = client.post(
            '/api/v1/payments/confirm',
            {'transaction_id': str(tx.id), 'status': 'ACCEPTED'},
            format='json',
            HTTP_X_KLIK_BANK_API_KEY=key,
        )
        assert response.status_code == 409
        assert 'CLOSED' in response.data['error']['code']

    def test_accepted_without_msc_returns_422(
        self,
        client,
        bank_with_key,
        agent,
        merchant_off_us,
    ):
        # Brak fixture msc — agent bez aktywnej umowy
        bank, key = bank_with_key
        tx = _create_pending_tx(
            sender_bank=bank,
            agent=agent,
            merchant=merchant_off_us,
            is_on_us=False,
            idempotency_key='order-no-msc',
        )

        response = client.post(
            '/api/v1/payments/confirm',
            {'transaction_id': str(tx.id), 'status': 'ACCEPTED'},
            format='json',
            HTTP_X_KLIK_BANK_API_KEY=key,
        )
        assert response.status_code == 422
        assert response.data['error']['code'] == '422_NO_ACTIVE_MSC'

        # Tx pozostaje PENDING — bank może retry
        tx.refresh_from_db()
        assert tx.status == TransactionStatus.PENDING

    def test_missing_auth_returns_401(self, client):
        response = client.post(
            '/api/v1/payments/confirm',
            {'transaction_id': '00000000-0000-0000-0000-000000000000', 'status': 'ACCEPTED'},
            format='json',
        )
        assert response.status_code == 401
