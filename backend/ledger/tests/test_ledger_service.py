"""Testy LedgerService."""

from decimal import Decimal

import pytest
from django.utils import timezone

from agents.models import Agent, MSCAgreement
from banks.models import Bank
from codes.enums import TransactionStatus
from codes.models import Transaction
from common.enums import Zone
from ledger.enums import (
    BeneficiaryType,
    LedgerEntryType,
    SettlementSessionStatus,
    SettlementTransferStatus,
)
from ledger.exceptions import (
    ActiveSessionExistsError,
    FeesNotCalculatedError,
    InvalidSessionStateError,
    TransactionNotCompletedError,
)
from ledger.models import LedgerEntry, SettlementTransfer
from ledger.services import LedgerService
from merchants.models import Merchant

PL_IBAN = {'type': 'iban', 'value': 'PL61109010140000071219812874'}
PL_IBAN_2 = {'type': 'iban', 'value': 'PL27114020040000300201355387'}


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
        valid_from=timezone.now() - timezone.timedelta(days=1),
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
        account_identifier=PL_IBAN_2,
        zone=Zone.PL,
    )


def _make_completed_tx(*, sender_bank, agent, merchant, is_on_us, idempotency_key='order-1'):
    """Tworzy COMPLETED transakcję z policzonymi fees."""
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
        is_on_us=is_on_us,
        idempotency_key=idempotency_key,
        status=TransactionStatus.COMPLETED,
        authorized_at=timezone.now(),
        completed_at=timezone.now(),
    )
    return tx


# ----------------------------------------------------------------------
# record_c2b_transaction
# ----------------------------------------------------------------------


