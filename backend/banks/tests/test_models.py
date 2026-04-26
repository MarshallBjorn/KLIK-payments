"""Testy flag modułowych Banku (C2B / P2P).

Pokrywamy:
- defaultowe wartości pól modułowych przy nowym banku
- możliwość świadomego wyłączenia C2B i włączenia P2P
- non-negative constraint na p2p_lookup_fee
- precyzję 4 miejsc po przecinku w p2p_lookup_fee (mikropłatności za lookup)
"""

from decimal import Decimal

import pytest
from django.db import IntegrityError, transaction

from banks.models import Bank


@pytest.mark.django_db
class TestBankModuleDefaults:
    """Defaulty: C2B włączony, P2P wyłączony, fee = 0."""

    def test_c2b_enabled_by_default(self, db):
        bank = Bank(name='Default C2B Bank', zone='PL', currency='PLN')
        bank.rotate_api_key()
        bank.save()
        bank.refresh_from_db()
        assert bank.c2b_enabled is True

    def test_p2p_disabled_by_default(self, db):
        bank = Bank(name='Default P2P Bank', zone='PL', currency='PLN')
        bank.rotate_api_key()
        bank.save()
        bank.refresh_from_db()
        assert bank.p2p_enabled is False

    def test_p2p_lookup_fee_default_is_zero(self, db):
        bank = Bank(name='Default Fee Bank', zone='PL', currency='PLN')
        bank.rotate_api_key()
        bank.save()
        bank.refresh_from_db()
        assert bank.p2p_lookup_fee == Decimal('0')

    def test_make_bank_fixture_uses_module_defaults(self, make_bank):
        """Sanity check: standardowy fixture z conftest też ma sensowne defaulty."""
        bank, _ = make_bank()
        assert bank.c2b_enabled is True
        assert bank.p2p_enabled is False
        assert bank.p2p_lookup_fee == Decimal('0')


@pytest.mark.django_db
class TestBankModuleToggles:
    """Świadome przełączanie flag — bank P2P-only, bank z opłatą."""

    def test_can_create_p2p_only_bank(self, db):
        """Bank P2P-only: C2B wyłączony, P2P włączony. Edge-case dla nowych
        graczy wchodzących tylko w przelewy między aliasami."""
        bank = Bank(
            name='P2P Only Bank',
            zone='PL',
            currency='PLN',
            c2b_enabled=False,
            p2p_enabled=True,
        )
        bank.rotate_api_key()
        bank.save()
        bank.refresh_from_db()
        assert bank.c2b_enabled is False
        assert bank.p2p_enabled is True

    def test_can_set_p2p_lookup_fee(self, db):
        """4 miejsca po przecinku — mikropłatność (np. 0.0050 PLN za lookup)."""
        bank = Bank(
            name='Paid Lookup Bank',
            zone='PL',
            currency='PLN',
            p2p_enabled=True,
            p2p_lookup_fee=Decimal('0.0050'),
        )
        bank.rotate_api_key()
        bank.save()
        bank.refresh_from_db()
        assert bank.p2p_lookup_fee == Decimal('0.0050')


@pytest.mark.django_db
class TestP2pLookupFeeConstraint:
    """DB constraint blokuje ujemne fee — analogicznie do debt_limit."""

    def test_negative_lookup_fee_rejected(self, db):
        bank = Bank(
            name='Negative Fee Bank',
            zone='PL',
            currency='PLN',
            p2p_lookup_fee=Decimal('-0.0001'),
        )
        bank.rotate_api_key()
        with pytest.raises(IntegrityError), transaction.atomic():
            bank.save()

    def test_zero_lookup_fee_allowed(self, db):
        bank = Bank(
            name='Zero Fee Bank',
            zone='PL',
            currency='PLN',
            p2p_lookup_fee=Decimal('0'),
        )
        bank.rotate_api_key()
        bank.save()  # nie powinno rzucić
        bank.refresh_from_db()
        assert bank.p2p_lookup_fee == Decimal('0')
