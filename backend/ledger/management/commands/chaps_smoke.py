"""Smoke test integracji KLIK → CHAPS (strefa UK, GBP) — realny pipeline.

Celuje w realny serwis CHAPS innego zespołu (`ukps_chaps_server` w sieci
`chaps_klik`). Trzy scenariusze:

- happy        — bezpośrednie LedgerEntry (BANK_TO_BANK), netting 3→2, SUCCESS.
- realistic    — PEŁNY pipeline C2B w GBP: agent + MSC + merchanci + transakcje
                 C2B → record_c2b_transaction → netting → CHAPS. Ćwiczy realną
                 ścieżkę księgowania (merchant_net + klik_fee + agent_fee).
- unknown-bank — przelew do banku spoza CHAPS → FAILED("unknown receiver bank").

Banki seedowane są pod DOKŁADNIE tymi nazwami, pod którymi figurują w CHAPS
(`participant_profiles.name`) — CHAPS robi lookup po nazwie.

Świadomość godzin pracy CHAPS:
    CHAPS odrzuca przelewy po cutoffie ("CHAPS interbank cutoff at 18:00 passed").
    To zewnętrzna reguła, nie regresja KLIK — gdy wszystkie transfery padają z
    powodu "zamknięcia", scenariusz jest SKIP (nie FAIL), a seed jest sprzątany.

WYMAGANIA:
    - SettlementTransfer MUSI mieć pole `failure_reason` (migracja).
    - kontener (web/worker) MUSI być w sieci `chaps_klik`.
    - CHAPS_URL wskazuje na port WEWNĘTRZNY kontenera CHAPS, np.
      http://ukps_chaps_server:8080/v1/klik/chaps
    - chaps-server musi mieć w seedzie: Alice Bank, Barclays Bank, HSBC UK.

Użycie:
    docker compose exec web python manage.py chaps_smoke
    docker compose exec web python manage.py chaps_smoke --scenario realistic
    docker compose exec web python manage.py chaps_smoke --scenario unknown-bank
    docker compose exec web python manage.py chaps_smoke --scenario all

Exit codes:
    0 — brak FAIL (PASS i/lub SKIP)
    1 — przynajmniej jeden FAIL (asercja runtime)
    2 — błąd techniczny (CHAPS nieosiągalny, aktywna sesja UK blokuje run, seed)
"""

from __future__ import annotations

import sys
import traceback
from dataclasses import dataclass
from decimal import Decimal
from uuid import uuid4

from django.core.management.base import BaseCommand
from django.utils import timezone

from agents.models import Agent, MSCAgreement
from banks.models import Bank
from codes.enums import TransactionStatus
from codes.models import Transaction
from common.enums import Currency, Zone
from ledger.enums import (
    BeneficiaryType,
    LedgerEntryType,
    SettlementSessionStatus,
    SettlementTransferStatus,
)
from ledger.models import LedgerEntry, SettlementSession, SettlementTransfer
from ledger.rtgs import RTGSDispatcher
from ledger.services import LedgerService
from ledger.tasks import run_settlement_session
from merchants.models import Merchant

# Nazwy MUSZĄ być 1:1 z seedem CHAPS (participant_profiles.name).
CHAPS_ALICE = 'Alice Bank'
CHAPS_BARCLAYS = 'Barclays Bank'
CHAPS_HSBC = 'HSBC UK'
# Bank istniejący w KLIK, ale NIE zarejestrowany w CHAPS — do scenariusza błędu.
GHOST_BANK = 'Ghost Bank UK'

# Poprawne IBAN-y GB (prefix GB wymagany przez common.account dla strefy UK).
IBAN_AGENT = {'type': 'iban', 'value': 'GB29NWBK60161331926819'}
IBAN_M_ALICE = {'type': 'iban', 'value': 'GB94BARC10201530093459'}
IBAN_M_BARCLAYS = {'type': 'iban', 'value': 'GB33BUKB20201555555555'}
IBAN_M_HSBC = {'type': 'iban', 'value': 'GB29HBUK40126243456789'}

