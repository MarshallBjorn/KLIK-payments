"""Smoke test integracji KLIK ↔ FedNow (strefa US, USD, ISO 20022 pacs.008).

Wzorowany na chaps_smoke.py i target_smoke.py w tym samym projekcie —
identyczna struktura Command / ScenarioResult / _print_summary.

Scenariusze:
    happy        — 3 banki US, krzyżujące się tx off-us → netting → FedNow;
                   weryfikacja COMPLETED, referencji FEDNOW-*, settled entries
                   i kwot prowizji (KLIK_FEE_C2B + AGENT_FEE).
    unknown-rtn  — transfer do banku z niezarejestrowanym RTN w FedNow
                   → transfer FAILED z powodem zawierającym "Unknown"/"RTN".
    fee-accrual  — jeden tx off-us → 7 asercji o entry'ach prowizji;
                   nie uruchamia settlement, sprawdza tylko księgowanie.

Użycie:
    make shell
    python manage.py fednow_smoke
    python manage.py fednow_smoke --scenario happy
    python manage.py fednow_smoke --scenario unknown-rtn
    python manage.py fednow_smoke --scenario fee-accrual
    python manage.py fednow_smoke --scenario all

Exit codes:  0 = wszystko OK / SKIP,  1 = FAIL,  2 = błąd techniczny
"""

from __future__ import annotations

import sys
import traceback
from dataclasses import dataclass
from decimal import Decimal
from uuid import uuid4

import requests
from django.core.management.base import BaseCommand
from django.utils import timezone

from agents.models import Agent, MSCAgreement
from banks.models import Bank
from codes.enums import TransactionStatus
from codes.models import Transaction
from common.enums import Currency, Zone
from ledger.enums import (
    SettlementSessionStatus,
    SettlementTransferStatus,
)
from ledger.models import LedgerEntry, SettlementSession, SettlementTransfer
from ledger.services import LedgerService
from ledger.tasks import run_settlement_session
from merchants.models import Merchant

# ── RTN-y banków testowych ─────────────────────────────────────────────────
# Muszą być zarejestrowane w bank_details FedNow (BANK0_RTN... w .env FedSystems).
RTN_A     = '021000021'
RTN_B     = '021000022'
RTN_C     = '021000023'
RTN_GHOST = '000000099'   # celowo niezarejestrowany w FedNow

ACCT_A     = '1000000001'
ACCT_B     = '2000000002'
ACCT_C     = '3000000003'
ACCT_GHOST = '9999999999'

# Prowizje MSC — muszą pasować do MSCAgreement tworzonych przez seed
KLIK_PERC  = Decimal('0.30')   # 0.30%
AGENT_PERC = Decimal('1.00')   # 1.00%


def _klik_fee(amount: Decimal) -> Decimal:
    return (amount * KLIK_PERC / 100).quantize(Decimal('0.01'))


def _agent_fee(amount: Decimal) -> Decimal:
    return (amount * AGENT_PERC / 100).quantize(Decimal('0.01'))


@dataclass
class ScenarioResult:
    name: str
    ok: bool
    detail: str = ''
    skipped: bool = False


