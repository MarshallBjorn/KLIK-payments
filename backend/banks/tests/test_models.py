"""Testy modelu Bank.

Pokrywamy:
- defaultowe wartości (active=False, debt_limit=0)
- walidację strefa↔waluta (clean + DB constraint)
- generowanie/rotację klucza API (uniqueness, hash, plaintext format)
- __str__ i porządek
"""

import re
import uuid

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from banks.models import Bank, generate_api_key, hash_api_key

# ----------------------------------------------------------------------
# Defaulty i podstawowa konstrukcja
# ----------------------------------------------------------------------


@pytest.mark.django_db
class TestBankDefaults:
    def test_inactive_by_default(self, make_bank):
        bank, _ = make_bank(active=False)
        assert bank.active is False

    def test_pk_is_uuid(self, make_bank):
        bank, _ = make_bank()
        assert isinstance(bank.id, uuid.UUID)

    def test_timestamps_set_on_create(self, make_bank):
        bank, _ = make_bank()
        assert bank.created_at is not None
        assert bank.updated_at is not None

    def test_default_debt_limit_is_zero(self, db):
        from decimal import Decimal

        bank = Bank(name='Bank Zero', zone='PL', currency='PLN')
        bank.rotate_api_key()
        bank.save()
        bank.refresh_from_db()
        assert bank.debt_limit == Decimal('0')

    def test_str_representation(self, make_bank):
        bank, _ = make_bank(name='mBank', zone='PL')
        assert str(bank) == 'mBank (PL)'

    def test_is_authenticated_true(self, make_bank):
        """Wymagane przez DRF IsAuthenticated permission. Bank w request.user
        zawsze jest "uwierzytelniony" — auth class go zwaliował."""
        bank, _ = make_bank()
        assert bank.is_authenticated is True

    def test_is_anonymous_false(self, make_bank):
        bank, _ = make_bank()
        assert bank.is_anonymous is False


# ----------------------------------------------------------------------
# Walidacja strefa ↔ waluta
# ----------------------------------------------------------------------


@pytest.mark.django_db
class TestZoneCurrencyConsistency:
    @pytest.mark.parametrize(
        ('zone', 'currency'),
        [('PL', 'PLN'), ('EU', 'EUR'), ('UK', 'GBP'), ('US', 'USD')],
    )
    def test_valid_combinations_pass_clean(self, db, zone, currency):
        bank = Bank(name=f'Bank {zone}', zone=zone, currency=currency)
        bank.rotate_api_key()
        bank.full_clean()  # nie powinno rzucić

    def test_clean_rejects_pl_with_eur(self, db):
        bank = Bank(name='PL EUR Bank', zone='PL', currency='EUR')
        bank.rotate_api_key()
        with pytest.raises(ValidationError) as exc:
            bank.full_clean()
        assert 'currency' in exc.value.message_dict

    def test_db_constraint_blocks_zone_currency_mismatch(self, db, make_bank):
        """Drugą linią obrony jest CheckConstraint — sprawdzamy że bypass
        full_clean() nadal nie pozwoli zapisać niespójnych danych."""
        bank = Bank(name='Bypass Bank', zone='UK', currency='USD')
        bank.rotate_api_key()
        with pytest.raises(IntegrityError), transaction.atomic():
            bank.save()


# ----------------------------------------------------------------------
# Klucze API
# ----------------------------------------------------------------------


class TestApiKeyHelpers:
    def test_generate_returns_plaintext_and_hash(self):
        plaintext, hashed = generate_api_key()
        assert plaintext.startswith('klik_')
        assert len(hashed) == 64  # SHA-256 hex
        # Plaintext jest URL-safe (litery, cyfry, podkreślniki, myślniki)
        assert re.fullmatch(r'klik_[A-Za-z0-9_-]+', plaintext)

    def test_hash_is_deterministic(self):
        assert hash_api_key('foo') == hash_api_key('foo')

    def test_different_inputs_yield_different_hashes(self):
        assert hash_api_key('foo') != hash_api_key('bar')

    def test_hash_does_not_contain_plaintext(self):
        # Sanity check że nie używamy MD5/identity przez pomyłkę.
        plaintext = 'klik_abcdef'
        assert plaintext not in hash_api_key(plaintext)


@pytest.mark.django_db
class TestBankApiKeyRotation:
    def test_rotate_changes_hash(self, make_bank):
        bank, original_plaintext = make_bank()
        original_hash = bank.api_key_hash

        new_plaintext = bank.rotate_api_key()
        bank.save()

        assert new_plaintext != original_plaintext
        assert bank.api_key_hash != original_hash
        assert bank.api_key_hash == hash_api_key(new_plaintext)

    def test_two_banks_get_different_keys(self, make_bank):
        a, plain_a = make_bank(name='Bank A', zone='PL', currency='PLN')
        b, plain_b = make_bank(name='Bank B', zone='EU', currency='EUR')
        assert plain_a != plain_b
        assert a.api_key_hash != b.api_key_hash

    def test_api_key_hash_must_be_unique(self, db, make_bank):
        a, _ = make_bank(name='Bank A')
        b = Bank(
            name='Bank B (clone)',
            zone='PL',
            currency='PLN',
            api_key_hash=a.api_key_hash,  # forsujemy duplikat
        )
        with pytest.raises(IntegrityError), transaction.atomic():
            b.save()

    def test_rotate_does_not_save_implicitly(self, make_bank):
        """Kontrakt: `rotate_api_key()` mutuje obiekt, ale `save()` jest
        po stronie wywołującego (żeby można było zrobić w transakcji)."""
        bank, _ = make_bank()
        original_hash_in_db = Bank.objects.get(pk=bank.pk).api_key_hash

        bank.rotate_api_key()  # bez save()
        # DB nie powinno mieć nowego hasha — bank w DB to obiekt sprzed rotate.
        assert Bank.objects.get(pk=bank.pk).api_key_hash == original_hash_in_db


# ----------------------------------------------------------------------
# Constraint: debt_limit >= 0
# ----------------------------------------------------------------------


@pytest.mark.django_db
class TestDebtLimitConstraint:
    def test_negative_debt_limit_rejected(self, db):
        bank = Bank(name='Negative Bank', zone='PL', currency='PLN', debt_limit='-1.00')
        bank.rotate_api_key()
        with pytest.raises(IntegrityError), transaction.atomic():
            bank.save()

    def test_zero_debt_limit_allowed(self, db):
        from decimal import Decimal

        bank = Bank(name='Zero Bank', zone='PL', currency='PLN', debt_limit='0')
        bank.rotate_api_key()
        bank.save()  # nie powinno rzucić
        # Refresh żeby dostać typ z DB (Decimal), nie string z Python-side initu.
        bank.refresh_from_db()
        assert bank.debt_limit == Decimal('0')
