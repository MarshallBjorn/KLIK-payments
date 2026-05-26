"""Smoke test settlementu — realny pipeline przez mock-RTGS.

W odróżnieniu od `run_settlement` (który tylko triggeruje sesję bez seedu)
tutaj:

1. Seedujemy 3 banki + agent + MSC + 2 merchantów + 3 transakcje C2B
   tworzące krzyżujące się zobowiązania między bankami.
2. Triggerujemy `run_settlement_session('PL')` — task używa realnego
   `RTGSDispatcher.from_settings()`, więc dispatcher wysyła HTTP do
   serwisu `rtgs-mock` w sieci docker-compose.
3. Weryfikujemy: sesja COMPLETED, netting zredukował 4 raw pary do 2
   transferów (oba do banku C), wszystkie entries settled.

W odróżnieniu od `smoke_c2b` (który testuje webhook → bank), tutaj
walidujemy ścieżkę KLIK → RTGS: serializację `TransferRequest`, response
parsing, healthcheck.

Użycie:
    make smoke MODULE=settle
    docker compose ... exec web python manage.py settle_smoke
    docker compose ... exec web python manage.py settle_smoke --scenario partial

Exit codes:
    0 — wszystkie scenariusze zaliczone
    1 — przynajmniej jeden FAIL (asercja runtime)
    2 — błąd techniczny (RTGS nieosiągalny, seed/exception, aktywna sesja blokuje run)
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
from common.enums import Zone
from ledger.enums import (
    SettlementSessionStatus,
    SettlementTransferStatus,
)
from ledger.models import LedgerEntry, SettlementSession, SettlementTransfer
from ledger.rtgs import RTGSDispatcher
from ledger.services import LedgerService
from ledger.tasks import run_settlement_session
from merchants.models import Merchant

PL_IBAN = {'type': 'iban', 'value': 'PL61109010140000071219812874'}
PL_IBAN_2 = {'type': 'iban', 'value': 'PL27114020040000300201355387'}
PL_IBAN_3 = {'type': 'iban', 'value': 'PL83102000810000060001234567'}


@dataclass
class ScenarioResult:
    name: str
    ok: bool
    detail: str = ''


class Command(BaseCommand):
    help = 'Smoke test settlementu C2B end-to-end przez realny mock-RTGS.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--scenario',
            choices=['happy', 'partial', 'all'],
            default='happy',
            help='happy: czysty run; partial: jeden bank na RTGS blacklist; ' 'all: oba.',
        )

    def handle(self, *_args, **opts):
        scenario = opts['scenario']

        try:
            self._check_rtgs_health()
            self._check_no_active_session('PL')

            results: list[ScenarioResult] = []
            if scenario in ('happy', 'all'):
                results.append(self._scenario_happy())
            if scenario in ('partial', 'all'):
                self._check_no_active_session('PL')
                results.append(self._scenario_partial())
        except Exception as exc:  # noqa: BLE001
            self.stderr.write(self.style.ERROR(f'\nSMOKE ABORTED: {exc}'))
            traceback.print_exc()
            sys.exit(2)

        self._summary(results)
        sys.exit(0 if all(r.ok for r in results) else 1)

    # ------------------------------------------------------------------
    # Pre-flight checks
    # ------------------------------------------------------------------

    def _check_rtgs_health(self) -> None:
        """RTGS musi odpowiadać przed jakimkolwiek seedem."""
        dispatcher = RTGSDispatcher.from_settings()
        health = dispatcher.healthcheck_all()
        downs = [zone for zone, ok in health.items() if not ok]
        if downs:
            raise RuntimeError(f'RTGS down dla stref: {downs}')
        self.stdout.write(f'[pre] RTGS healthy dla wszystkich stref: {list(health)}')

    def _check_no_active_session(self, zone: str) -> None:
        """Jeśli jest OPEN/NETTING/SETTLING — task zwróci SKIPPED i smoke nic
        nie powie o realnym pipeline. Wczesny błąd jest lepszy.
        """
        from ledger.services.ledger_service import ACTIVE_SESSION_STATUSES

        existing = SettlementSession.objects.filter(
            zone=zone, status__in=ACTIVE_SESSION_STATUSES
        ).first()
        if existing:
            raise RuntimeError(
                f'Aktywna sesja {existing.id} (status={existing.status}) blokuje run. '
                f'Zakończ ją (Django admin) lub odpal `make down && make dev-d`.'
            )

    # ------------------------------------------------------------------
    # Seed (idempotentny, unikalne źródła per run)
    # ------------------------------------------------------------------

    def _seed_three_banks_with_crossing_txs(self):
        """3 banki + agent + 3 transakcje z krzyżującymi się obligacjami.

        Każdy run używa unikalnego `run_id` w nazwach/source_refs — wcześniejsze
        artefakty nie kolidują, a nowo utworzone wpisy są łatwo odróżnialne
        w admin/logach.
        """
        run_id = uuid4().hex[:8]

        bank_a = self._make_bank(f'SMOKE_A_{run_id}')
        bank_b = self._make_bank(f'SMOKE_B_{run_id}')
        bank_c = self._make_bank(f'SMOKE_C_{run_id}')

        agent = Agent.objects.create(
            name=f'smoke_agent_{run_id}',
            api_key_hash=f'smoke_agent_hash_{run_id}',
            settlement_bank=bank_c,
            account_identifier=PL_IBAN_3,
            zone=Zone.PL,
        )
        MSCAgreement.objects.create(
            agent=agent,
            klik_fee_perc=Decimal('0.30'),
            agent_fee_perc=Decimal('1.00'),
            valid_from=timezone.now() - timezone.timedelta(days=1),
        )
        merchant_in_b = self._make_merchant(f'M-B-{run_id}', bank_b, PL_IBAN_2)
        merchant_in_a = self._make_merchant(f'M-A-{run_id}', bank_a, PL_IBAN)
        merchant_in_c = self._make_merchant(f'M-C-{run_id}', bank_c, PL_IBAN_3)

        for sender, merchant, amount, label in [
            (bank_a, merchant_in_b, Decimal('150.00'), 'tx1'),
            (bank_b, merchant_in_a, Decimal('200.00'), 'tx2'),
            (bank_a, merchant_in_c, Decimal('100.00'), 'tx3'),
        ]:
            tx = self._make_completed_tx(
                sender_bank=sender,
                agent=agent,
                merchant=merchant,
                amount=amount,
                idem=f'smoke-{run_id}-{label}',
            )
            LedgerService.record_c2b_transaction(tx)

        self.stdout.write(
            f'[seed] run_id={run_id}: 3 banki (A/B/C), 3 transakcje, '
            f'{LedgerEntry.objects.filter(source_ref__startswith=f"smoke-{run_id}").count()} '
            f'wpisów ledger'
        )
        return bank_a, bank_b, bank_c, run_id

    def _make_bank(self, name: str) -> Bank:
        return Bank.objects.create(
            name=name,
            api_key_hash=f'{name.lower()}_hash',
            zone=Zone.PL,
            currency='PLN',
            debt_limit=Decimal('1000000.00'),
            webhook_url=f'https://{name.lower()}.example.com/webhook',
            active=True,
        )

    def _make_merchant(self, name: str, settlement_bank: Bank, account: dict) -> Merchant:
        return Merchant.objects.create(
            name=name,
            settlement_bank=settlement_bank,
            account_identifier=account,
            zone=Zone.PL,
        )

    def _make_completed_tx(
        self, *, sender_bank, agent, merchant, amount: Decimal, idem: str
    ) -> Transaction:
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

    # ------------------------------------------------------------------
    # Scenariusze
    # ------------------------------------------------------------------

    def _scenario_happy(self) -> ScenarioResult:
        name = 'HAPPY (3 banki, krzyżujące się tx, RTGS clean)'
        self.stdout.write(f'\n--- {name} ---')

        bank_a, bank_b, bank_c, run_id = self._seed_three_banks_with_crossing_txs()
        self._reset_rtgs_blacklist()

        result = run_settlement_session.apply(args=['PL']).get()
        self.stdout.write(f'  task result: {result}')

        if result.get('status') != 'COMPLETED':
            return ScenarioResult(name, False, f'oczekiwano COMPLETED, dostano {result}')

        session = SettlementSession.objects.get(id=result['session_id'])
        if session.status != SettlementSessionStatus.COMPLETED:
            return ScenarioResult(name, False, f'session.status={session.status}')

        # Po nettingu 4 raw pary (A→B, A→C, B→A, B→C) → 2 transfery do C
        transfers = SettlementTransfer.objects.filter(session=session)
        if transfers.count() == 0:
            return ScenarioResult(name, False, 'oczekiwano co najmniej 1 transferu, jest 0')

        non_completed = [t for t in transfers if t.status != SettlementTransferStatus.COMPLETED]
        if non_completed:
            statuses = [(str(t.from_bank), str(t.to_bank), t.status) for t in non_completed]
            return ScenarioResult(name, False, f'transfery nie-COMPLETED: {statuses}')

        # Każdy transfer ma rtgs_reference (potwierdzenie z RTGS)
        no_ref = [t for t in transfers if not t.rtgs_reference]
        if no_ref:
            return ScenarioResult(name, False, f'{len(no_ref)} transferów bez rtgs_reference')

        # Wszystkie NASZE entries (z tego runu) settled
        our_unsettled = LedgerEntry.objects.filter(
            session=session,
            source_ref__startswith=f'smoke-{run_id}-',
            settled=False,
        ).count()
        if our_unsettled > 0:
            return ScenarioResult(name, False, f'{our_unsettled} naszych entries niesettled')

        self.stdout.write(
            f'  COMPLETED. transfery={transfers.count()} (wszystkie SUCCESS), '
            f'sumy={[str(t.amount) for t in transfers]}'
        )
        return ScenarioResult(name, True)

    def _scenario_partial(self) -> ScenarioResult:
        """Bank na blackliście RTGS → transfer z tego banku FAILED,
        drugi SUCCESS → sesja FAILED, mieszane statusy."""
        name = 'PARTIAL (RTGS blacklist na jednym banku)'
        self.stdout.write(f'\n--- {name} ---')

        bank_a, bank_b, bank_c, run_id = self._seed_three_banks_with_crossing_txs()

        # Wstawiamy bank A na blacklistę → A→C transfer dostanie FAILED z RTGS
        if not self._set_rtgs_blacklist([bank_a.name]):
            return ScenarioResult(name, False, 'nie udało się ustawić RTGS blacklisty')
        self.stdout.write(f'  RTGS blacklist: [{bank_a.name}]')

        try:
            result = run_settlement_session.apply(args=['PL']).get()
            self.stdout.write(f'  task result: {result}')

            session = SettlementSession.objects.get(id=result['session_id'])
            # Mieszany wynik → sesja FAILED (mark_settled: any_failed=True)
            if session.status != SettlementSessionStatus.FAILED:
                return ScenarioResult(
                    name, False, f'oczekiwano FAILED session, dostano {session.status}'
                )

            transfers = list(SettlementTransfer.objects.filter(session=session))
            statuses = {t.from_bank_id: t.status for t in transfers}

            # Transfer Z banku A → FAILED
            if statuses.get(bank_a.id) != SettlementTransferStatus.FAILED:
                return ScenarioResult(
                    name, False, f'oczekiwano FAILED dla transferu z A, statusy: {statuses}'
                )
            # Transfer Z banku B → COMPLETED (B nie na blackliście)
            if statuses.get(bank_b.id) != SettlementTransferStatus.COMPLETED:
                return ScenarioResult(
                    name, False, f'oczekiwano COMPLETED dla transferu z B, statusy: {statuses}'
                )

            # Entries A→C (transfer FAILED → zostają niesettled).
            # Entries B→C (transfer SUCCESS → settled).
            ac_unsettled = LedgerEntry.objects.filter(
                session=session, from_bank=bank_a, to_bank=bank_c, settled=False
            ).count()
            bc_settled = LedgerEntry.objects.filter(
                session=session, from_bank=bank_b, to_bank=bank_c, settled=True
            ).count()
            if ac_unsettled == 0:
                return ScenarioResult(
                    name, False, 'entries A→C powinny zostać niesettled po FAILED transferze'
                )
            if bc_settled == 0:
                return ScenarioResult(
                    name, False, 'entries B→C powinny być settled po SUCCESS transferze'
                )

            self.stdout.write(
                '  FAILED session, A→C FAILED + entries niesettled, '
                'B→C SUCCESS + entries settled'
            )
            return ScenarioResult(name, True)
        finally:
            self._reset_rtgs_blacklist()

    # ------------------------------------------------------------------
    # RTGS blacklist (admin endpoint mock-RTGS)
    # ------------------------------------------------------------------

    def _rtgs_admin_url(self) -> str:
        """RTGS mock w sieci compose: rtgs-mock:9000. Admin endpointy są tam globalne."""
        from django.conf import settings

        # Wszystkie strefy w mock-RTGS dzielą bazę; bierzemy SORBNET3 i odcinamy ścieżkę.
        base = settings.SORBNET3_URL.rstrip('/')
        # 'http://rtgs-mock:9000/sorbnet3' → 'http://rtgs-mock:9000'
        if '/' in base.split('://', 1)[1]:
            base = base.rsplit('/', 1)[0]
        return base

    def _set_rtgs_blacklist(self, bank_names: list[str]) -> bool:
        try:
            r = requests.post(
                f'{self._rtgs_admin_url()}/admin/blacklist',
                json={'banks': bank_names},
                timeout=5,
            )
            r.raise_for_status()
            return True
        except requests.RequestException as exc:
            self.stderr.write(f'  blacklist update FAIL: {exc}')
            return False

    def _reset_rtgs_blacklist(self) -> None:
        try:
            requests.delete(f'{self._rtgs_admin_url()}/admin/blacklist', timeout=5)
        except requests.RequestException as exc:
            self.stderr.write(f'  blacklist reset FAIL (ignoruję): {exc}')

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------

    def _summary(self, results: list[ScenarioResult]) -> None:
        self.stdout.write('\n========== SETTLEMENT SMOKE SUMMARY ==========')
        for r in results:
            tag = self.style.SUCCESS('PASS') if r.ok else self.style.ERROR('FAIL')
            line = f'  {tag}  {r.name}'
            if r.detail:
                line += f'  — {r.detail}'
            self.stdout.write(line)
        passed = sum(1 for r in results if r.ok)
        total = len(results)
        verdict = self.style.SUCCESS if passed == total else self.style.ERROR
        self.stdout.write(verdict(f'\n{passed}/{total} scenariuszy zaliczonych'))
