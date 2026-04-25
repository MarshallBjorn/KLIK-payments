"""
Walidacja i utility dla account_identifier — strukturalnego identyfikatora konta
bankowego, różnego per strefa.

Schemat:
- IBAN (PL/EU/UK):  {"type": "iban", "value": "PL61..."}
- US routing (US):  {"type": "us_routing", "routing_number": "021...", "account_number": "1234..."}
"""

import re

from django.core.exceptions import ValidationError

from common.enums import Zone

# Format IBAN: 2 litery kraju + 2 cyfry kontrolne + do 30 znaków alphanumerycznych
# (max długość IBAN = 34, min = 15)
IBAN_REGEX = re.compile(r'^[A-Z]{2}\d{2}[A-Z0-9]{11,30}$')

# Routing number USA: dokładnie 9 cyfr
US_ROUTING_REGEX = re.compile(r'^\d{9}$')

# Account number USA: 4-17 cyfr (bywa różnie)
US_ACCOUNT_REGEX = re.compile(r'^\d{4,17}$')

# Mapowanie strefa → dozwolony typ identyfikatora
ZONE_TO_ACCOUNT_TYPE = {
    Zone.PL: 'iban',
    Zone.EU: 'iban',
    Zone.UK: 'iban',
    Zone.US: 'us_routing',
}

# Mapowanie strefa → prefix IBAN (dla walidacji że IBAN PL zaczyna się od PL itd.)
ZONE_TO_IBAN_PREFIX = {
    Zone.PL: 'PL',
    Zone.UK: 'GB',  # UK używa GB w IBAN
    # EU jest specjalna — wiele krajów. Walidujemy tylko że to *jakiś* IBAN europejski.
}


def validate_account_identifier(account_identifier: dict, zone: str) -> None:
    """
    Waliduje strukturę account_identifier dla danej strefy.

    Raises:
        ValidationError: jeśli JSON niezgodny ze schematem dla strefy.
    """
    if not isinstance(account_identifier, dict):
        raise ValidationError('account_identifier musi być obiektem JSON.')

    account_type = account_identifier.get('type')
    if not account_type:
        raise ValidationError('account_identifier musi zawierać pole "type".')

    expected_type = ZONE_TO_ACCOUNT_TYPE.get(zone)
    if expected_type is None:
        raise ValidationError(f'Nieznana strefa: {zone}.')

    if account_type != expected_type:
        raise ValidationError(
            f'Strefa {zone} wymaga account_identifier typu "{expected_type}", '
            f'otrzymano "{account_type}".'
        )

    if account_type == 'iban':
        _validate_iban(account_identifier, zone)
    elif account_type == 'us_routing':
        _validate_us_routing(account_identifier)


def _validate_iban(data: dict, zone: str) -> None:
    """Walidacja struktury IBAN."""
    value = data.get('value')
    if not value:
        raise ValidationError('IBAN account_identifier musi zawierać pole "value".')

    if not isinstance(value, str):
        raise ValidationError('IBAN value musi być stringiem.')

    value_normalized = value.replace(' ', '').upper()

    if not IBAN_REGEX.match(value_normalized):
        raise ValidationError(f'Niepoprawny format IBAN: {value}.')

    # Sprawdzenie prefixu krajowego (jeśli zdefiniowany dla strefy)
    expected_prefix = ZONE_TO_IBAN_PREFIX.get(zone)
    if expected_prefix and not value_normalized.startswith(expected_prefix):
        raise ValidationError(
            f'IBAN dla strefy {zone} musi zaczynać się od "{expected_prefix}", '
            f'otrzymano: {value_normalized[:2]}.'
        )


def _validate_us_routing(data: dict) -> None:
    """Walidacja struktury US routing + account number."""
    routing = data.get('routing_number')
    account = data.get('account_number')

    if not routing:
        raise ValidationError('US account_identifier musi zawierać "routing_number".')
    if not account:
        raise ValidationError('US account_identifier musi zawierać "account_number".')

    if not isinstance(routing, str) or not US_ROUTING_REGEX.match(routing):
        raise ValidationError(f'Niepoprawny routing_number: {routing}. Wymagane 9 cyfr.')

    if not isinstance(account, str) or not US_ACCOUNT_REGEX.match(account):
        raise ValidationError(f'Niepoprawny account_number: {account}. Wymagane 4-17 cyfr.')


def format_account_identifier(account_identifier: dict) -> str:
    """
    Zwraca human-readable string dla account_identifier — przydatne w admin/logach.
    """
    if account_identifier.get('type') == 'iban':
        return account_identifier['value']
    if account_identifier.get('type') == 'us_routing':
        routing = account_identifier['routing_number']
        account = account_identifier['account_number']
        return f'{routing}/{account}'
    return str(account_identifier)
