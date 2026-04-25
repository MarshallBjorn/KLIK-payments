"""Testy modelu Merchant."""

from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError

from banks.models import Bank
from common.enums import Zone
from merchants.models import Merchant

PL_IBAN = {'type': 'iban', 'value': 'PL61109010140000071219812874'}
GB_IBAN = {'type': 'iban', 'value': 'GB82WEST12345698765432'}
US_ACCOUNT = {
    'type': 'us_routing',
    'routing_number': '021000021',
    'account_number': '1234567890',
}


@pytest.fixture
def bank_pl(db):
    return Bank.objects.create(
        name='Bank PL',
        api_key_hash='dummy_hash_bank_pl',  # pragma: allowlist secret
        zone=Zone.PL,
        currency='PLN',
        debt_limit=Decimal('1000000.00'),
        webhook_url='https://bank-pl.example.com/webhook',
    )


@pytest.fixture
def bank_uk(db):
    return Bank.objects.create(
        name='Bank UK',
        api_key_hash='dummy_hash_bank_uk',  # pragma: allowlist secret
        zone=Zone.UK,
        currency='GBP',
        debt_limit=Decimal('1000000.00'),
        webhook_url='https://bank-uk.example.com/webhook',
    )


@pytest.fixture
def bank_us(db):
    return Bank.objects.create(
        name='Bank US',
        api_key_hash='dummy_hash_bank_us',  # pragma: allowlist secret
        zone=Zone.US,
        currency='USD',
        debt_limit=Decimal('1000000.00'),
        webhook_url='https://bank-us.example.com/webhook',
    )


@pytest.mark.django_db
class TestMerchantModel:
    def test_create_merchant(self, bank_pl):
        merchant = Merchant.objects.create(
            name='Sklep Żabka',
            settlement_bank=bank_pl,
            account_identifier=PL_IBAN,
            zone=Zone.PL,
        )
        assert merchant.id is not None
        assert merchant.active is True

    def test_create_us_merchant(self, bank_us):
        merchant = Merchant.objects.create(
            name='US Store Inc.',
            settlement_bank=bank_us,
            account_identifier=US_ACCOUNT,
            zone=Zone.US,
        )
        assert merchant.id is not None

    def test_zone_must_match_settlement_bank_zone(self, bank_uk):
        merchant = Merchant(
            name='Mismatch Merchant',
            settlement_bank=bank_uk,
            account_identifier=GB_IBAN,
            zone=Zone.PL,
        )
        with pytest.raises(ValidationError, match='Strefa merchanta'):
            merchant.save()

    def test_account_identifier_must_match_zone(self, bank_pl):
        merchant = Merchant(
            name='Bad Identifier',
            settlement_bank=bank_pl,
            account_identifier=US_ACCOUNT,
            zone=Zone.PL,
        )
        with pytest.raises(ValidationError, match='Strefa PL'):
            merchant.save()

    def test_invalid_iban_in_account_identifier(self, bank_pl):
        merchant = Merchant(
            name='Invalid IBAN',
            settlement_bank=bank_pl,
            account_identifier={'type': 'iban', 'value': 'NOT_AN_IBAN'},
            zone=Zone.PL,
        )
        with pytest.raises(ValidationError):
            merchant.save()

    def test_str_representation(self, bank_pl):
        merchant = Merchant.objects.create(
            name='Sklep ABC',
            settlement_bank=bank_pl,
            account_identifier=PL_IBAN,
            zone=Zone.PL,
        )
        assert str(merchant) == 'Sklep ABC (PL)'