class Command(BaseCommand):
    help = 'Smoke test integracji KLIK → FedNow (US/USD, pacs.008).'

    def add_arguments(self, parser):
        parser.add_argument(
            '--scenario',
            choices=['happy', 'unknown-rtn', 'fee-accrual', 'all'],
            default='all',
        )

    def handle(self, *_args, **opts):
        scenario = opts['scenario']
        try:
            self._preflight_health()
            self._preflight_no_active_session()
        except Exception as exc:
            self.stderr.write(self.style.ERROR(f'\nABORTED (preflight): {exc}'))
            traceback.print_exc()
            sys.exit(2)

        results: list[ScenarioResult] = []
        try:
            if scenario in ('happy', 'all'):
                results.append(self._scenario_happy())
            if scenario in ('unknown-rtn', 'all'):
                self._preflight_no_active_session()
                results.append(self._scenario_unknown_rtn())
            if scenario in ('fee-accrual', 'all'):
                results.append(self._scenario_fee_accrual())
        except Exception as exc:
            self.stderr.write(self.style.ERROR(f'\nABORTED (runtime): {exc}'))
            traceback.print_exc()
            sys.exit(2)

        self._print_summary(results)
        sys.exit(0 if all(r.ok or r.skipped for r in results) else 1)

    # ──────────────────────────────────────────────────────────────────
    # Pre-flight
    # ──────────────────────────────────────────────────────────────────

    def _fednow_url(self) -> str:
        from django.conf import settings
        return settings.FEDNOW_URL.rstrip('/')

    def _preflight_health(self) -> None:
        url = f'{self._fednow_url()}/health'
        try:
            resp = requests.get(url, timeout=5)
            if resp.status_code != 200:
                raise RuntimeError(f'FedNow /health zwrócił HTTP {resp.status_code}')
        except requests.RequestException as exc:
            raise RuntimeError(
                f'FedNow nieosiągalny ({url}): {exc}\n'
                'Sprawdź: docker compose -f FedSystems/docker-compose.yml ps\n'
                'oraz FEDNOW_URL w .env KLIK.'
            ) from exc
        self.stdout.write('[preflight] FedNow /health OK')

    def _preflight_no_active_session(self) -> None:
        active = SettlementSession.objects.filter(
            zone='US',
            status__in=[
                SettlementSessionStatus.OPEN,
                SettlementSessionStatus.NETTING,
                SettlementSessionStatus.SETTLING,
            ],
        ).first()
        if active:
            raise RuntimeError(
                f'Aktywna sesja US {active.id} (status={active.status}) blokuje run. '
                'Zakończ ją w Admin lub poczekaj na jej zakończenie.'
            )

    # ──────────────────────────────────────────────────────────────────
    # Seed helpers
    # ──────────────────────────────────────────────────────────────────

    def _get_or_create_bank(self, name: str, rtn: str, acct: str) -> Bank:
        bank, _ = Bank.objects.get_or_create(
            name=name,
            defaults={
                'zone': Zone.US,
                'currency': Currency.USD,
                'debt_limit': Decimal('10000000.00'),
                'active': True,
                'webhook_url': 'http://bank-mock-backend:8100/webhook',
                'c2b_enabled': True,
                'fednow_routing_number': rtn,
                'fednow_account_number': acct,
            },
        )
        # Upewnij się że pola FedNow są ustawione (bank mógł istnieć wcześniej)
        dirty = []
        if bank.fednow_routing_number != rtn:
            bank.fednow_routing_number = rtn
            dirty.append('fednow_routing_number')
        if bank.fednow_account_number != acct:
            bank.fednow_account_number = acct
            dirty.append('fednow_account_number')
        if not bank.active:
            bank.active = True
            dirty.append('active')
        if dirty:
            bank.save(update_fields=[*dirty, 'updated_at'])
        return bank

    def _get_or_create_agent(self, name: str, settlement_bank: Bank) -> Agent:
        agent, _ = Agent.objects.get_or_create(
            name=name,
            defaults={'settlement_bank': settlement_bank, 'zone': Zone.US},
        )
        if not MSCAgreement.objects.filter(agent=agent, valid_to__isnull=True).exists():
            MSCAgreement.objects.create(
                agent=agent,
                klik_fee_perc=KLIK_PERC,
                agent_fee_perc=AGENT_PERC,
                valid_from=timezone.now() - timezone.timedelta(days=1),
                valid_to=None,
            )
        return agent

    def _get_or_create_merchant(self, name: str, bank: Bank) -> Merchant:
        merchant, _ = Merchant.objects.get_or_create(
            name=name,
            defaults={'settlement_bank': bank, 'zone': Zone.US},
        )
        return merchant

    def _make_tx(
        self,
        *,
        sender_bank: Bank,
        agent: Agent,
        merchant: Merchant,
        amount: Decimal,
        idempotency_key: str,
    ) -> Transaction:
        """Tworzy COMPLETED Transaction i wywołuje record_c2b_transaction.

        record_c2b_transaction tworzy LedgerEntries:
            BANK_TO_BANK  — zobowiązanie netto między bankami (off-us)
            KLIK_FEE_C2B  — prowizja KLIK
            AGENT_FEE     — prowizja agenta
        """
        klik_fee  = _klik_fee(amount)
        agent_fee = _agent_fee(amount)
        merch_net = amount - klik_fee - agent_fee
        now = timezone.now()

        tx = Transaction.objects.create(
            sender_bank=sender_bank,
            agent=agent,
            merchant=merchant,
            amount_gross=amount,
            klik_fee=klik_fee,
            agent_fee=agent_fee,
            merchant_net=merch_net,
            currency=Currency.USD,
            zone=Zone.US,
            is_on_us=(sender_bank.id == merchant.settlement_bank_id),
            idempotency_key=idempotency_key,
            status=TransactionStatus.COMPLETED,
            code_snapshot='SMOKE',
            authorized_at=now,
            completed_at=now,
        )
        LedgerService.record_c2b_transaction(tx)
        return tx

    # ──────────────────────────────────────────────────────────────────
    # Scenariusz HAPPY
    # ──────────────────────────────────────────────────────────────────

    def _scenario_happy(self) -> ScenarioResult:
        """3 banki US, krzyżujące się tx off-us → netting → FedNow → COMPLETED.

        Identyczna struktura co chaps_smoke._scenario_realistic i target_smoke._scenario_happy:
        banki A, B, C; 3 tx krzyżujące się → 2 transfery netto do C.

        Weryfikuje:
            - sesja COMPLETED, wszystkie transfery COMPLETED
            - rtgs_reference zaczyna się od "FEDNOW-"
            - nasze entries settled po sesji
            - KLIK_FEE_C2B i AGENT_FEE z poprawnymi kwotami (dla każdej tx)
        """
        name = 'HAPPY — 3 banki US, netting, FedNow settled, prowizje OK'
        self.stdout.write(f'\n[scenario] {name}')
        run = uuid4().hex[:8]

        bank_a = self._get_or_create_bank(f'FN-A-{run}', RTN_A, ACCT_A)
        bank_b = self._get_or_create_bank(f'FN-B-{run}', RTN_B, ACCT_B)
        bank_c = self._get_or_create_bank(f'FN-C-{run}', RTN_C, ACCT_C)
        agent  = self._get_or_create_agent(f'FN-Agent-{run}', bank_c)
        m_b    = self._get_or_create_merchant(f'FN-Merch-B-{run}', bank_b)
        m_a    = self._get_or_create_merchant(f'FN-Merch-A-{run}', bank_a)
        m_c    = self._get_or_create_merchant(f'FN-Merch-C-{run}', bank_c)

        # 3 off-us tx — A→B 150, B→A 200, A→C 100
        # netto: A = -150+200-100 = -50, B = +150-200 = -50, C = +100 → 2 transfery do C
        tx_specs = [
            (bank_a, m_b, Decimal('150.00'), f'fn-{run}-tx1'),
            (bank_b, m_a, Decimal('200.00'), f'fn-{run}-tx2'),
            (bank_a, m_c, Decimal('100.00'), f'fn-{run}-tx3'),
        ]
        txs = []
        for sender, merchant, amount, idem in tx_specs:
            txs.append(self._make_tx(
                sender_bank=sender, agent=agent, merchant=merchant,
                amount=amount, idempotency_key=idem,
            ))

        self.stdout.write(f'  [seed] {len(txs)} tx off-us, run={run}')

        # ── Asercja prowizji PRZED settlement ──────────────────────
        for tx in txs:
            err = self._check_fee_entries(tx)
            if err:
                return ScenarioResult(name, False, err)
        self.stdout.write('  [fee] KLIK_FEE_C2B + AGENT_FEE + merchant_net spójne  ✓')

        # ── Settlement ─────────────────────────────────────────────
        task_result = run_settlement_session.apply(args=['US']).get()
        self.stdout.write(f'  [task] {task_result}')

        session_id = task_result.get('session_id')
        if not session_id:
            return ScenarioResult(name, False, f'brak session_id w wyniku: {task_result}')

        session = SettlementSession.objects.get(id=session_id)
        transfers = list(SettlementTransfer.objects.filter(session=session))

        if not transfers:
            return ScenarioResult(name, False, 'oczekiwano ≥1 transferu, jest 0')

        failed_transfers = [t for t in transfers if t.status != SettlementTransferStatus.COMPLETED]
        if failed_transfers:
            details = [
                (str(t.from_bank), str(t.to_bank), t.status, getattr(t, 'failure_reason', ''))
                for t in failed_transfers
            ]
            return ScenarioResult(name, False, f'transfery nie-COMPLETED: {details}')

        if session.status != SettlementSessionStatus.COMPLETED:
            return ScenarioResult(name, False, f'sesja status={session.status}, oczekiwano COMPLETED')

        # Referencje FedNow muszą zaczynać się od "FEDNOW-"
        bad_refs = [t for t in transfers if not t.rtgs_reference.startswith('FEDNOW-')]
        if bad_refs:
            return ScenarioResult(
                name, False,
                f'{len(bad_refs)} transferów bez prefixu FEDNOW-*: '
                f'{[t.rtgs_reference for t in bad_refs]}',
            )

        # Nasze entries muszą być settled po sesji
        unsettled = LedgerEntry.objects.filter(
            session=session,
            source_ref__startswith=f'fn-{run}-',
            settled=False,
        ).count()
        if unsettled:
            return ScenarioResult(name, False, f'{unsettled} naszych entries niesettled')

        self.stdout.write(
            f'  COMPLETED: {len(transfers)} transferów, '
            f'refs={[t.rtgs_reference for t in transfers]}'
        )
        return ScenarioResult(name, True)

    # ──────────────────────────────────────────────────────────────────
    # Scenariusz UNKNOWN RTN
    # ──────────────────────────────────────────────────────────────────

    def _scenario_unknown_rtn(self) -> ScenarioResult:
        """Bank z RTN niezarejestrowanym w FedNow → transfer FAILED.

        Analogia do chaps_smoke._scenario_unknown_bank.
        FedNow zwraca HTTP 400 {"detail": "Unknown bank RTN(s): 000000099"}.
        Gateway mapuje to na FAILED z failure_reason z tego "detail".
        Entry do ghost-banku musi pozostać niesettled.
        """
        name = 'UNKNOWN RTN — FedNow odrzuca niezarejestrowany RTN → FAILED'
        self.stdout.write(f'\n[scenario] {name}')
        run = uuid4().hex[:8]

        bank_a = self._get_or_create_bank(f'FN-A-{run}', RTN_A, ACCT_A)
        ghost  = self._get_or_create_bank(f'FN-Ghost-{run}', RTN_GHOST, ACCT_GHOST)
        agent  = self._get_or_create_agent(f'FN-Agent-{run}', bank_a)
        m_ghost = self._get_or_create_merchant(f'FN-Merch-Ghost-{run}', ghost)

        idem = f'fn-{run}-ghost'
        self._make_tx(
            sender_bank=bank_a, agent=agent, merchant=m_ghost,
            amount=Decimal('100.00'), idempotency_key=idem,
        )
        self.stdout.write(f'  [seed] bank_a ({RTN_A}) → ghost ({RTN_GHOST}) 100 USD')

        task_result = run_settlement_session.apply(args=['US']).get()
        self.stdout.write(f'  [task] {task_result}')

        session_id = task_result.get('session_id')
        if not session_id:
            return ScenarioResult(name, False, f'brak session_id: {task_result}')

        session = SettlementSession.objects.get(id=session_id)
        transfers = list(SettlementTransfer.objects.filter(session=session))

        ghost_transfers = [t for t in transfers if t.to_bank_id == ghost.id]
        if not ghost_transfers:
            return ScenarioResult(name, False, 'brak transferu do ghost bank w sesji')

        t = ghost_transfers[0]
        reason = getattr(t, 'failure_reason', '') or ''

        if t.status != SettlementTransferStatus.FAILED:
            return ScenarioResult(name, False, f'oczekiwano FAILED dla ghost, jest {t.status}')

        # FedNow zwraca "Unknown bank RTN(s): ..." albo lokalny walidator
        # zwraca "Brak danych FedNow..." — oba akceptujemy
        reason_lower = reason.lower()
        if not any(kw in reason_lower for kw in ('unknown', 'rtn', 'routing', 'brak danych')):
            return ScenarioResult(
                name, False,
                f'powód nie zawiera "unknown"/"rtn"/"routing": {reason!r}',
            )

        # Entry do ghost-banku musi być niesettled
        unsettled = LedgerEntry.objects.filter(
            session=session,
            source_ref=idem,
            settled=False,
        ).count()
        if unsettled == 0:
            return ScenarioResult(name, False, 'entry do ghost-banku powinno być niesettled')

        self.stdout.write(f'  FAILED z powodem: {reason!r}, entry niesettled  ✓')
        return ScenarioResult(name, True)

    # ──────────────────────────────────────────────────────────────────
    # Scenariusz FEE ACCRUAL
    # ──────────────────────────────────────────────────────────────────

    def _scenario_fee_accrual(self) -> ScenarioResult:
        """Weryfikacja że prowizja jest faktycznie pobierana z każdej tx.

        Jeden tx off-us 500 USD → 7 asercji:
            1. entry KLIK_FEE_C2B istnieje z kwotą = 500 × 0.30% = 1.50 USD
            2. entry AGENT_FEE istnieje z kwotą = 500 × 1.00% = 5.00 USD
            3. tx.merchant_net = 500 - 1.50 - 5.00 = 493.50 USD
            4. bilans: klik_fee + agent_fee + merchant_net == amount_gross
            5. KLIK_FEE_C2B.beneficiary_type == 'KLIK', beneficiary_ref == None
            6. AGENT_FEE.beneficiary_type == 'AGENT', beneficiary_ref == agent.id
            7. currency='USD', zone='US'

        Nie uruchamia settlement — sprawdza tylko księgowanie prowizji.
        """
        name = 'FEE ACCRUAL — prowizja KLIK + agent pobierana z każdej transakcji'
        self.stdout.write(f'\n[scenario] {name}')
        run = uuid4().hex[:8]
        amount = Decimal('500.00')

        bank_a = self._get_or_create_bank(f'FN-A-{run}', RTN_A, ACCT_A)
        bank_b = self._get_or_create_bank(f'FN-B-{run}', RTN_B, ACCT_B)
        agent  = self._get_or_create_agent(f'FN-Agent-{run}', bank_b)
        m_b    = self._get_or_create_merchant(f'FN-Merch-B-{run}', bank_b)

        idem = f'fn-{run}-fee'
        tx = self._make_tx(
            sender_bank=bank_a, agent=agent, merchant=m_b,
            amount=amount, idempotency_key=idem,
        )

        err = self._check_fee_entries(tx, agent=agent)
        if err:
            return ScenarioResult(name, False, err)

        # Wyświetl podsumowanie
        entries = LedgerEntry.objects.filter(source_ref=idem)
        klik_e  = entries.filter(entry_type='KLIK_FEE_C2B').first()
        agent_e = entries.filter(entry_type='AGENT_FEE').first()
        self.stdout.write(
            f'  amount_gross = {amount} USD\n'
            f'  klik_fee     = {klik_e.amount} USD  '
            f'({KLIK_PERC}% × {amount} = {_klik_fee(amount)})  ✓\n'
            f'  agent_fee    = {agent_e.amount} USD  '
            f'({AGENT_PERC}% × {amount} = {_agent_fee(amount)})  ✓\n'
            f'  merchant_net = {tx.merchant_net} USD  ✓\n'
            f'  bilans: {klik_e.amount + agent_e.amount + tx.merchant_net} == {amount}  ✓'
        )
        return ScenarioResult(name, True)

    # ──────────────────────────────────────────────────────────────────
    # Asercja prowizji — wspólna dla happy i fee-accrual
    # ──────────────────────────────────────────────────────────────────

    def _check_fee_entries(self, tx: Transaction, *, agent=None) -> str | None:
        """Weryfikuje entries prowizji dla transakcji. Zwraca opis błędu lub None."""
        entries = LedgerEntry.objects.filter(source_ref=tx.idempotency_key)

        klik_e  = entries.filter(entry_type='KLIK_FEE_C2B').first()
        agent_e = entries.filter(entry_type='AGENT_FEE').first()

        # 1 + 2. Istnienie entries
        if not klik_e:
            return f'tx {tx.idempotency_key}: brak entry KLIK_FEE_C2B'
        if not agent_e:
            return f'tx {tx.idempotency_key}: brak entry AGENT_FEE'

        # Oczekiwane kwoty
        exp_klik  = _klik_fee(tx.amount_gross)
        exp_agent = _agent_fee(tx.amount_gross)
        exp_net   = tx.amount_gross - exp_klik - exp_agent

        # 1. Kwota klik_fee
        if klik_e.amount != exp_klik:
            return (
                f'tx {tx.idempotency_key}: '
                f'KLIK_FEE_C2B.amount={klik_e.amount} != {exp_klik} '
                f'({tx.amount_gross} × {KLIK_PERC}%)'
            )

        # 2. Kwota agent_fee
        if agent_e.amount != exp_agent:
            return (
                f'tx {tx.idempotency_key}: '
                f'AGENT_FEE.amount={agent_e.amount} != {exp_agent} '
                f'({tx.amount_gross} × {AGENT_PERC}%)'
            )

        # 3. merchant_net na transakcji
        if tx.merchant_net != exp_net:
            return (
                f'tx {tx.idempotency_key}: '
                f'merchant_net={tx.merchant_net} != {exp_net} '
                f'({tx.amount_gross} - {exp_klik} - {exp_agent})'
            )

        # 4. Bilans
        total = klik_e.amount + agent_e.amount + tx.merchant_net
        if total != tx.amount_gross:
            return (
                f'tx {tx.idempotency_key}: '
                f'klik+agent+net={total} != amount_gross={tx.amount_gross}'
            )

        # 5. KLIK_FEE_C2B.beneficiary_type == 'KLIK', bez ref
        if str(klik_e.beneficiary_type) != 'KLIK':
            return f'KLIK_FEE_C2B.beneficiary_type={klik_e.beneficiary_type!r} != "KLIK"'
        if klik_e.beneficiary_ref is not None:
            return f'KLIK_FEE_C2B.beneficiary_ref={klik_e.beneficiary_ref} powinno być None'

        # 6. AGENT_FEE.beneficiary_type == 'AGENT', ref == agent.id
        if str(agent_e.beneficiary_type) != 'AGENT':
            return f'AGENT_FEE.beneficiary_type={agent_e.beneficiary_type!r} != "AGENT"'
        if agent is not None and agent_e.beneficiary_ref != agent.id:
            return (
                f'AGENT_FEE.beneficiary_ref={agent_e.beneficiary_ref} '
                f'!= agent.id={agent.id}'
            )

        # 7. Waluta i strefa
        if str(klik_e.currency) != 'USD':
            return f'KLIK_FEE_C2B.currency={klik_e.currency!r} != "USD"'
        if str(klik_e.zone) != 'US':
            return f'KLIK_FEE_C2B.zone={klik_e.zone!r} != "US"'

        return None

    # ──────────────────────────────────────────────────────────────────
    # Summary — identyczny wzorzec co chaps_smoke i target_smoke
    # ──────────────────────────────────────────────────────────────────

    def _print_summary(self, results: list[ScenarioResult]) -> None:
        self.stdout.write('\n========== FEDNOW SMOKE SUMMARY ==========')
        for r in results:
            if r.skipped:
                tag = self.style.WARNING('SKIP')
            elif r.ok:
                tag = self.style.SUCCESS('PASS')
            else:
                tag = self.style.ERROR('FAIL')
            line = f'  {tag}  {r.name}'
            if r.detail:
                line += f'  — {r.detail}'
            self.stdout.write(line)

        passed  = sum(1 for r in results if r.ok and not r.skipped)
        skipped = sum(1 for r in results if r.skipped)
        failed  = sum(1 for r in results if not r.ok and not r.skipped)

        verdict = self.style.SUCCESS if failed == 0 else self.style.ERROR
        self.stdout.write(verdict(f'\n{passed} PASS,  {skipped} SKIP,  {failed} FAIL'))