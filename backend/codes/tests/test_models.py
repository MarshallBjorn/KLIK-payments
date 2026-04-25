"""Testy modelu Transaction."""

from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db.utils import IntegrityError
from django.utils import timezone

from agents.models import Agent
from banks.models import Bank
from codes.enums import TransactionStatus
from codes.models import Transaction
from common.enums import Zone
from merchants.models import Merchant

PL_IBAN = {'type': 'iban', 'value': 'PL61109010140000071219812874'}


@pytest.fixture
def bank_pl(db):
    return Bank.objects.create(
        name='Bank PL',
        api_key_hash='bank_pl_hash',
        zone=Zone.PL,
        currency='PLN',
        debt_limit=Decimal('1000000.00'),
        webhook_url='https://bank-pl.example.com/webhook',
    )


@pytest.fixture
def bank_pl_2(db):
    return Bank.objects.create(
        name='Bank PL 2',
        api_key_hash='bank_pl_2_hash',
        zone=Zone.PL,
        currency='PLN',
        debt_limit=Decimal('1000000.00'),
        webhook_url='https://bank-pl-2.example.com/webhook',
    )


@pytest.fixture
def agent(db, bank_pl):
    return Agent.objects.create(
        name='Agent',
        api_key_hash='agent_hash',
        settlement_bank=bank_pl,
        account_identifier=PL_IBAN,
        zone=Zone.PL,
    )


@pytest.fixture
def merchant_on_us(db, bank_pl):
    """Merchant z kontem w bank_pl — ten sam co przyszły sender."""
    return Merchant.objects.create(
        name='Sklep On-Us',
        settlement_bank=bank_pl,
        account_identifier=PL_IBAN,
        zone=Zone.PL,
    )


@pytest.fixture
def merchant_off_us(db, bank_pl_2):
    """Merchant z kontem w innym banku."""
    return Merchant.objects.create(
        name='Sklep Off-Us',
        settlement_bank=bank_pl_2,
        account_identifier={'type': 'iban', 'value': 'PL27114020040000300201355387'},
        zone=Zone.PL,
    )


@pytest.mark.django_db
class TestTransactionCreate:
    def test_create_pending_transaction(self, bank_pl, agent, merchant_off_us):
        tx = Transaction.objects.create(
            sender_bank=bank_pl,
            agent=agent,
            merchant=merchant_off_us,
            code_snapshot='123456',
            amount_gross=Decimal('150.00'),
            currency='PLN',
            zone=Zone.PL,
            is_on_us=False,
            idempotency_key='order-123',
        )
        assert tx.id is not None
        assert tx.status == TransactionStatus.PENDING

    def test_currency_must_match_zone(self, bank_pl, agent, merchant_off_us):
        tx = Transaction(
            sender_bank=bank_pl,
            agent=agent,
            merchant=merchant_off_us,
            code_snapshot='123456',
            amount_gross=Decimal('150.00'),
            currency='EUR',  # zła waluta dla PL
            zone=Zone.PL,
            is_on_us=False,
            idempotency_key='order-456',
        )
        with pytest.raises(ValidationError, match='waluty PLN'):
            tx.save()

    def test_amount_must_be_positive(self, bank_pl, agent, merchant_off_us):
        tx = Transaction(
            sender_bank=bank_pl,
            agent=agent,
            merchant=merchant_off_us,
            code_snapshot='123456',
            amount_gross=Decimal('0.00'),
            currency='PLN',
            zone=Zone.PL,
            is_on_us=False,
            idempotency_key='order-789',
        )
        with pytest.raises((ValidationError, IntegrityError)):
            tx.save()

    def test_idempotency_unique_per_agent(self, bank_pl, agent, merchant_off_us):
        Transaction.objects.create(
            sender_bank=bank_pl,
            agent=agent,
            merchant=merchant_off_us,
            code_snapshot='111111',
            amount_gross=Decimal('100.00'),
            currency='PLN',
            zone=Zone.PL,
            is_on_us=False,
            idempotency_key='same-key',
        )
        with pytest.raises((ValidationError, IntegrityError)):
            Transaction.objects.create(
                sender_bank=bank_pl,
                agent=agent,
                merchant=merchant_off_us,
                code_snapshot='222222',
                amount_gross=Decimal('200.00'),
                currency='PLN',
                zone=Zone.PL,
                is_on_us=False,
                idempotency_key='same-key',  # duplikat
            )


@pytest.mark.django_db
class TestTransactionStatus:
    def test_completed_requires_fees(self, bank_pl, agent, merchant_off_us):
        tx = Transaction(
            sender_bank=bank_pl,
            agent=agent,
            merchant=merchant_off_us,
            code_snapshot='123456',
            amount_gross=Decimal('150.00'),
            currency='PLN',
            zone=Zone.PL,
            is_on_us=False,
            idempotency_key='order-completed',
            status=TransactionStatus.COMPLETED,
            # Brak klik_fee, agent_fee, merchant_net
        )
        with pytest.raises(ValidationError, match='COMPLETED wymaga'):
            tx.save()

    def test_rejected_requires_reason(self, bank_pl, agent, merchant_off_us):
        tx = Transaction(
            sender_bank=bank_pl,
            agent=agent,
            merchant=merchant_off_us,
            code_snapshot='123456',
            amount_gross=Decimal('150.00'),
            currency='PLN',
            zone=Zone.PL,
            is_on_us=False,
            idempotency_key='order-rejected',
            status=TransactionStatus.REJECTED,
            # Brak reject_reason
        )
        with pytest.raises(ValidationError, match='REJECTED wymaga'):
            tx.save()

    def test_total_fees_calculation(self, bank_pl, agent, merchant_off_us):
        tx = Transaction.objects.create(
            sender_bank=bank_pl,
            agent=agent,
            merchant=merchant_off_us,
            code_snapshot='123456',
            amount_gross=Decimal('150.00'),
            klik_fee=Decimal('0.45'),
            agent_fee=Decimal('1.50'),
            merchant_net=Decimal('148.05'),
            currency='PLN',
            zone=Zone.PL,
            is_on_us=False,
            idempotency_key='order-fees',
            status=TransactionStatus.COMPLETED,
            authorized_at=timezone.now(),
            completed_at=timezone.now(),
        )
        assert tx.total_fees == Decimal('1.95')
