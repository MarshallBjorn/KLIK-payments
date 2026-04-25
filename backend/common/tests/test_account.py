"""Testy validate_account_identifier."""

import pytest
from django.core.exceptions import ValidationError

from common.account import format_account_identifier, validate_account_identifier
from common.enums import Zone


class TestIBANValidation:
    def test_valid_pl_iban(self):
        validate_account_identifier(
            {'type': 'iban', 'value': 'PL61109010140000071219812874'},
            Zone.PL,
        )

    def test_valid_uk_iban(self):
        validate_account_identifier(
            {'type': 'iban', 'value': 'GB82WEST12345698765432'},
            Zone.UK,
        )

    def test_valid_eu_iban(self):
        # EU akceptuje IBAN dowolnego kraju europejskiego
        validate_account_identifier(
            {'type': 'iban', 'value': 'DE89370400440532013000'},
            Zone.EU,
        )

    def test_pl_iban_must_start_with_pl(self):
        with pytest.raises(ValidationError, match='PL'):
            validate_account_identifier(
                {'type': 'iban', 'value': 'GB82WEST12345698765432'},
                Zone.PL,
            )

    def test_uk_iban_must_start_with_gb(self):
        with pytest.raises(ValidationError, match='GB'):
            validate_account_identifier(
                {'type': 'iban', 'value': 'PL61109010140000071219812874'},
                Zone.UK,
            )

    def test_invalid_iban_format(self):
        with pytest.raises(ValidationError, match='format IBAN'):
            validate_account_identifier(
                {'type': 'iban', 'value': 'NOT_AN_IBAN'},
                Zone.PL,
            )

    def test_iban_with_spaces_normalized(self):
        # IBAN ze spacjami też powinien przejść (są normalizowane)
        validate_account_identifier(
            {'type': 'iban', 'value': 'PL61 1090 1014 0000 0712 1981 2874'},
            Zone.PL,
        )

    def test_missing_value(self):
        with pytest.raises(ValidationError, match='"value"'):
            validate_account_identifier(
                {'type': 'iban'},
                Zone.PL,
            )


class TestUSRoutingValidation:
    def test_valid_us_routing(self):
        validate_account_identifier(
            {
                'type': 'us_routing',
                'routing_number': '021000021',
                'account_number': '1234567890',
            },
            Zone.US,
        )

    def test_routing_number_must_be_9_digits(self):
        with pytest.raises(ValidationError, match='routing_number'):
            validate_account_identifier(
                {
                    'type': 'us_routing',
                    'routing_number': '12345',  # za krótki
                    'account_number': '1234567890',
                },
                Zone.US,
            )

    def test_account_number_required(self):
        with pytest.raises(ValidationError, match='account_number'):
            validate_account_identifier(
                {
                    'type': 'us_routing',
                    'routing_number': '021000021',
                },
                Zone.US,
            )

    def test_routing_number_only_digits(self):
        with pytest.raises(ValidationError):
            validate_account_identifier(
                {
                    'type': 'us_routing',
                    'routing_number': 'ABC123456',  # pragma: allowlist secret
                    'account_number': '1234567890',
                },
                Zone.US,
            )


class TestZoneTypeConsistency:
    def test_pl_with_us_routing_fails(self):
        with pytest.raises(ValidationError, match='Strefa PL'):
            validate_account_identifier(
                {
                    'type': 'us_routing',
                    'routing_number': '021000021',
                    'account_number': '1234567890',
                },
                Zone.PL,
            )

    def test_us_with_iban_fails(self):
        with pytest.raises(ValidationError, match='Strefa US'):
            validate_account_identifier(
                {'type': 'iban', 'value': 'PL61109010140000071219812874'},
                Zone.US,
            )

    def test_missing_type_field(self):
        with pytest.raises(ValidationError, match='"type"'):
            validate_account_identifier({}, Zone.PL)

    def test_not_a_dict_fails(self):
        with pytest.raises(ValidationError):
            validate_account_identifier('PL61109010140000071219812874', Zone.PL)


class TestFormatAccountIdentifier:
    def test_format_iban(self):
        result = format_account_identifier(
            {'type': 'iban', 'value': 'PL61109010140000071219812874'}
        )
        assert result == 'PL61109010140000071219812874'

    def test_format_us_routing(self):
        result = format_account_identifier(
            {
                'type': 'us_routing',
                'routing_number': '021000021',
                'account_number': '1234567890',
            }
        )
        assert result == '021000021/1234567890'