# Powody, po których uznajemy że CHAPS jest "zamknięty" (cutoff / godziny pracy).
CLOSED_HINTS = ('cutoff', 'closed', 'working hours', 'business hours', 'out of hours')


def _looks_closed(reason: str) -> bool:
    r = (reason or '').lower()
    return any(h in r for h in CLOSED_HINTS)


@dataclass
class ScenarioResult:
    name: str
    ok: bool
    detail: str = ''
    skipped: bool = False


class Command(BaseCommand):
    help = 'Smoke test integracji KLIK → CHAPS (UK/GBP) end-to-end.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--scenario',
            choices=['happy', 'realistic', 'unknown-bank', 'all'],
            default='happy',
            help='happy: bezpośrednie entries; realistic: pełny pipeline C2B w GBP; '
            'unknown-bank: odbiorca spoza CHAPS → FAILED("unknown"); all: wszystkie.',
        )

    def handle(self, *_args, **opts):
        scenario = opts['scenario']
        try:
            self._check_chaps_health()
            self._check_no_active_session('UK')

            results: list[ScenarioResult] = []
            if scenario in ('happy', 'all'):
                results.append(self._scenario_happy())
            if scenario in ('realistic', 'all'):
                self._check_no_active_session('UK')
                results.append(self._scenario_realistic())
            if scenario in ('unknown-bank', 'all'):
                self._check_no_active_session('UK')
                results.append(self._scenario_unknown_bank())
        except Exception as exc:  # noqa: BLE001
            self.stderr.write(self.style.ERROR(f'\nSMOKE ABORTED: {exc}'))
            traceback.print_exc()
            sys.exit(2)

        self._summary(results)
        sys.exit(0 if all(r.ok for r in results) else 1)

    # ------------------------------------------------------------------
    # Pre-flight
    # ------------------------------------------------------------------

    def _check_chaps_health(self) -> None:
        """Tylko UK musi odpowiadać — nie wymagamy mock-RTGS dla PL/EU/US."""
        dispatcher = RTGSDispatcher.from_settings()
        health = dispatcher.healthcheck_all()
        if not health.get('UK'):
            raise RuntimeError(
                'CHAPS (UK) healthcheck FAILED. Sprawdź: (1) chaps-server działa, '
                '(2) kontener jest w sieci chaps_klik, (3) CHAPS_URL wskazuje na '
                'port WEWNĘTRZNY kontenera, np. '
                'http://ukps_chaps_server:8080/v1/klik/chaps'
            )
        self.stdout.write('[pre] CHAPS (UK) healthy')

    def _check_no_active_session(self, zone: str) -> None:
        from ledger.services.ledger_service import ACTIVE_SESSION_STATUSES

        existing = SettlementSession.objects.filter(
            zone=zone, status__in=ACTIVE_SESSION_STATUSES
        ).first()
        if existing:
            raise RuntimeError(
                f'Aktywna sesja {existing.id} (status={existing.status}) dla {zone} '
                f'blokuje run. Zakończ ją (Django admin) lub `make down && make dev-d`.'
            )

    # ------------------------------------------------------------------
    # Seed — wspólne
    # ------------------------------------------------------------------

    def _get_or_create_uk_bank(self, name: str) -> Bank:
        """Idempotentnie pobiera/tworzy bank UK. Nazwa MUSI być stała (lookup CHAPS)."""
        bank = Bank.objects.filter(name=name).first()
        if bank is not None:
            if bank.zone != Zone.UK:
                raise RuntimeError(
                    f'Bank "{name}" istnieje, ale w strefie {bank.zone} (oczekiwano UK). '
                    f'Usuń go lub popraw strefę — CHAPS wymaga tej nazwy w UK.'
                )
            return bank
        return Bank.objects.create(
            name=name,
            api_key_hash=f'chaps_smoke_{name.lower().replace(" ", "_")}_hash',
            zone=Zone.UK,
            currency=Currency.GBP,
            debt_limit=Decimal('100000000.00'),
            webhook_url=f'https://{name.lower().replace(" ", "-")}.example.co.uk/webhook',
            active=True,
        )

    def _entry(self, *, from_bank, to_bank, amount, run_id, label):
        """Wstawia pojedyncze zobowiązanie BANK_TO_BANK (off-us merchant_net)."""
        return LedgerEntry.objects.create(
            from_bank=from_bank,
            to_bank=to_bank,
            beneficiary_type=BeneficiaryType.BANK,
            beneficiary_ref=to_bank.id,
            amount=amount,
            currency=Currency.GBP,
            zone=Zone.UK,
            entry_type=LedgerEntryType.BANK_TO_BANK,
            source_ref=f'chaps-smoke-{run_id}-{label}',
        )

    def _cleanup_run(self, session, run_id: str) -> None:
        """SKIP scenariusza HAPPY/UNKNOWN: usuwa bezpośrednie entries + sesję."""
        LedgerEntry.objects.filter(source_ref__startswith=f'chaps-smoke-{run_id}-').delete()
        SettlementSession.objects.filter(id=session.id).delete()

    # ------------------------------------------------------------------
    # Seed — realistic (pełny pipeline C2B)
    # ------------------------------------------------------------------

    def _make_merchant_gbp(self, name, bank, account) -> Merchant:
        return Merchant.objects.create(
            name=name, settlement_bank=bank, account_identifier=account, zone=Zone.UK
        )

    def _make_completed_tx_gbp(self, *, sender_bank, agent, merchant, amount, idem) -> Transaction:
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
            currency=Currency.GBP,
            zone=Zone.UK,
            is_on_us=(sender_bank.id == merchant.settlement_bank_id),
            idempotency_key=idem,
            status=TransactionStatus.COMPLETED,
            authorized_at=now,
            completed_at=now,
        )

    def _cleanup_realistic(self, session, run_id: str, agent) -> None:
        """SKIP scenariusza REALISTIC: usuwa entries+sesję+tx+merchantów+agenta.

        Kolejność wymuszona przez PROTECT FK: entries → sesja (cascade transfers)
        → transakcje → merchanci → agent (cascade MSC). Banki zostają (reużywane).
        """
        LedgerEntry.objects.filter(source_ref__startswith=f'chaps-real-{run_id}-').delete()
        SettlementSession.objects.filter(id=session.id).delete()
        Transaction.objects.filter(idempotency_key__startswith=f'chaps-real-{run_id}-').delete()
        Merchant.objects.filter(name__startswith=f'CR-{run_id}-').delete()
        if agent is not None:
            Agent.objects.filter(id=agent.id).delete()

    # ------------------------------------------------------------------
    # Scenariusz HAPPY
    # ------------------------------------------------------------------

    def _scenario_happy(self) -> ScenarioResult:
        name = 'HAPPY (bezpośrednie entries, netting 3→2, CHAPS clean)'
        self.stdout.write(f'\n--- {name} ---')

        run_id = uuid4().hex[:8]
        alice = self._get_or_create_uk_bank(CHAPS_ALICE)
        barclays = self._get_or_create_uk_bank(CHAPS_BARCLAYS)
        hsbc = self._get_or_create_uk_bank(CHAPS_HSBC)

        # Pozycje netto: Alice -50, Barclays -50, HSBC +100 → 2 przelewy do HSBC.
        self._entry(
            from_bank=alice, to_bank=barclays, amount=Decimal('150.00'), run_id=run_id, label='a-b'
        )
        self._entry(
            from_bank=barclays, to_bank=alice, amount=Decimal('200.00'), run_id=run_id, label='b-a'
        )
        self._entry(
            from_bank=alice, to_bank=hsbc, amount=Decimal('100.00'), run_id=run_id, label='a-h'
        )
        self.stdout.write(f'[seed] run_id={run_id}: 3 zobowiązania UK (GBP)')

        result = run_settlement_session.apply(args=['UK']).get()
        self.stdout.write(f'  task result: {result}')

        session = SettlementSession.objects.get(id=result['session_id'])
        transfers = list(SettlementTransfer.objects.filter(session=session))
        return self._assert_settled(
            name,
            result,
            session,
            transfers,
            our_prefix=f'chaps-smoke-{run_id}-',
            on_closed=lambda: self._cleanup_run(session, run_id),
        )

    # ------------------------------------------------------------------
    # Scenariusz REALISTIC — pełny pipeline C2B w GBP
    # ------------------------------------------------------------------

    def _scenario_realistic(self) -> ScenarioResult:
        name = 'REALISTIC (pełny pipeline C2B w GBP → netting → CHAPS)'
        self.stdout.write(f'\n--- {name} ---')

        run_id = uuid4().hex[:8]
        alice = self._get_or_create_uk_bank(CHAPS_ALICE)
        barclays = self._get_or_create_uk_bank(CHAPS_BARCLAYS)
        hsbc = self._get_or_create_uk_bank(CHAPS_HSBC)

        # Agent rozlicza się w HSBC UK (opłata agenta → przelew do HSBC).
        agent = Agent.objects.create(
            name=f'chaps-agent-{run_id}',
            api_key_hash=f'chaps_real_agent_{run_id}',
            settlement_bank=hsbc,
            account_identifier=IBAN_AGENT,
            zone=Zone.UK,
        )
        MSCAgreement.objects.create(
            agent=agent,
            klik_fee_perc=Decimal('0.30'),
            agent_fee_perc=Decimal('1.00'),
            valid_from=timezone.now() - timezone.timedelta(days=1),
        )
        m_alice = self._make_merchant_gbp(f'CR-{run_id}-A', alice, IBAN_M_ALICE)
        m_barclays = self._make_merchant_gbp(f'CR-{run_id}-B', barclays, IBAN_M_BARCLAYS)
        m_hsbc = self._make_merchant_gbp(f'CR-{run_id}-H', hsbc, IBAN_M_HSBC)

        # Off-us transakcje (sender != bank merchanta) → powstają entries BANK_TO_BANK.
        for sender, merchant, amount, label in [
            (alice, m_barclays, Decimal('150.00'), 'tx1'),
            (barclays, m_alice, Decimal('200.00'), 'tx2'),
            (alice, m_hsbc, Decimal('100.00'), 'tx3'),
        ]:
            tx = self._make_completed_tx_gbp(
                sender_bank=sender,
                agent=agent,
                merchant=merchant,
                amount=amount,
                idem=f'chaps-real-{run_id}-{label}',
            )
            LedgerService.record_c2b_transaction(tx)

        entries_n = LedgerEntry.objects.filter(
            source_ref__startswith=f'chaps-real-{run_id}-'
        ).count()
        self.stdout.write(
            f'[seed] run_id={run_id}: agent+MSC+3 merchantów+3 tx C2B → {entries_n} entries'
        )

        result = run_settlement_session.apply(args=['UK']).get()
        self.stdout.write(f'  task result: {result}')

        session = SettlementSession.objects.get(id=result['session_id'])
        transfers = list(SettlementTransfer.objects.filter(session=session))
        return self._assert_settled(
            name,
            result,
            session,
            transfers,
            our_prefix=f'chaps-real-{run_id}-',
            on_closed=lambda: self._cleanup_realistic(session, run_id, agent),
        )

    # ------------------------------------------------------------------
    # Wspólna asercja COMPLETED dla happy/realistic
    # ------------------------------------------------------------------

    def _assert_settled(self, name, result, session, transfers, *, our_prefix, on_closed):
        if not transfers:
            return ScenarioResult(name, False, 'oczekiwano >=1 transferu, jest 0')

        non_completed = [t for t in transfers if t.status != SettlementTransferStatus.COMPLETED]
        if non_completed:
            reasons = {t.failure_reason for t in non_completed}
            if all(_looks_closed(t.failure_reason) for t in non_completed):
                self.stdout.write(f'  CHAPS zamknięty (cutoff/godziny): {reasons}')
                on_closed()
                return ScenarioResult(
                    name, True, f'SKIP — CHAPS zamknięty: {reasons}', skipped=True
                )
            statuses = [
                (str(t.from_bank), str(t.to_bank), t.status, t.failure_reason)
                for t in non_completed
            ]
            return ScenarioResult(name, False, f'transfery nie-COMPLETED: {statuses}')

        if (
            result.get('status') != 'COMPLETED'
            or session.status != SettlementSessionStatus.COMPLETED
        ):
            return ScenarioResult(
                name, False, f'oczekiwano COMPLETED, dostano {result} / {session.status}'
            )

        bad_ref = [t for t in transfers if not t.rtgs_reference.startswith('CHAPS')]
        if bad_ref:
            return ScenarioResult(
                name,
                False,
                f'{len(bad_ref)} transferów bez referencji CHAPS-*: '
                f'{[(str(t.to_bank), t.rtgs_reference) for t in bad_ref]}',
            )

        our_unsettled = LedgerEntry.objects.filter(
            session=session, source_ref__startswith=our_prefix, settled=False
        ).count()
        if our_unsettled:
            return ScenarioResult(name, False, f'{our_unsettled} naszych entries niesettled')

        self.stdout.write(
            f'  COMPLETED. transfery={len(transfers)} (wszystkie SUCCESS), '
            f'refs={[t.rtgs_reference for t in transfers]}'
        )
        return ScenarioResult(name, True)

    # ------------------------------------------------------------------
    # Scenariusz UNKNOWN BANK
    # ------------------------------------------------------------------

    def _scenario_unknown_bank(self) -> ScenarioResult:
        name = 'UNKNOWN BANK (odbiorca spoza CHAPS → FAILED "unknown")'
        self.stdout.write(f'\n--- {name} ---')

        run_id = uuid4().hex[:8]
        alice = self._get_or_create_uk_bank(CHAPS_ALICE)
        ghost = self._get_or_create_uk_bank(GHOST_BANK)  # NIE ma go w CHAPS

        self._entry(
            from_bank=alice, to_bank=ghost, amount=Decimal('100.00'), run_id=run_id, label='a-ghost'
        )
        self.stdout.write(f'[seed] run_id={run_id}: Alice → {GHOST_BANK} 100 GBP')

        result = run_settlement_session.apply(args=['UK']).get()
        self.stdout.write(f'  task result: {result}')

        session = SettlementSession.objects.get(id=result['session_id'])
        transfers = list(SettlementTransfer.objects.filter(session=session))
        ghost_t = [t for t in transfers if t.to_bank_id == ghost.id]
        if not ghost_t:
            return ScenarioResult(name, False, 'brak transferu do Ghost Bank')

        transfer = ghost_t[0]
        reason = transfer.failure_reason
        if transfer.status != SettlementTransferStatus.FAILED:
            return ScenarioResult(name, False, f'transfer do Ghost: {transfer.status}')

        if _looks_closed(reason):
            self.stdout.write(f'  CHAPS zamknięty (cutoff/godziny): {reason!r}')
            self._cleanup_run(session, run_id)
            return ScenarioResult(name, True, f'SKIP — CHAPS zamknięty: {reason!r}', skipped=True)

        if 'unknown' not in (reason or '').lower():
            return ScenarioResult(
                name, False, f'oczekiwano powodu "unknown ... bank", dostano: {reason!r}'
            )

        unsettled = LedgerEntry.objects.filter(
            session=session, source_ref__startswith=f'chaps-smoke-{run_id}-', settled=False
        ).count()
        if unsettled == 0:
            return ScenarioResult(name, False, 'entry do Ghost powinno zostać niesettled')

        self.stdout.write(f'  FAILED z powodem: {reason!r}, entry niesettled')
        return ScenarioResult(name, True)

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------

    def _summary(self, results: list[ScenarioResult]) -> None:
        self.stdout.write('\n========== CHAPS SMOKE SUMMARY ==========')
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
        passed = sum(1 for r in results if r.ok and not r.skipped)
        skipped = sum(1 for r in results if r.skipped)
        failed = sum(1 for r in results if not r.ok)
        verdict = self.style.SUCCESS if failed == 0 else self.style.ERROR
        self.stdout.write(verdict(f'\n{passed} PASS, {skipped} SKIP, {failed} FAIL'))
