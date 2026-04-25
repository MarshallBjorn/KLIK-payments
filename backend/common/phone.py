"""
Walidacja numerów telefonów w formacie E.164 i mapowanie prefiksów krajowych
na strefy KLIK.

Używane przez `aliases.Alias` do walidacji że strefa zarejestrowanego numeru
jest spójna z prefiksem (np. `+48` musi być rejestrowane jako PL).

E.164 (ITU-T): `+` + 1-3 cyfry kodu kraju + numer narodowy. Razem max 15 cyfr.
"""

import re

from django.core.exceptions import ValidationError

from common.enums import Zone

# E.164: + i 8-15 cyfr (bez spacji, myślników, nawiasów).
# Minimum 8 cyfr żeby odrzucić oczywiste śmieci typu "+48".
E164_REGEX = re.compile(r'^\+[1-9]\d{7,14}$')

# Mapowanie prefiks → strefa. Lista prefiksów dla EU jest celowo niepełna —
# obejmuje główne kraje strefy euro. Dla MVP wystarczy. Strategia lookupu:
# najdłuższy pasujący prefiks wygrywa (sortujemy po długości malejąco).
#
# UK = +44 (Wielka Brytania). US = +1 (Ameryka Płn., w tym Kanada — w MVP
# traktujemy +1 jako US, bo Kanada nie jest w naszych strefach).
PHONE_PREFIX_TO_ZONE: dict[str, Zone] = {
    # PL
    '+48': Zone.PL,
    # UK
    '+44': Zone.UK,
    # US (Kanada też zaczyna się od +1, ale w MVP traktujemy jako US)
    '+1': Zone.US,
    # EU — wybrane kraje strefy euro
    '+49': Zone.EU,  # Niemcy
    '+33': Zone.EU,  # Francja
    '+39': Zone.EU,  # Włochy
    '+34': Zone.EU,  # Hiszpania
    '+31': Zone.EU,  # Holandia
    '+32': Zone.EU,  # Belgia
    '+351': Zone.EU,  # Portugalia
    '+353': Zone.EU,  # Irlandia
    '+43': Zone.EU,  # Austria
    '+358': Zone.EU,  # Finlandia
    '+30': Zone.EU,  # Grecja
}


def validate_e164(phone: str) -> None:
    """Sprawdza że `phone` jest poprawnym numerem E.164.

    Raises:
        ValidationError: jeśli format niezgodny z E.164.
    """
    if not isinstance(phone, str):
        raise ValidationError('phone musi być stringiem.')
    if not E164_REGEX.match(phone):
        raise ValidationError(
            f'Niepoprawny format numeru telefonu: {phone!r}. '
            'Wymagany format E.164 (np. +48501234567).'
        )


def resolve_zone_from_phone(phone: str) -> Zone | None:
    """Zwraca strefę odpowiadającą prefiksowi telefonu, lub None jeśli nieznany.

    Najdłuższy pasujący prefiks wygrywa — dlatego `+351` (Portugalia) musi być
    sprawdzone przed `+3` (gdyby kiedyś istniało).
    """
    for prefix in sorted(PHONE_PREFIX_TO_ZONE, key=len, reverse=True):
        if phone.startswith(prefix):
            return PHONE_PREFIX_TO_ZONE[prefix]
    return None


def validate_phone_matches_zone(phone: str, zone: str) -> None:
    """Waliduje że prefiks telefonu odpowiada deklarowanej strefie.

    Wywoływane przy rejestracji aliasu — gwarantuje że nikt nie zarejestruje
    `+48...` w strefie UK (co by zepsuło routing).

    Raises:
        ValidationError: jeśli format błędny, prefiks nieznany lub nie pasuje
        do `zone`.
    """
    validate_e164(phone)

    expected_zone = resolve_zone_from_phone(phone)
    if expected_zone is None:
        raise ValidationError(
            f"Nie rozpoznano strefy dla numeru {phone}. "
            f'Wspierane prefiksy: {", ".join(sorted(PHONE_PREFIX_TO_ZONE))}.'
        )

    if expected_zone != zone:
        raise ValidationError(
            f'Prefiks numeru {phone} wskazuje strefę {expected_zone}, '
            f'ale rejestracja w strefie {zone}.'
        )
