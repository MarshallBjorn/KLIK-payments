"""Testy modeli apki ledger."""

from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db.utils import IntegrityError
from django.utils import timezone

from banks.models import Bank
from common.enums import Zone
from ledger.enums import (
    BeneficiaryType,
    LedgerEntryType,
    SettlementSessionStatus,
    SettlementTransferStatus,
)
from ledger.models import LedgerEntry, SettlementSession, SettlementTransfer


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
def bank_uk(db):
    return Bank.objects.create(
        name='Bank UK',
        api_key_hash='bank_uk_hash',  # pragma: allowlist secret
        zone=Zone.UK,
        currency='GBP',
        debt_limit=Decimal('1000000.00'),
        webhook_url='https://bank-uk.example.com/webhook',
    )


@pytest.mark.django_db
class TestLedgerEntry:
    def test_create_bank_to_bank_entry(self, bank_pl, bank_pl_2):
        entry = LedgerEntry.objects.create(
            from_bank=bank_pl,
            to_bank=bank_pl_2,
            beneficiary_type=BeneficiaryType.BANK,
            beneficiary_ref=bank_pl_2.id,
            amount=Decimal('100.00'),
            currency='PLN',
            zone=Zone.PL,
            entry_type=LedgerEntryType.BANK_TO_BANK,
            source_ref='order-123',
        )
        assert entry.id is not None
        assert entry.settled is False
        assert entry.settled_at is None

    def test_klik_beneficiary_must_have_null_ref(self, bank_pl):
        entry = LedgerEntry(
            from_bank=bank_pl,
            to_bank=bank_pl,
            beneficiary_type=BeneficiaryType.KLIK,
            beneficiary_ref=bank_pl.id,  # nie powinno być
            amount=Decimal('1.00'),
            currency='PLN',
            zone=Zone.PL,
            entry_type=LedgerEntryType.KLIK_FEE_C2B,
            source_ref='order-456',
        )
        with pytest.raises((ValidationError, IntegrityError)):
            entry.save()

    def test_bank_beneficiary_requires_ref(self, bank_pl, bank_pl_2):
        entry = LedgerEntry(
            from_bank=bank_pl,
            to_bank=bank_pl_2,
            beneficiary_type=BeneficiaryType.BANK,
            beneficiary_ref=None,  # wymagane
            amount=Decimal('100.00'),
            currency='PLN',
            zone=Zone.PL,
            entry_type=LedgerEntryType.BANK_TO_BANK,
            source_ref='order-789',
        )
        with pytest.raises((ValidationError, IntegrityError)):
            entry.save()

    def test_amount_must_be_positive(self, bank_pl):
        entry = LedgerEntry(
            from_bank=bank_pl,
            to_bank=bank_pl,
            beneficiary_type=BeneficiaryType.KLIK,
            beneficiary_ref=None,
            amount=Decimal('0'),
            currency='PLN',
            zone=Zone.PL,
            entry_type=LedgerEntryType.KLIK_FEE_C2B,
            source_ref='order-zero',
        )
        with pytest.raises((ValidationError, IntegrityError)):
            entry.save()

    def test_currency_must_match_zone(self, bank_pl, bank_pl_2):
        entry = LedgerEntry(
            from_bank=bank_pl,
            to_bank=bank_pl_2,
            beneficiary_type=BeneficiaryType.BANK,
            beneficiary_ref=bank_pl_2.id,
            amount=Decimal('100.00'),
            currency='EUR',  # zła waluta
            zone=Zone.PL,
            entry_type=LedgerEntryType.BANK_TO_BANK,
            source_ref='order-bad-currency',
        )
        with pytest.raises(ValidationError, match='waluty PLN'):
            entry.save()

    def test_bank_zones_must_match_entry_zone(self, bank_pl, bank_uk):
        entry = LedgerEntry(
            from_bank=bank_pl,
            to_bank=bank_uk,  # różne strefy
            beneficiary_type=BeneficiaryType.BANK,
            beneficiary_ref=bank_uk.id,
            amount=Decimal('100.00'),
            currency='PLN',
            zone=Zone.PL,
            entry_type=LedgerEntryType.BANK_TO_BANK,
            source_ref='order-mixed-zones',
        )
        with pytest.raises(ValidationError):
            entry.save()


@pytest.mark.django_db
class TestSettlementSession:
    def test_create_session(self):
        session = SettlementSession.objects.create(
            zone=Zone.PL,
            started_at=timezone.now(),
        )
        assert session.id is not None
        assert session.status == SettlementSessionStatus.OPEN
        assert session.total_entries_count == 0
        assert session.total_volume == Decimal('0')


@pytest.mark.django_db
class TestSettlementTransfer:
    @pytest.fixture
    def session(self):
        return SettlementSession.objects.create(
            zone=Zone.PL,
            started_at=timezone.now(),
        )

    def test_create_transfer(self, session, bank_pl, bank_pl_2):
        transfer = SettlementTransfer.objects.create(
            session=session,
            from_bank=bank_pl,
            to_bank=bank_pl_2,
            amount=Decimal('500.00'),
            currency='PLN',
        )
        assert transfer.id is not None
        assert transfer.status == SettlementTransferStatus.PENDING

    def test_from_to_must_differ(self, session, bank_pl):
        transfer = SettlementTransfer(
            session=session,
            from_bank=bank_pl,
            to_bank=bank_pl,  # to samo
            amount=Decimal('100.00'),
            currency='PLN',
        )
        with pytest.raises(IntegrityError):
            transfer.save()

    def test_amount_positive(self, session, bank_pl, bank_pl_2):
        transfer = SettlementTransfer(
            session=session,
            from_bank=bank_pl,
            to_bank=bank_pl_2,
            amount=Decimal('0'),
            currency='PLN',
        )
        with pytest.raises(IntegrityError):
            transfer.save()
