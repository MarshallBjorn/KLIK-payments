"""Testy AliasService — czysty service, bez DRF/HTTP."""

from datetime import UTC, datetime

import pytest

from aliases.models import Alias
from aliases.services import AliasService
from aliases.services.alias_service import LOOKUP_COUNTER_PREFIX
from aliases.services.exceptions import (
    AliasAlreadyRegisteredError,
    AliasDoesNotExistError,
    AliasOwnershipError,
    AliasZoneMismatchError,
)
from aliases.tests.conftest import PL_IBAN


@pytest.fixture
def service():
    return AliasService()


@pytest.mark.django_db
class TestRegister:
    def test_creates_alias_with_correct_fields(self, service, bank_pl_p2p):
        bank, _ = bank_pl_p2p
        alias = service.register(
            bank=bank,
            phone="+48501234567",
            account_identifier=PL_IBAN,
            zone="PL",
        )

        assert alias.id is not None
        assert alias.phone == "+48501234567"
        assert alias.bank_id == bank.id
        assert alias.zone == "PL"
        assert alias.account_identifier == PL_IBAN

        # I rzeczywiście jest w DB
        assert Alias.objects.filter(pk=alias.pk).exists()

    def test_duplicate_phone_raises(self, service, bank_pl_p2p):
        bank, _ = bank_pl_p2p
        service.register(
            bank=bank,
            phone="+48501234567",
            account_identifier=PL_IBAN,
            zone="PL",
        )

        with pytest.raises(AliasAlreadyRegisteredError) as exc:
            service.register(
                bank=bank,
                phone="+48501234567",
                account_identifier=PL_IBAN,
                zone="PL",
            )
        assert exc.value.phone == "+48501234567"

    def test_phone_prefix_zone_mismatch_raises(self, service, bank_pl_p2p):
        """+48 wskazuje PL, ale rejestracja w UK → ZoneMismatch."""
        # Bierzemy bank PL ale próbujemy zone='UK' — najpierw rąbnie nas
        # walidacja zone↔bank.zone, ale message i tak będzie pod kluczem 'zone'.
        bank, _ = bank_pl_p2p
        with pytest.raises(AliasZoneMismatchError):
            service.register(
                bank=bank,
                phone="+48501234567",
                account_identifier=PL_IBAN,
                zone="UK",
            )

    def test_phone_zone_vs_bank_zone_mismatch_raises(self, service, bank_uk_p2p):
        """Bank UK próbuje rejestrować numer PL — ZoneMismatch."""
        bank_uk, _ = bank_uk_p2p
        with pytest.raises(AliasZoneMismatchError):
            service.register(
                bank=bank_uk,
                phone="+48501234567",
                account_identifier=PL_IBAN,
                zone="PL",
            )


