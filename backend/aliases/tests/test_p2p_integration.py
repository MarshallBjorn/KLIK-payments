"""Testy integracyjne P2P — pełny łańcuch przez realne endpointy + task naliczania.

Istniejące testy pokrywają:
- aliases/tests/test_views.py — view-level kontrakt (errory, statusy)
- aliases/tests/test_alias_service.py — AliasService w izolacji
- ledger/tests/test_tasks.py::TestAccrueP2PLookupFees — task z ręcznie
  ustawionym counterem w Redisie

Czego brakuje (luka, którą zamykamy tutaj): SPÓJNOŚĆ łańcucha

    bank A: POST /aliases/register
        ↓
    bank B: GET  /aliases/lookup/{phone}   ×N    → counter w Redis ++
        ↓
    Celery: accrue_p2p_lookup_fees(today)        → LedgerEntry
        ↓
    Redis: counter wyzerowany

Bez tego regresje w kluczu counter-a, polityce inkrementu (np. inkrement na
404), lub szczegółach taska ledger-owego nie byłyby wychwycone — bo każdy
poziom jest testowany osobno z mockiem styku.

Granica: realny Postgres + Redis + Celery eager. Brak mocków HTTP — wszystko
przez DRF APIClient.
"""

from decimal import Decimal

import pytest
from django.utils import timezone
from django_redis import get_redis_connection
from rest_framework import status

from aliases.models import Alias
from aliases.services.alias_service import LOOKUP_COUNTER_PREFIX
from ledger.enums import LedgerEntryType
from ledger.models import LedgerEntry
from ledger.tasks import accrue_p2p_lookup_fees

PL_IBAN = {'type': 'iban', 'value': 'PL61109010140000071219812874'}

REGISTER_URL = '/api/v1/aliases/register'


def _lookup_url(phone: str) -> str:
    return f'/api/v1/aliases/lookup/{phone}'


def _delete_url(phone: str) -> str:
    return f'/api/v1/aliases/{phone}'


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def celery_eager(settings):
    settings.CELERY_TASK_ALWAYS_EAGER = True
    settings.CELERY_TASK_EAGER_PROPAGATES = True
    yield


@pytest.fixture
def bank_a_owner(make_p2p_bank):
    """Bank-właściciel aliasu (rejestruje phone klienta)."""
    bank, key = make_p2p_bank(name='Bank A (owner)')
    return bank, key


@pytest.fixture
def bank_b_querier(make_p2p_bank):
    """Bank pytający (lookup-uje aliasy do routingu P2P). Ma p2p_lookup_fee > 0."""
    bank, key = make_p2p_bank(name='Bank B (querier)')
    bank.p2p_lookup_fee = Decimal('0.05')  # 5 groszy za lookup
    bank.save()
    return bank, key


def _client_for(api_client, key):
    """Zwraca APIClient z nagłówkiem auth dla danego banku."""
    api_client.credentials(HTTP_X_KLIK_BANK_API_KEY=key)
    return api_client


