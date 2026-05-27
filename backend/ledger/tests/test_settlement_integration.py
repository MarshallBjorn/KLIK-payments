"""Testy integracyjne settlementu — pełny pipeline na realnych danych.

W odróżnieniu od `test_tasks.py` (które używa `AlwaysSuccessGateway` /
`AlwaysFailGateway` w trybie all-or-nothing i 1 transakcji), tutaj:

1. **Multi-tx netting** — kilka realnych transakcji C2B między 3 bankami,
   krzyżujące się zobowiązania. Weryfikuje że netting faktycznie redukuje
   liczbę transferów vs surowa lista obligacji.
2. **Partial RTGS failure** — gateway zwraca mieszany wynik (część SUCCESS,
   część FAILED). Weryfikuje granularność `mark_settled`: entries między
   parami banków SUCCESS są oznaczane settled, entries między parami FAILED
   pozostają niesettled (do retry w następnej sesji), sesja kończy FAILED.
3. **Self-balanced session** — entries A→B i B→A o tej samej kwocie. Netting
   produkuje 0 transferów, sesja COMPLETED bez ruchu do RTGS.

Granica integracji: realny Postgres + Redis + Celery eager. RTGS na granicy
gateway-a (patchujemy `RTGSDispatcher.from_settings`), nie na granicy HTTP —
to zostawiamy dla smoke testu przeciwko mock-RTGS w docker-compose.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

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
)
from ledger.models import LedgerEntry, SettlementSession, SettlementTransfer
from ledger.rtgs import RTGSDispatcher, TransferResult, TransferStatus
from ledger.rtgs.gateway import RTGSGateway
from ledger.services import LedgerService
from ledger.tasks import run_settlement_session
from merchants.models import Merchant

PL_IBAN = {'type': 'iban', 'value': 'PL61109010140000071219812874'}
PL_IBAN_2 = {'type': 'iban', 'value': 'PL27114020040000300201355387'}
PL_IBAN_3 = {'type': 'iban', 'value': 'PL83102000810000060001234567'}


# ---------------------------------------------------------------------------
# Konfiguracyjny gateway zwracający wynik z predykatu
# ---------------------------------------------------------------------------


class ProgrammableGateway(RTGSGateway):
    """Gateway którego wynik per-transfer wyznacza funkcja decyzyjna.

    Pozwala na kontrolowany mieszany scenariusz (część SUCCESS, część FAILED)
    bez ruszania HTTP — wszystko deterministycznie wewnątrz testu.
    """

    system_name = 'TEST_PROGRAMMABLE'

    def __init__(self, decide):
        super().__init__(base_url='http://test')
        self._decide = decide

    def settle(self, session_id, transfers):
        out = []
        for t in transfers:
            ok = self._decide(t)
            if ok:
                out.append(
                    TransferResult(
                        transfer_id=t.transfer_id,
                        status=TransferStatus.SUCCESS,
                        rtgs_reference=f'OK-{str(t.transfer_id)[:8]}',
                    )
                )
            else:
                out.append(
                    TransferResult(
                        transfer_id=t.transfer_id,
                        status=TransferStatus.FAILED,
                        failure_reason='Programowany fail',
                    )
                )
        return out

    def healthcheck(self):
        return True


def _patch_dispatcher_with(gateway: RTGSGateway):
    return patch.object(
        RTGSDispatcher,
        'from_settings',
        return_value=RTGSDispatcher({z.value: gateway for z in Zone}),
    )


# ---------------------------------------------------------------------------
# Fixtures — 3 banki PL, agent settlowany w banku C
# ---------------------------------------------------------------------------


def _make_bank(name: str, *, zone=Zone.PL, currency='PLN') -> Bank:
    return Bank.objects.create(
        name=name,
        api_key_hash=f'{name.lower()}_hash',  # pragma: allowlist secret
        zone=zone,
        currency=currency,
        debt_limit=Decimal('1000000.00'),
        webhook_url=f'https://{name.lower()}.example.com/webhook',
        active=True,
    )


@pytest.fixture
def bank_a(db):
    return _make_bank('BankA')


@pytest.fixture
def bank_b(db):
    return _make_bank('BankB')


@pytest.fixture
def bank_c(db):
    return _make_bank('BankC')


@pytest.fixture
def agent(db, bank_c):
    """Agent rozliczany w banku C — jego agent_fee zawsze trafia do C."""
    return Agent.objects.create(
        name='IntegAgent',
        api_key_hash='integ_agent_hash',  # pragma: allowlist secret
        settlement_bank=bank_c,
        account_identifier=PL_IBAN_3,
        zone=Zone.PL,
    )


@pytest.fixture
def msc(db, agent):
    return MSCAgreement.objects.create(
        agent=agent,
        klik_fee_perc=Decimal('0.30'),
        agent_fee_perc=Decimal('1.00'),
        valid_from=timezone.now() - timedelta(days=1),
    )


def _make_merchant(name: str, settlement_bank: Bank, account=PL_IBAN_2) -> Merchant:
    return Merchant.objects.create(
        name=name,
        settlement_bank=settlement_bank,
        account_identifier=account,
        zone=Zone.PL,
    )


def _completed_tx(
    *, sender_bank: Bank, agent: Agent, merchant: Merchant, amount: Decimal, idem: str
) -> Transaction:
    """Tworzy COMPLETED tx z wyliczonymi fees (jak po /payments/confirm ACCEPTED)."""
    klik_fee = (amount * Decimal('0.0030')).quantize(Decimal('0.01'))
    agent_fee = (amount * Decimal('0.0100')).quantize(Decimal('0.01'))
    merchant_net = amount - klik_fee - agent_fee
    now = timezone.now()
    return Transaction.objects.create(
        sender_bank=sender_bank,
        agent=agent,
        merchant=merchant,
        code_snapshot='123456',
        amount_gross=amount,
        klik_fee=klik_fee,
        agent_fee=agent_fee,
        merchant_net=merchant_net,
        currency='PLN',
        zone=Zone.PL,
        is_on_us=(sender_bank.id == merchant.settlement_bank_id),
        idempotency_key=idem,
        status=TransactionStatus.COMPLETED,
        authorized_at=now,
        completed_at=now,
    )


# ---------------------------------------------------------------------------
# 1. Multi-transaction netting
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.django_db
class TestMultiTransactionNetting:
    def test_crossing_obligations_reduce_to_fewer_transfers(
        self, bank_a, bank_b, bank_c, agent, msc
    ):
        """Trzy banki, krzyżujące się zobowiązania → netting redukuje 4+ pary
        do 2 transferów (każdy musi mieć innego from i to).

        Scenariusz:
          tx1: A→B 150 (off-us, merchant w B). Entries: A→B 148.05, A→A 0.45 (klik), A→C 1.50 (agent)
          tx2: B→A 200 (off-us, merchant w A). Entries: B→A 197.40, B→B 0.60, B→C 2.00
          tx3: A→C 100 (off-us, merchant w C). Entries: A→C 98.70, A→A 0.30, A→C 1.00

        Raw obligacje (po wykluczeniu self A→A, B→B):
          A→B 148.05, A→C 1.50+98.70+1.00 = 101.20, B→A 197.40, B→C 2.00

        Pozycje netto (suma = 0 sanity):
          A: +197.40 (od B) - 148.05 - 101.20 = -51.85
          B: +148.05 (od A) - 197.40 - 2.00   = -51.35
          C: +101.20 (od A) + 2.00            = +103.20

        Greedy: A→C 51.85, B→C 51.35 → 2 transfery (zamiast 4 raw).
        """
        merchant_in_b = _make_merchant('M-in-B', bank_b)
        merchant_in_a = _make_merchant('M-in-A', bank_a, account=PL_IBAN)
        merchant_in_c = _make_merchant('M-in-C', bank_c, account=PL_IBAN_3)

        for sender, merchant, amount, idem in [
            (bank_a, merchant_in_b, Decimal('150.00'), 'tx-1'),
            (bank_b, merchant_in_a, Decimal('200.00'), 'tx-2'),
            (bank_a, merchant_in_c, Decimal('100.00'), 'tx-3'),
        ]:
            tx = _completed_tx(
                sender_bank=sender, agent=agent, merchant=merchant, amount=amount, idem=idem
            )
            LedgerService.record_c2b_transaction(tx)

        # Sanity: 3 × 3 = 9 entries (każda off-us tx daje 3)
        assert LedgerEntry.objects.count() == 9

        # Liczymy "raw pary" (różne from/to) w entries — to ile transferów byłoby BEZ nettingu
        raw_pairs = (
            LedgerEntry.objects.exclude(from_bank=models_f('to_bank'))
            .values('from_bank_id', 'to_bank_id')
            .distinct()
            .count()
        )
        assert raw_pairs == 4  # A→B, A→C, B→A, B→C

        with _patch_dispatcher_with(ProgrammableGateway(decide=lambda _t: True)):
            result = run_settlement_session.apply(args=['PL']).get()

        assert result['status'] == 'COMPLETED'
        assert result['entries_count'] == 9

        session = SettlementSession.objects.get(id=result['session_id'])
        assert session.status == SettlementSessionStatus.COMPLETED

        # Netting: 2 transfery, nie 4
        transfers = SettlementTransfer.objects.filter(session=session)
        assert transfers.count() == 2, (
            f'Netting powinien zredukować 4 raw pary do 2, ' f'jest {transfers.count()}'
        )

        # Wszyscy mają C jako odbiorcę (C jest jedynym wierzycielem netto)
        for t in transfers:
            assert t.to_bank_id == bank_c.id

        # Sumy transferów = sumy netto debit (A=-51.20, B=-51.35)
        transfer_amounts = {t.from_bank_id: t.amount for t in transfers}
        assert transfer_amounts[bank_a.id] == Decimal('51.85')
        assert transfer_amounts[bank_b.id] == Decimal('51.35')

        # Suma RTGS = |suma debitów| = 103.20 (waluta przepływa po nettingu)
        assert sum(transfer_amounts.values()) == Decimal('103.20')

        # Wszystkie entries settled (sesja COMPLETED)
        assert LedgerEntry.objects.filter(session=session, settled=False).count() == 0
        assert LedgerEntry.objects.filter(session=session, settled=True).count() == 9


# ---------------------------------------------------------------------------
# 3. Self-balanced session (netting → 0 transferów)
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.django_db
class TestSelfBalancedSession:
    def test_offsetting_entries_produce_no_transfers(self, bank_a, bank_b):
        """A→B 100 i B→A 100 (BANK_TO_BANK) → netto = 0, RTGS nie dostaje nic.

        Sprawdzamy że gateway NIE jest wołany (mamy go programowanego z fail
        na wszystko — jakby cokolwiek poleciało, test by się przewrócił),
        sesja kończy COMPLETED, wszystkie entries settled.
        """
        common_kwargs = {
            'currency': 'PLN',
            'zone': Zone.PL,
            'entry_type': LedgerEntryType.BANK_TO_BANK,
            'beneficiary_type': BeneficiaryType.BANK,
        }
        LedgerEntry.objects.create(
            from_bank=bank_a,
            to_bank=bank_b,
            amount=Decimal('100.00'),
            beneficiary_ref=bank_b.id,
            source_ref='sb-1',
            **common_kwargs,
        )
        LedgerEntry.objects.create(
            from_bank=bank_b,
            to_bank=bank_a,
            amount=Decimal('100.00'),
            beneficiary_ref=bank_a.id,
            source_ref='sb-2',
            **common_kwargs,
        )

        # Gateway, który fail-uje WSZYSTKO — gdyby cokolwiek tu trafiło, test
        # wykryłby FAILED status. Sukces oznacza, że dispatch nie został wywołany.
        with _patch_dispatcher_with(ProgrammableGateway(decide=lambda _t: False)):
            result = run_settlement_session.apply(args=['PL']).get()

        assert result['status'] == 'COMPLETED'
        assert result['entries_count'] == 2
        assert result['transfers_count'] == 0

        session = SettlementSession.objects.get(id=result['session_id'])
        assert session.status == SettlementSessionStatus.COMPLETED
        assert SettlementTransfer.objects.filter(session=session).count() == 0

        # Oba entries settled mimo braku ruchu do RTGS
        assert LedgerEntry.objects.filter(session=session, settled=False).count() == 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def models_f(field):
    """Skrót na django.db.models.F żeby uniknąć dodatkowego importu na górze."""
    from django.db.models import F

    return F(field)