@pytest.mark.django_db
class TestLookupForBank:
    def test_returns_alias_when_found(self, service, bank_pl_p2p):
        bank, _ = bank_pl_p2p
        service.register(
            bank=bank,
            phone="+48501234567",
            account_identifier=PL_IBAN,
            zone="PL",
        )

        result = service.lookup_for_bank(querying_bank=bank, phone="+48501234567")

        assert result.phone == "+48501234567"
        assert result.bank_id == bank.id

    def test_missing_phone_raises(self, service, bank_pl_p2p):
        bank, _ = bank_pl_p2p
        with pytest.raises(AliasDoesNotExistError) as exc:
            service.lookup_for_bank(querying_bank=bank, phone="+48999999999")
        assert exc.value.phone == "+48999999999"

    def test_increments_counter_on_success(self, service, bank_pl_p2p, make_p2p_bank):
        # Rejestracja na bank A, lookup z banku B — to typowy P2P use case
        bank_a, _ = bank_pl_p2p
        bank_b, _ = make_p2p_bank(name="Querying Bank PL")

        service.register(
            bank=bank_a,
            phone="+48501234567",
            account_identifier=PL_IBAN,
            zone="PL",
        )

        # Pre-condition: counter dla bank_b = 0
        assert service.get_lookup_count(bank_b.id) == 0

        service.lookup_for_bank(querying_bank=bank_b, phone="+48501234567")

        # Counter total inkrementowany dla pytającego (bank_b), nie dla
        # właściciela aliasu (bank_a).
        assert service.get_lookup_count(bank_b.id) == 1
        assert service.get_lookup_count(bank_a.id) == 0

    def test_counter_increments_with_each_call(self, service, bank_pl_p2p):
        bank, _ = bank_pl_p2p
        service.register(
            bank=bank,
            phone="+48501234567",
            account_identifier=PL_IBAN,
            zone="PL",
        )

        for expected in range(1, 4):
            service.lookup_for_bank(querying_bank=bank, phone="+48501234567")
            assert service.get_lookup_count(bank.id) == expected

    def test_counter_not_incremented_on_miss(self, service, bank_pl_p2p):
        """Miss (404) NIE inkrementuje counter — bank nie płaci za nieudane lookup."""
        bank, _ = bank_pl_p2p

        with pytest.raises(AliasDoesNotExistError):
            service.lookup_for_bank(querying_bank=bank, phone="+48999999999")

        assert service.get_lookup_count(bank.id) == 0

    def test_daily_counter_set_with_today_key(self, service, bank_pl_p2p):
        bank, _ = bank_pl_p2p
        service.register(
            bank=bank,
            phone="+48501234567",
            account_identifier=PL_IBAN,
            zone="PL",
        )
        service.lookup_for_bank(querying_bank=bank, phone="+48501234567")

        today = datetime.now(UTC).strftime("%Y%m%d")
        assert service.get_lookup_count(bank.id, day=today) == 1

    def test_daily_counter_has_ttl(self, service, bank_pl_p2p):
        """Sanity check: dzienny klucz ma ustawiony TTL (nie wisi w nieskończoność)."""
        bank, _ = bank_pl_p2p
        service.register(
            bank=bank,
            phone="+48501234567",
            account_identifier=PL_IBAN,
            zone="PL",
        )
        service.lookup_for_bank(querying_bank=bank, phone="+48501234567")

        today = datetime.now(UTC).strftime("%Y%m%d")
        key = f"{LOOKUP_COUNTER_PREFIX}:{bank.id}:{today}"
        ttl = service._redis.ttl(key)
        # 35 dni z buforem — dokładna wartość nieistotna, byle TTL > 0 i sensowny
        assert ttl > 0
        assert ttl <= 35 * 24 * 60 * 60

    def test_total_counter_has_no_ttl(self, service, bank_pl_p2p):
        """Total counter żyje wiecznie — TTL = -1 w Redisie."""
        bank, _ = bank_pl_p2p
        service.register(
            bank=bank,
            phone="+48501234567",
            account_identifier=PL_IBAN,
            zone="PL",
        )
        service.lookup_for_bank(querying_bank=bank, phone="+48501234567")

        key = f"{LOOKUP_COUNTER_PREFIX}:{bank.id}:total"
        # -1 = brak TTL w konwencji Redisa
        assert service._redis.ttl(key) == -1


@pytest.mark.django_db
class TestUnregister:
    def test_owner_can_unregister(self, service, bank_pl_p2p):
        bank, _ = bank_pl_p2p
        alias = service.register(
            bank=bank,
            phone="+48501234567",
            account_identifier=PL_IBAN,
            zone="PL",
        )

        service.unregister(bank=bank, phone="+48501234567")

        assert not Alias.objects.filter(pk=alias.pk).exists()

    def test_missing_phone_raises(self, service, bank_pl_p2p):
        bank, _ = bank_pl_p2p
        with pytest.raises(AliasDoesNotExistError):
            service.unregister(bank=bank, phone="+48999999999")

    def test_other_bank_cannot_unregister(self, service, bank_pl_p2p, make_p2p_bank):
        owner_bank, _ = bank_pl_p2p
        other_bank, _ = make_p2p_bank(name="Sneaky Bank")

        service.register(
            bank=owner_bank,
            phone="+48501234567",
            account_identifier=PL_IBAN,
            zone="PL",
        )

        with pytest.raises(AliasOwnershipError) as exc:
            service.unregister(bank=other_bank, phone="+48501234567")

        assert exc.value.phone == "+48501234567"
        assert exc.value.bank_id == other_bank.id

        # Alias nie został usunięty
        assert Alias.objects.filter(phone="+48501234567").exists()