# ---------------------------------------------------------------------------
# Pełny łańcuch P2P: register → lookup ×N → task → LedgerEntry
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.django_db
class TestP2PFullFeeAccrualChain:
    def test_lookups_accumulate_then_accrue_creates_ledger_entry(
        self, api_client, bank_a_owner, bank_b_querier
    ):
        """Pełen cykl dnia: A rejestruje → B pyta ×3 → task → LedgerEntry."""
        bank_a, key_a = bank_a_owner
        bank_b, key_b = bank_b_querier
        phone = '+48501234567'

        # 1. Bank A rejestruje alias swojego klienta
        client_a = _client_for(api_client, key_a)
        reg = client_a.post(
            REGISTER_URL,
            {'phone': phone, 'iban': PL_IBAN['value'], 'zone': 'PL'},
            format='json',
        )
        assert reg.status_code == status.HTTP_201_CREATED
        assert Alias.objects.filter(phone=phone, bank=bank_a).exists()

        # 2. Bank B robi 3 lookupy (każdy zwraca 200 i inkrementuje counter)
        # Trzeba zmienić credentials na bank B
        client_b = _client_for(api_client, key_b)
        for _ in range(3):
            r = client_b.get(_lookup_url(phone))
            assert r.status_code == status.HTTP_200_OK
            # Lookup zwraca routing dla banku-nadawcy:
            assert r.data['bank_id'] == str(bank_a.id)
            assert r.data['iban'] == PL_IBAN['value']

        # Counter w Redisie odzwierciedla 3 lookupy banku B (a NIE banku A)
        today = timezone.now().date().strftime('%Y%m%d')
        redis = get_redis_connection('default')
        b_key = f'{LOOKUP_COUNTER_PREFIX}:{bank_b.id}:{today}'
        a_key = f'{LOOKUP_COUNTER_PREFIX}:{bank_a.id}:{today}'
        assert int(redis.get(b_key)) == 3
        assert redis.get(a_key) is None  # właściciel aliasu nie był pytającym

        # 3. Task naliczania
        today_date = timezone.now().date()
        result = accrue_p2p_lookup_fees.apply(args=[today_date.isoformat()]).get()
        assert result['status'] == 'OK'
        assert result['entries_created'] == 1

        # 4. LedgerEntry: 3 × 0.05 = 0.15, P2P_LOOKUP_FEE, na banku B
        entry = LedgerEntry.objects.get(entry_type=LedgerEntryType.P2P_LOOKUP_FEE)
        assert entry.from_bank_id == bank_b.id
        assert entry.amount == Decimal('0.15')
        assert entry.currency == bank_b.currency
        assert entry.zone == bank_b.zone
        # source_ref pozwala na idempotency drugiego runu
        assert entry.source_ref == f'p2p:{bank_b.id}:{today}'

        # 5. Counter wyczyszczony (zużyty)
        assert redis.get(b_key) is None

    def test_404_lookup_does_not_increment_counter(self, api_client, bank_b_querier):
        """Miss (alias nie istnieje) NIE inkrementuje counter-a → brak naliczenia."""
        bank_b, key_b = bank_b_querier
        client_b = _client_for(api_client, key_b)

        r = client_b.get(_lookup_url('+48500000001'))
        assert r.status_code == status.HTTP_404_NOT_FOUND

        # Counter nie powinien istnieć
        today = timezone.now().date().strftime('%Y%m%d')
        redis = get_redis_connection('default')
        assert redis.get(f'{LOOKUP_COUNTER_PREFIX}:{bank_b.id}:{today}') is None

        # Task: brak counterów → brak entries
        result = accrue_p2p_lookup_fees.apply(args=[timezone.now().date().isoformat()]).get()
        assert result['entries_created'] == 0
        assert LedgerEntry.objects.filter(entry_type=LedgerEntryType.P2P_LOOKUP_FEE).count() == 0

    def test_register_then_delete_makes_lookup_404(self, api_client, bank_a_owner, bank_b_querier):
        """Pełen cykl życia aliasu: register → delete → lookup zwraca 404."""
        bank_a, key_a = bank_a_owner
        bank_b, key_b = bank_b_querier
        phone = '+48501234568'

        client_a = _client_for(api_client, key_a)
        reg = client_a.post(
            REGISTER_URL,
            {'phone': phone, 'iban': PL_IBAN['value'], 'zone': 'PL'},
            format='json',
        )
        assert reg.status_code == status.HTTP_201_CREATED

        # Lookup banku B — alias istnieje
        client_b = _client_for(api_client, key_b)
        assert client_b.get(_lookup_url(phone)).status_code == status.HTTP_200_OK

        # Bank A (właściciel) usuwa alias
        client_a = _client_for(api_client, key_a)
        delete_resp = client_a.delete(_delete_url(phone))
        assert delete_resp.status_code == status.HTTP_204_NO_CONTENT

        # Lookup banku B — teraz 404, counter NIE powinien się dalej zwiększać
        client_b = _client_for(api_client, key_b)
        before = _counter_for(bank_b.id)
        assert client_b.get(_lookup_url(phone)).status_code == status.HTTP_404_NOT_FOUND
        assert _counter_for(bank_b.id) == before

    def test_idempotent_double_accrual_keeps_single_entry(
        self, api_client, bank_a_owner, bank_b_querier
    ):
        """Second run dla tej samej daty NIE tworzy duplikatu LedgerEntry."""
        bank_a, key_a = bank_a_owner
        bank_b, key_b = bank_b_querier
        phone = '+48501234569'

        client_a = _client_for(api_client, key_a)
        client_a.post(
            REGISTER_URL,
            {'phone': phone, 'iban': PL_IBAN['value'], 'zone': 'PL'},
            format='json',
        )

        client_b = _client_for(api_client, key_b)
        for _ in range(2):
            client_b.get(_lookup_url(phone))

        today = timezone.now().date()
        accrue_p2p_lookup_fees.apply(args=[today.isoformat()]).get()

        # Symulujemy że counter "wraca" (np. spóźnione lookupy po pierwszym run)
        # — task musi pozostać idempotentny po source_ref.
        redis = get_redis_connection('default')
        redis.set(f'{LOOKUP_COUNTER_PREFIX}:{bank_b.id}:{today.strftime("%Y%m%d")}', 5)

        second = accrue_p2p_lookup_fees.apply(args=[today.isoformat()]).get()
        assert second['entries_created'] == 0
        assert (
            LedgerEntry.objects.filter(
                from_bank=bank_b,
                entry_type=LedgerEntryType.P2P_LOOKUP_FEE,
            ).count()
            == 1
        )