@pytest.mark.django_db
class TestRecordC2BTransaction:
    def test_off_us_creates_three_entries(
        self,
        bank_pl,
        bank_pl_2,
        agent,
        msc,
        merchant_off_us,
    ):
        tx = _make_completed_tx(
            sender_bank=bank_pl,
            agent=agent,
            merchant=merchant_off_us,
            is_on_us=False,
            idempotency_key='off-us-1',
        )
        entries = LedgerService.record_c2b_transaction(tx)

        assert len(entries) == 3

        # BANK_TO_BANK merchant_net
        bank_to_bank = next(e for e in entries if e.entry_type == LedgerEntryType.BANK_TO_BANK)
        assert bank_to_bank.from_bank_id == bank_pl.id
        assert bank_to_bank.to_bank_id == bank_pl_2.id
        assert bank_to_bank.beneficiary_type == BeneficiaryType.BANK
        assert bank_to_bank.beneficiary_ref == bank_pl_2.id
        assert bank_to_bank.amount == Decimal('148.05')

        # KLIK_FEE_C2B
        klik = next(e for e in entries if e.entry_type == LedgerEntryType.KLIK_FEE_C2B)
        assert klik.beneficiary_type == BeneficiaryType.KLIK
        assert klik.beneficiary_ref is None
        assert klik.amount == Decimal('0.45')

        # AGENT_FEE
        agent_e = next(e for e in entries if e.entry_type == LedgerEntryType.AGENT_FEE)
        assert agent_e.beneficiary_type == BeneficiaryType.AGENT
        assert agent_e.beneficiary_ref == agent.id
        assert agent_e.amount == Decimal('1.50')

    def test_on_us_creates_two_entries(
        self,
        bank_pl,
        agent,
        msc,
        merchant_on_us,
    ):
        tx = _make_completed_tx(
            sender_bank=bank_pl,
            agent=agent,
            merchant=merchant_on_us,
            is_on_us=True,
            idempotency_key='on-us-1',
        )
        entries = LedgerService.record_c2b_transaction(tx)

        # 2 entries: KLIK_FEE_C2B + AGENT_FEE (bez merchant_net dla on-us)
        assert len(entries) == 2
        types = {e.entry_type for e in entries}
        assert LedgerEntryType.BANK_TO_BANK not in types
        assert LedgerEntryType.KLIK_FEE_C2B in types
        assert LedgerEntryType.AGENT_FEE in types

    def test_idempotent_replay(
        self,
        bank_pl,
        bank_pl_2,
        agent,
        msc,
        merchant_off_us,
    ):
        tx = _make_completed_tx(
            sender_bank=bank_pl,
            agent=agent,
            merchant=merchant_off_us,
            is_on_us=False,
            idempotency_key='replay-1',
        )
        first_call = LedgerService.record_c2b_transaction(tx)
        second_call = LedgerService.record_c2b_transaction(tx)

        first_ids = {e.id for e in first_call}
        second_ids = {e.id for e in second_call}
        assert first_ids == second_ids
        assert LedgerEntry.objects.filter(source_ref='replay-1').count() == 3

    def test_pending_transaction_rejected(
        self,
        bank_pl,
        agent,
        msc,
        merchant_off_us,
    ):
        tx = Transaction.objects.create(
            sender_bank=bank_pl,
            agent=agent,
            merchant=merchant_off_us,
            code_snapshot='123456',
            amount_gross=Decimal('100.00'),
            currency='PLN',
            zone=Zone.PL,
            is_on_us=False,
            idempotency_key='pending-1',
            status=TransactionStatus.PENDING,
        )
        with pytest.raises(TransactionNotCompletedError):
            LedgerService.record_c2b_transaction(tx)

    def test_completed_without_fees_rejected(
        self,
        bank_pl,
        agent,
        msc,
        merchant_off_us,
    ):
        # Bypassujemy walidację `Transaction.clean()` przez bulk_create
        tx = Transaction(
            sender_bank=bank_pl,
            agent=agent,
            merchant=merchant_off_us,
            code_snapshot='123456',
            amount_gross=Decimal('100.00'),
            currency='PLN',
            zone=Zone.PL,
            is_on_us=False,
            idempotency_key='no-fees-1',
            status=TransactionStatus.COMPLETED,
            # brak fees
        )
        # bulk_create omija full_clean
        Transaction.objects.bulk_create([tx])
        tx_db = Transaction.objects.get(idempotency_key='no-fees-1')

        with pytest.raises(FeesNotCalculatedError):
            LedgerService.record_c2b_transaction(tx_db)

    def test_zero_klik_fee_skipped(
        self,
        bank_pl,
        bank_pl_2,
        agent,
        msc,
        merchant_off_us,
    ):
        """Jeśli klik_fee == 0, nie tworzymy entry (rzadki, ale możliwy edge case)."""
        tx = Transaction(
            sender_bank=bank_pl,
            agent=agent,
            merchant=merchant_off_us,
            code_snapshot='000000',
            amount_gross=Decimal('100.00'),
            klik_fee=Decimal('0.00'),
            agent_fee=Decimal('1.00'),
            merchant_net=Decimal('99.00'),
            currency='PLN',
            zone=Zone.PL,
            is_on_us=False,
            idempotency_key='zero-klik-1',
            status=TransactionStatus.COMPLETED,
            authorized_at=timezone.now(),
            completed_at=timezone.now(),
        )
        Transaction.objects.bulk_create([tx])
        tx_db = Transaction.objects.get(idempotency_key='zero-klik-1')

        entries = LedgerService.record_c2b_transaction(tx_db)
        types = {e.entry_type for e in entries}
        assert LedgerEntryType.KLIK_FEE_C2B not in types
        assert LedgerEntryType.AGENT_FEE in types
        assert LedgerEntryType.BANK_TO_BANK in types


# ----------------------------------------------------------------------
# Sesje rozliczeniowe
# ----------------------------------------------------------------------


@pytest.mark.django_db
class TestCreateSession:
    def test_creates_open_session(self, bank_pl):
        session = LedgerService.create_session(Zone.PL)
        assert session.status == SettlementSessionStatus.OPEN
        assert session.zone == Zone.PL

    def test_rejects_duplicate_active_session(self, bank_pl):
        LedgerService.create_session(Zone.PL)
        with pytest.raises(ActiveSessionExistsError):
            LedgerService.create_session(Zone.PL)

    def test_allows_session_after_completion(self, bank_pl):
        first = LedgerService.create_session(Zone.PL)
        first.status = SettlementSessionStatus.COMPLETED
        first.save()

        second = LedgerService.create_session(Zone.PL)
        assert second.id != first.id

    def test_different_zones_independent(self, bank_pl):
        s1 = LedgerService.create_session(Zone.PL)
        s2 = LedgerService.create_session(Zone.UK)
        assert s1.id != s2.id


