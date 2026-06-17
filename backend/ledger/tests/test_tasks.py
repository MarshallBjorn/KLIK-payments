"""Testy Celery tasków: run_settlement_session i accrue_p2p_lookup_fees.

Strategia:
- Tasky odpalane synchronicznie (CELERY_TASK_ALWAYS_EAGER w settings dev/test).
- RTGS dispatcher patchowany na FakeGateway żeby nie ruszać HTTP.
- P2P fee accrual: setupujemy klucze w Redisie, sprawdzamy entries.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch
from uuid import uuid4

import pytest
from django.utils import timezone
from django_redis import get_redis_connection

from agents.models import Agent, MSCAgreement
from banks.models import Bank
from codes.enums import TransactionStatus
from codes.models import Transaction
from common.enums import Zone
from ledger.enums import (
    LedgerEntryType,
    SettlementSessionStatus,
    SettlementTransferStatus,
)
from ledger.models import LedgerEntry, SettlementSession, SettlementTransfer
from ledger.rtgs import RTGSDispatcher, TransferResult, TransferStatus
from ledger.rtgs.gateway import RTGSGateway
from ledger.services import LedgerService
from ledger.tasks import accrue_p2p_lookup_fees, run_settlement_session
from merchants.models import Merchant

PL_IBAN = {'type': 'iban', 'value': 'PL61109010140000071219812874'}
PL_IBAN_2 = {'type': 'iban', 'value': 'PL27114020040000300201355387'}


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


class AlwaysSuccessGateway(RTGSGateway):
    system_name = 'TEST_OK'

    def __init__(self):
        super().__init__(base_url='http://test')

    def settle(self, session_id, transfers):
        return [
            TransferResult(
                transfer_id=t.transfer_id,
                status=TransferStatus.SUCCESS,
                rtgs_reference=f'OK-{t.transfer_id.hex[:6]}',
            )
            for t in transfers
        ]

    def healthcheck(self):
        return True


class AlwaysFailGateway(RTGSGateway):
    system_name = 'TEST_FAIL'

    def __init__(self):
        super().__init__(base_url='http://test')

    def settle(self, session_id, transfers):
        return [
            TransferResult(
                transfer_id=t.transfer_id,
                status=TransferStatus.FAILED,
                failure_reason='Test failure',
            )
            for t in transfers
        ]

    def healthcheck(self):
        return True


def _patched_dispatcher(gateway: RTGSGateway):
    """Helper: patchuje RTGSDispatcher.from_settings żeby wszystkie strefy
    używały podanego gateway-a."""
    return patch.object(
        RTGSDispatcher,
        'from_settings',
        return_value=RTGSDispatcher({z.value: gateway for z in Zone}),
    )


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------


@pytest.fixture
def bank_pl(db):
    return Bank.objects.create(
        name='Bank PL',
        api_key_hash='bank_pl_hash',  # pragma: allowlist secret
        zone=Zone.PL,
        currency='PLN',
        debt_limit=Decimal('1000000.00'),
        webhook_url='https://bank-pl.example.com/webhook',
    )


@pytest.fixture
def bank_pl_2(db):
    return Bank.objects.create(
        name='Bank PL 2',
        api_key_hash='bank_pl_2_hash',  # pragma: allowlist secret
        zone=Zone.PL,
        currency='PLN',
        debt_limit=Decimal('1000000.00'),
        webhook_url='https://bank-pl-2.example.com/webhook',
    )


@pytest.fixture
def agent(db, bank_pl):
    return Agent.objects.create(
        name='Agent',
        api_key_hash='agent_hash',  # pragma: allowlist secret
        settlement_bank=bank_pl,
        account_identifier=PL_IBAN,
        zone=Zone.PL,
    )


@pytest.fixture
def msc(db, agent):
    return MSCAgreement.objects.create(
        agent=agent,
        klik_fee_perc=Decimal('0.30'),
        agent_fee_perc=Decimal('1.00'),
        valid_from=timezone.now() - timedelta(days=1),
    )


@pytest.fixture
def merchant_off_us(db, bank_pl_2):
    return Merchant.objects.create(
        name='Sklep Off-Us',
        settlement_bank=bank_pl_2,
        account_identifier=PL_IBAN_2,
        zone=Zone.PL,
    )


def _make_completed_tx(*, sender_bank, agent, merchant, idempotency_key):
    tx = Transaction.objects.create(
        sender_bank=sender_bank,
        agent=agent,
        merchant=merchant,
        code_snapshot='123456',
        amount_gross=Decimal('150.00'),
        klik_fee=Decimal('0.45'),
        agent_fee=Decimal('1.50'),
        merchant_net=Decimal('148.05'),
        currency='PLN',
        zone=Zone.PL,
        is_on_us=False,
        idempotency_key=idempotency_key,
        status=TransactionStatus.COMPLETED,
        authorized_at=timezone.now(),
        completed_at=timezone.now(),
    )
    return tx


# ----------------------------------------------------------------------
# run_settlement_session
# ----------------------------------------------------------------------


@pytest.mark.django_db
class TestRunSettlementSessionHappyPath:
    def test_full_pipeline_with_one_transaction(
        self, bank_pl, bank_pl_2, agent, msc, merchant_off_us
    ):
        """Off-us tx → 3 entries (BANK_TO_BANK, KLIK_FEE, AGENT_FEE)
        → 2 transfery (po nettingu KLIK_FEE i AGENT_FEE z bank_pl do bank_pl
        są self-zero, BANK_TO_BANK zostaje)."""
        tx = _make_completed_tx(
            sender_bank=bank_pl,
            agent=agent,
            merchant=merchant_off_us,
            idempotency_key='order-1',
        )
        LedgerService.record_c2b_transaction(tx)

        with _patched_dispatcher(AlwaysSuccessGateway()):
            result = run_settlement_session.apply(args=['PL']).get()

        assert result['status'] == 'COMPLETED'
        assert result['entries_count'] == 3

        session = SettlementSession.objects.get(id=result['session_id'])
        assert session.status == SettlementSessionStatus.COMPLETED

        # Wszystkie ledger entries settled
        unsettled = LedgerEntry.objects.filter(session=session, settled=False).count()
        assert unsettled == 0

    def test_no_pending_entries_creates_empty_session(self, bank_pl):
        """Brak entries → sesja COMPLETED z 0 transferów (audit trail)."""
        with _patched_dispatcher(AlwaysSuccessGateway()):
            result = run_settlement_session.apply(args=['PL']).get()

        assert result['status'] == 'COMPLETED'
        assert result['entries_count'] == 0
        assert result['transfers_count'] == 0

    def test_skipped_when_active_session_exists(self, bank_pl):
        """Drugi trigger podczas trwającej sesji jest skipowany."""
        LedgerService.create_session(Zone.PL)  # zostawia OPEN

        with _patched_dispatcher(AlwaysSuccessGateway()):
            result = run_settlement_session.apply(args=['PL']).get()

        assert result['status'] == 'SKIPPED'
        assert result['reason'] == 'active_session_exists'

    def test_invalid_zone_returns_error(self):
        result = run_settlement_session.apply(args=['XX']).get()
        assert result['status'] == 'ERROR'


@pytest.mark.django_db
class TestRunSettlementSessionFailures:
    def test_rtgs_failure_marks_session_failed(
        self, bank_pl, bank_pl_2, agent, msc, merchant_off_us
    ):
        tx = _make_completed_tx(
            sender_bank=bank_pl,
            agent=agent,
            merchant=merchant_off_us,
            idempotency_key='order-fail-1',
        )
        LedgerService.record_c2b_transaction(tx)

        with _patched_dispatcher(AlwaysFailGateway()):
            result = run_settlement_session.apply(args=['PL']).get()

        assert result['status'] == 'FAILED'
        session = SettlementSession.objects.get(id=result['session_id'])
        assert session.status == SettlementSessionStatus.FAILED

        # Entries pozostają niesettled — wrócą do następnej sesji
        unsettled_in_session = LedgerEntry.objects.filter(session=session, settled=False).count()
        assert unsettled_in_session > 0

    def test_failed_transfer_records_failure_reason(
        self, bank_pl, bank_pl_2, agent, msc, merchant_off_us
    ):
        tx = _make_completed_tx(
            sender_bank=bank_pl,
            agent=agent,
            merchant=merchant_off_us,
            idempotency_key='order-failure-msg',
        )
        LedgerService.record_c2b_transaction(tx)

        with _patched_dispatcher(AlwaysFailGateway()):
            run_settlement_session.apply(args=['PL']).get()

        failed = SettlementTransfer.objects.filter(status=SettlementTransferStatus.FAILED)
        assert failed.exists()
        assert failed.first().failure_reason == 'Test failure'


# ----------------------------------------------------------------------
# accrue_p2p_lookup_fees
# ----------------------------------------------------------------------


@pytest.fixture
def bank_pl_paid_lookups(db):
    """Bank z włączonym P2P i niezerowym lookup fee."""
    return Bank.objects.create(
        name='Bank P2P PL',
        api_key_hash='bank_p2p_pl_hash',  # pragma: allowlist secret
        zone=Zone.PL,
        currency='PLN',
        debt_limit=Decimal('1000000.00'),
        p2p_enabled=True,
        p2p_lookup_fee=Decimal('0.05'),  # 5 groszy za lookup
    )


@pytest.fixture
def bank_uk_paid_lookups(db):
    return Bank.objects.create(
        name='Bank P2P UK',
        api_key_hash='bank_p2p_uk_hash',  # pragma: allowlist secret
        zone=Zone.UK,
        currency='GBP',
        debt_limit=Decimal('1000000.00'),
        p2p_enabled=True,
        p2p_lookup_fee=Decimal('0.10'),
    )


@pytest.fixture
def clean_redis():
    """Czyści counter klucze przed/po testem."""
    redis = get_redis_connection('default')
    keys = redis.keys('aliases:lookups:*')
    if keys:
        redis.delete(*keys)
    yield redis
    keys = redis.keys('aliases:lookups:*')
    if keys:
        redis.delete(*keys)


def _set_counter(redis, bank_id, target_date: date, count: int):
    key = f'aliases:lookups:{bank_id}:{target_date.strftime("%Y%m%d")}'
    redis.set(key, count)


@pytest.mark.django_db
class TestAccrueP2PLookupFees:
    def test_creates_ledger_entry_per_bank(
        self, bank_pl_paid_lookups, bank_uk_paid_lookups, clean_redis
    ):
        today = timezone.now().date()
        _set_counter(clean_redis, bank_pl_paid_lookups.id, today, 100)
        _set_counter(clean_redis, bank_uk_paid_lookups.id, today, 50)

        result = accrue_p2p_lookup_fees.apply(args=[today.isoformat()]).get()

        assert result['status'] == 'OK'
        assert result['entries_created'] == 2

        pl_entry = LedgerEntry.objects.get(from_bank=bank_pl_paid_lookups)
        # 100 × 0.05 = 5.00
        assert pl_entry.amount == Decimal('5.00')
        assert pl_entry.entry_type == LedgerEntryType.P2P_LOOKUP_FEE
        assert pl_entry.zone == Zone.PL
        assert pl_entry.currency == 'PLN'

        uk_entry = LedgerEntry.objects.get(from_bank=bank_uk_paid_lookups)
        # 50 × 0.10 = 5.00
        assert uk_entry.amount == Decimal('5.00')

    def test_clears_counters_after_processing(self, bank_pl_paid_lookups, clean_redis):
        today = timezone.now().date()
        key = f'aliases:lookups:{bank_pl_paid_lookups.id}:{today.strftime("%Y%m%d")}'
        clean_redis.set(key, 10)

        accrue_p2p_lookup_fees.apply(args=[today.isoformat()]).get()

        assert clean_redis.get(key) is None

    def test_zero_fee_bank_skipped(self, db, clean_redis):
        bank = Bank.objects.create(
            name='Free Bank',
            api_key_hash='free_hash',  # pragma: allowlist secret
            zone=Zone.PL,
            currency='PLN',
            p2p_enabled=True,
            p2p_lookup_fee=Decimal('0.00'),
        )
        today = timezone.now().date()
        _set_counter(clean_redis, bank.id, today, 1000)

        result = accrue_p2p_lookup_fees.apply(args=[today.isoformat()]).get()
        assert result['entries_created'] == 0
        # Counter też wyczyszczony żeby nie akumulować
        key = f'aliases:lookups:{bank.id}:{today.strftime("%Y%m%d")}'
        assert clean_redis.get(key) is None

    def test_idempotent_second_run_no_duplicate(self, bank_pl_paid_lookups, clean_redis):
        today = timezone.now().date()
        _set_counter(clean_redis, bank_pl_paid_lookups.id, today, 100)

        # Pierwszy run
        result_1 = accrue_p2p_lookup_fees.apply(args=[today.isoformat()]).get()
        assert result_1['entries_created'] == 1

        # Drugi run dla tej samej daty — manualnie przywracamy counter
        # (po drugim runie powinien zostać skipowany)
        _set_counter(clean_redis, bank_pl_paid_lookups.id, today, 100)
        result_2 = accrue_p2p_lookup_fees.apply(args=[today.isoformat()]).get()
        assert result_2['entries_created'] == 0  # Zero — bo source_ref już istnieje

        assert LedgerEntry.objects.filter(from_bank=bank_pl_paid_lookups).count() == 1

    def test_no_counters_returns_empty(self, clean_redis):
        today = timezone.now().date()
        result = accrue_p2p_lookup_fees.apply(args=[today.isoformat()]).get()
        assert result['status'] == 'OK'
        assert result['entries_created'] == 0

    def test_invalid_date_returns_error(self):
        result = accrue_p2p_lookup_fees.apply(args=['nope']).get()
        assert result['status'] == 'ERROR'

    def test_unknown_bank_in_counter_skipped(self, clean_redis):
        today = timezone.now().date()
        # Counter dla nieistniejącego banku
        fake_bank_id = uuid4()
        _set_counter(clean_redis, fake_bank_id, today, 50)

        result = accrue_p2p_lookup_fees.apply(args=[today.isoformat()]).get()
        assert result['entries_created'] == 0


@pytest.mark.django_db
def test_klik_fee_routed_to_operator_when_zone_configured(
    settings, bank_pl, agent, msc, merchant_off_us
):
    """Gdy strefa ma operatora KLIK → KLIK_FEE leci do niego (realne inkaso, from≠to)."""
    klik = Bank.objects.create(
        name='KLIK Operator PL',
        api_key_hash='klik_op_pl_hash',  # pragma: allowlist secret
        zone=Zone.PL,
        currency='PLN',
        debt_limit=Decimal('0'),
        webhook_url='https://klik.example.com/webhook',
    )
    settings.KLIK_OPERATOR_BANK_BY_ZONE = {'PL': 'KLIK Operator PL'}

    tx = _make_completed_tx(
        sender_bank=bank_pl,
        agent=agent,
        merchant=merchant_off_us,
        idempotency_key='klik-fee-route-operator',
    )
    LedgerService.record_c2b_transaction(tx)

    fee = LedgerEntry.objects.get(
        source_ref='klik-fee-route-operator', entry_type=LedgerEntryType.KLIK_FEE_C2B
    )
    assert fee.from_bank_id == bank_pl.id
    assert fee.to_bank_id == klik.id  # from ≠ to → wygeneruje przelew RTGS


@pytest.mark.django_db
def test_klik_fee_collect_at_source_when_zone_not_configured(
    settings, bank_pl, agent, msc, merchant_off_us
):
    """Brak operatora dla strefy → stary tryb collect-at-source (from==to, netuje do zera)."""
    settings.KLIK_OPERATOR_BANK_BY_ZONE = {}

    tx = _make_completed_tx(
        sender_bank=bank_pl,
        agent=agent,
        merchant=merchant_off_us,
        idempotency_key='klik-fee-route-fallback',
    )
    LedgerService.record_c2b_transaction(tx)

    fee = LedgerEntry.objects.get(
        source_ref='klik-fee-route-fallback', entry_type=LedgerEntryType.KLIK_FEE_C2B
    )
    assert fee.to_bank_id == bank_pl.id  # from == to → bez przelewu RTGS