# ---------------------------------------------------------------------------
# Cross-zone lookup — bank UK pyta o alias PL
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.django_db
class TestP2PCrossZoneLookup:
    def test_uk_bank_can_lookup_pl_alias_and_counts_in_uk(
        self, api_client, bank_a_owner, make_p2p_bank
    ):
        """Bank UK lookup-uje PL alias → counter w UK (na pytającego)."""
        bank_a, key_a = bank_a_owner  # PL
        bank_uk, key_uk = make_p2p_bank(name='UK Querier', zone='UK', currency='GBP')
        bank_uk.p2p_lookup_fee = Decimal('0.10')
        bank_uk.save()
        phone = '+48501234570'

        # PL bank rejestruje
        _client_for(api_client, key_a).post(
            REGISTER_URL,
            {'phone': phone, 'iban': PL_IBAN['value'], 'zone': 'PL'},
            format='json',
        )

        # UK bank pyta
        r = _client_for(api_client, key_uk).get(_lookup_url(phone))
        assert r.status_code == status.HTTP_200_OK
        assert r.data['bank_id'] == str(bank_a.id)

        # Counter inkrementowany dla UK banku (pytającego), nie PL (właściciela)
        assert _counter_for(bank_uk.id) == 1
        assert _counter_for(bank_a.id) == 0

        # Naliczenie: w strefie UK, w GBP
        today = timezone.now().date()
        accrue_p2p_lookup_fees.apply(args=[today.isoformat()]).get()
        entry = LedgerEntry.objects.get(
            entry_type=LedgerEntryType.P2P_LOOKUP_FEE, from_bank=bank_uk
        )
        assert entry.zone == 'UK'
        assert entry.currency == 'GBP'
        assert entry.amount == Decimal('0.10')


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _counter_for(bank_id) -> int:
    """Odczyt aktualnego dziennego countera lookupów dla banku."""
    today = timezone.now().date().strftime('%Y%m%d')
    redis = get_redis_connection('default')
    raw = redis.get(f'{LOOKUP_COUNTER_PREFIX}:{bank_id}:{today}')
    return int(raw) if raw is not None else 0