@pytest.mark.django_db
class TestAssignPendingEntries:
    def test_assigns_only_pending_entries_of_zone(
        self,
        bank_pl,
        bank_pl_2,
        agent,
        msc,
        merchant_off_us,
    ):
        # Stwórz entries z C2B
        tx = _make_completed_tx(
            sender_bank=bank_pl,
            agent=agent,
            merchant=merchant_off_us,
            is_on_us=False,
            idempotency_key='session-test',
        )
        LedgerService.record_c2b_transaction(tx)

        session = LedgerService.create_session(Zone.PL)
        count = LedgerService.assign_pending_entries_to_session(session)

        assert count == 3
        session.refresh_from_db()
        assert session.status == SettlementSessionStatus.NETTING
        assert session.total_entries_count == 3
        assert session.total_volume == Decimal('150.00')  # 148.05 + 0.45 + 1.50

    def test_doesnt_reassign_settled_entries(
        self,
        bank_pl,
        bank_pl_2,
        agent,
        msc,
        merchant_off_us,
    ):
        tx = _make_completed_tx(
            sender_bank=bank_pl,
            agent=agent,
            merchant=merchant_off_us,
            is_on_us=False,
            idempotency_key='settled-test',
        )
        LedgerService.record_c2b_transaction(tx)

        # Ręcznie oznaczamy entries jako settled
        LedgerEntry.objects.filter(source_ref='settled-test').update(
            settled=True,
            settled_at=timezone.now(),
        )

        session = LedgerService.create_session(Zone.PL)
        count = LedgerService.assign_pending_entries_to_session(session)
        assert count == 0

    def test_only_open_session_can_be_filled(self, bank_pl):
        session = LedgerService.create_session(Zone.PL)
        session.status = SettlementSessionStatus.NETTING
        session.save()

        with pytest.raises(InvalidSessionStateError):
            LedgerService.assign_pending_entries_to_session(session)


@pytest.mark.django_db
class TestMarkSettled:
    def test_completed_transfers_settle_entries(
        self,
        bank_pl,
        bank_pl_2,
        agent,
        msc,
        merchant_off_us,
    ):
        tx = _make_completed_tx(
            sender_bank=bank_pl,
            agent=agent,
            merchant=merchant_off_us,
            is_on_us=False,
            idempotency_key='settle-test',
        )
        LedgerService.record_c2b_transaction(tx)

        session = LedgerService.create_session(Zone.PL)
        LedgerService.assign_pending_entries_to_session(session)

        # Stwórzmy ręcznie SettlementTransfer dla bank_pl → bank_pl_2 (BANK_TO_BANK)
        transfer = SettlementTransfer.objects.create(
            session=session,
            from_bank=bank_pl,
            to_bank=bank_pl_2,
            amount=Decimal('148.05'),
            currency='PLN',
        )

        # Symulujemy że dispatcher wszedł w fazę SETTLING
        session.status = SettlementSessionStatus.SETTLING
        session.save()

        result = LedgerService.mark_settled(
            session,
            {str(transfer.id): SettlementTransferStatus.COMPLETED},
        )

        assert result.status == SettlementSessionStatus.COMPLETED
        bank_to_bank_entry = LedgerEntry.objects.get(
            source_ref='settle-test',
            entry_type=LedgerEntryType.BANK_TO_BANK,
        )
        assert bank_to_bank_entry.settled is True
        assert bank_to_bank_entry.settled_at is not None

    def test_failed_transfer_marks_session_failed(
        self,
        bank_pl,
        bank_pl_2,
        agent,
        msc,
        merchant_off_us,
    ):
        tx = _make_completed_tx(
            sender_bank=bank_pl,
            agent=agent,
            merchant=merchant_off_us,
            is_on_us=False,
            idempotency_key='settle-fail',
        )
        LedgerService.record_c2b_transaction(tx)

        session = LedgerService.create_session(Zone.PL)
        LedgerService.assign_pending_entries_to_session(session)

        transfer = SettlementTransfer.objects.create(
            session=session,
            from_bank=bank_pl,
            to_bank=bank_pl_2,
            amount=Decimal('148.05'),
            currency='PLN',
        )

        session.status = SettlementSessionStatus.SETTLING
        session.save()

        result = LedgerService.mark_settled(
            session,
            {str(transfer.id): SettlementTransferStatus.FAILED},
        )

        assert result.status == SettlementSessionStatus.FAILED
        # Entries pozostają nieznettlowane
        bank_to_bank = LedgerEntry.objects.get(
            source_ref='settle-fail',
            entry_type=LedgerEntryType.BANK_TO_BANK,
        )
        assert bank_to_bank.settled is False


# ----------------------------------------------------------------------
# P2P skeleton
# ----------------------------------------------------------------------


@pytest.mark.django_db
class TestRecordP2PLookupFees:
    def test_skeleton_raises_not_implemented(self):
        with pytest.raises(NotImplementedError):
            LedgerService.record_p2p_lookup_fees(timezone.now().date())
