"""Smoke test integracji KLIK → TARGET (strefa EU, EUR, ISO 20022).

Celuje w realny TARGET RTGS (port 8001, /transfers/xml). Onboarding (rejestracja
banków + injection płynności) jest wbudowany — w realu byłby krokiem operatora.

Scenariusze:
- happy             — 3 banki EU, netting 3→2, settled; weryfikacja sald w TARGET
                      (kredytobiorca netto dostaje dokładnie kwotę NETTO).
- insufficient-funds — dłużnik bez płynności → FAILED("Insufficient funds").
- blocked-bank      — dłużnik zablokowany w TARGET → FAILED("blocked").

WYMAGANIA:
    - Bank ma pola bic + settlement_iban (migracja), SettlementTransfer.failure_reason.
    - web/worker w sieci TARGET-a, TARGET2_URL = http(s)://<host>:8001.
    - przy mTLS: TARGET_CLIENT_CERT/KEY/CA ustawione + certy zamontowane.

Użycie:
    docker compose exec web python manage.py target_smoke
    docker compose exec web python manage.py target_smoke --scenario all

Exit codes: 0 brak FAIL | 1 FAIL | 2 błąd techniczny.
"""

from __future__ import annotations

import sys
import traceback
from dataclasses import dataclass
from decimal import Decimal
from uuid import uuid4

import requests
from django.conf import settings
from django.core.management.base import BaseCommand

from banks.models import Bank
from common.enums import Currency, Zone
from ledger.enums import (
    BeneficiaryType,
    LedgerEntryType,
    SettlementTransferStatus,
)
from ledger.models import LedgerEntry, SettlementSession, SettlementTransfer
from ledger.rtgs import RTGSDispatcher
from ledger.tasks import run_settlement_session

# (nazwa KLIK, BIC, settlement IBAN) — IBAN-y poprawne formatowo (MOD-97 po stronie TARGET).
BANK_PL = ('TGT Bank PL', 'BANKPLPW', 'PL61109010140000071219812874')
BANK_DE = ('TGT Bank DE', 'BANKDEXX', 'DE89370400440532013000')
BANK_FR = ('TGT Bank FR', 'BANKFRPP', 'FR1420041010050500013M02606')
BANK_ES = (
    'TGT Bank ES',
    'BANKESXX',
    'ES9121000418450200051332',
)  # insufficient-funds (bez injection)
BANK_IT = ('TGT Bank IT', 'BANKITXX', 'IT60X0542811101000000123456')  # blocked-bank


@dataclass
class ScenarioResult:
    name: str
    ok: bool
    detail: str = ''


class Command(BaseCommand):
    help = 'Smoke test integracji KLIK → TARGET (EU/EUR) end-to-end.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--scenario',
            choices=['happy', 'insufficient-funds', 'blocked-bank', 'all'],
            default='happy',
        )

    def handle(self, *_args, **opts):
        scenario = opts['scenario']
        try:
            self._check_target_health()
            self._check_no_active_session('EU')

            results: list[ScenarioResult] = []
            if scenario in ('happy', 'all'):
                results.append(self._scenario_happy())
            if scenario in ('insufficient-funds', 'all'):
                self._check_no_active_session('EU')
                results.append(self._scenario_insufficient())
            if scenario in ('blocked-bank', 'all'):
                self._check_no_active_session('EU')
                results.append(self._scenario_blocked())
        except Exception as exc:  # noqa: BLE001
            self.stderr.write(self.style.ERROR(f'\nSMOKE ABORTED: {exc}'))
            traceback.print_exc()
            sys.exit(2)

        self._summary(results)
        sys.exit(0 if all(r.ok for r in results) else 1)

    # ------------------------------------------------------------------
    # TARGET API (rejestracja, płynność, blokada, salda)
    # ------------------------------------------------------------------

    def _target_url(self) -> str:
        return settings.TARGET2_URL.rstrip('/')

    def _req_kwargs(self) -> dict:
        kw: dict = {}
        cert = getattr(settings, 'TARGET_CLIENT_CERT', '')
        key = getattr(settings, 'TARGET_CLIENT_KEY', '')
        ca = getattr(settings, 'TARGET_CA_CERT', '')
        if cert and key:
            kw['cert'] = (cert, key)
        if ca:
            kw['verify'] = ca
        return kw

    def _register_target_bank(self, bic: str, name: str) -> None:
        """POST /banks — toleruje 'już istnieje' (idempotencja między runami)."""
        r = requests.post(
            f'{self._target_url()}/banks',
            json={'bic': bic, 'name': name},
            timeout=10,
            **self._req_kwargs(),
        )
        if r.status_code >= 400 and 'exist' not in r.text.lower() and r.status_code not in (409,):
            # 400 z powodu duplikatu jest OK; inne błędy zgłaszamy.
            self.stdout.write(f'  [warn] register {bic}: HTTP {r.status_code} {r.text[:120]}')

    def _inject(self, bic: str, amount: Decimal) -> None:
        r = requests.post(
            f'{self._target_url()}/liquidity/injection',
            json={'bank_bic': bic, 'amount': float(amount), 'currency': 'EUR'},
            timeout=10,
            **self._req_kwargs(),
        )
        r.raise_for_status()

    def _set_blocked(self, bic: str, blocked: bool) -> None:
        action = 'block' if blocked else 'unblock'
        requests.post(
            f'{self._target_url()}/banks/{action}/{bic}', timeout=10, **self._req_kwargs()
        )

    def _balance(self, bic: str) -> Decimal:
        r = requests.get(f'{self._target_url()}/banks/{bic}', timeout=10, **self._req_kwargs())
        r.raise_for_status()
        return Decimal(str(r.json().get('balance', '0')))

    # ------------------------------------------------------------------
    # Pre-flight
    # ------------------------------------------------------------------

    def _check_target_health(self) -> None:
        dispatcher = RTGSDispatcher.from_settings()
        if not dispatcher.healthcheck_all().get('EU'):
            raise RuntimeError(
                'TARGET (EU) healthcheck FAILED. Sprawdź: TARGET2_URL (host:8001), '
                'sieć docker, oraz — przy mTLS — certy (TARGET_CLIENT_CERT/KEY/CA).'
            )
        self.stdout.write('[pre] TARGET (EU) healthy')

    def _check_no_active_session(self, zone: str) -> None:
        from ledger.services.ledger_service import ACTIVE_SESSION_STATUSES

        existing = SettlementSession.objects.filter(
            zone=zone, status__in=ACTIVE_SESSION_STATUSES
        ).first()
        if existing:
            raise RuntimeError(
                f'Aktywna sesja {existing.id} ({existing.status}) dla {zone} blokuje run.'
            )

    # ------------------------------------------------------------------
    # Seed KLIK
    # ------------------------------------------------------------------

    def _get_or_create_eu_bank(self, spec) -> Bank:
        name, bic, iban = spec
        bank = Bank.objects.filter(name=name).first()
        if bank:
            return bank
        return Bank.objects.create(
            name=name,
            api_key_hash=f'tgt_smoke_{bic.lower()}_hash',
            zone=Zone.EU,
            currency=Currency.GBP if False else Currency.EUR,
            debt_limit=Decimal('100000000.00'),
            webhook_url=f'https://{bic.lower()}.example.eu/webhook',
            active=True,
            bic=bic,
            settlement_iban=iban,
        )

    def _entry(self, *, from_bank, to_bank, amount, run_id, label):
        return LedgerEntry.objects.create(
            from_bank=from_bank,
            to_bank=to_bank,
            beneficiary_type=BeneficiaryType.BANK,
            beneficiary_ref=to_bank.id,
            amount=amount,
            currency=Currency.EUR,
            zone=Zone.EU,
            entry_type=LedgerEntryType.BANK_TO_BANK,
            source_ref=f'target-smoke-{run_id}-{label}',
        )

    def _cleanup(self, session, run_id):
        LedgerEntry.objects.filter(source_ref__startswith=f'target-smoke-{run_id}-').delete()
        SettlementSession.objects.filter(id=session.id).delete()

    # ------------------------------------------------------------------
    # Scenariusz HAPPY
    # ------------------------------------------------------------------

    def _scenario_happy(self) -> ScenarioResult:
        name = 'HAPPY (3 banki EU, netting 3→2, settled + salda netto)'
        self.stdout.write(f'\n--- {name} ---')
        run_id = uuid4().hex[:8]

        pl = self._get_or_create_eu_bank(BANK_PL)
        de = self._get_or_create_eu_bank(BANK_DE)
        fr = self._get_or_create_eu_bank(BANK_FR)
        for nm, bic, _iban in (BANK_PL, BANK_DE, BANK_FR):
            self._register_target_bank(bic, nm)
            self._inject(bic, Decimal('1000000.00'))

        fr_before = self._balance(BANK_FR[1])

        # Netto: PL -50, DE -50, FR +100 → 2 przelewy do FR.
        self._entry(
            from_bank=pl, to_bank=de, amount=Decimal('150.00'), run_id=run_id, label='pl-de'
        )
        self._entry(
            from_bank=de, to_bank=pl, amount=Decimal('200.00'), run_id=run_id, label='de-pl'
        )
        self._entry(
            from_bank=pl, to_bank=fr, amount=Decimal('100.00'), run_id=run_id, label='pl-fr'
        )
        self.stdout.write(f'[seed] run_id={run_id}: 3 zobowiązania EU (EUR)')

        result = run_settlement_session.apply(args=['EU']).get()
        self.stdout.write(f'  task result: {result}')

        session = SettlementSession.objects.get(id=result['session_id'])
        transfers = list(SettlementTransfer.objects.filter(session=session))
        if not transfers:
            return ScenarioResult(name, False, 'brak transferów')

        non_completed = [t for t in transfers if t.status != SettlementTransferStatus.COMPLETED]
        if non_completed:
            return ScenarioResult(
                name,
                False,
                f'nie-COMPLETED: {[(str(t.from_bank), str(t.to_bank), t.failure_reason) for t in non_completed]}',
            )
        if result.get('status') != 'COMPLETED':
            return ScenarioResult(name, False, f'sesja {session.status}')
        if any(not t.rtgs_reference for t in transfers):
            return ScenarioResult(name, False, 'transfer bez rtgs_reference')

        # Weryfikacja NETTINGU na saldach TARGET: FR dostaje dokładnie 100 (netto), nie brutto.
        fr_delta = self._balance(BANK_FR[1]) - fr_before
        if fr_delta != Decimal('100.00'):
            return ScenarioResult(
                name, False, f'saldo FR zmieniło się o {fr_delta}, oczekiwano 100.00 (netto)'
            )

        self.stdout.write(
            f'  COMPLETED. transfery={len(transfers)} settled, FR +{fr_delta} EUR (netto OK), '
            f'refs={[t.rtgs_reference for t in transfers]}'
        )
        return ScenarioResult(name, True)

    # ------------------------------------------------------------------
    # Scenariusz INSUFFICIENT FUNDS
    # ------------------------------------------------------------------

    def _scenario_insufficient(self) -> ScenarioResult:
        name = 'INSUFFICIENT FUNDS (dłużnik bez płynności → FAILED)'
        self.stdout.write(f'\n--- {name} ---')
        run_id = uuid4().hex[:8]

        es = self._get_or_create_eu_bank(BANK_ES)
        fr = self._get_or_create_eu_bank(BANK_FR)
        self._register_target_bank(BANK_ES[1], BANK_ES[0])  # rejestrujemy, NIE wstrzykujemy
        self._register_target_bank(BANK_FR[1], BANK_FR[0])

        self._entry(
            from_bank=es, to_bank=fr, amount=Decimal('100.00'), run_id=run_id, label='es-fr'
        )
        result = run_settlement_session.apply(args=['EU']).get()
        self.stdout.write(f'  task result: {result}')

        session = SettlementSession.objects.get(id=result['session_id'])
        t = SettlementTransfer.objects.filter(session=session, from_bank=es).first()
        if not t:
            return ScenarioResult(name, False, 'brak transferu z ES')
        if t.status != SettlementTransferStatus.FAILED:
            return ScenarioResult(name, False, f'oczekiwano FAILED, jest {t.status}')
        if 'insufficient' not in (t.failure_reason or '').lower():
            return ScenarioResult(
                name, False, f'powód: {t.failure_reason!r} (oczekiwano insufficient)'
            )

        self.stdout.write(f'  FAILED zgodnie z oczekiwaniem: {t.failure_reason!r}')
        return ScenarioResult(name, True)

    # ------------------------------------------------------------------
    # Scenariusz BLOCKED BANK
    # ------------------------------------------------------------------

    def _scenario_blocked(self) -> ScenarioResult:
        name = 'BLOCKED BANK (dłużnik zablokowany w TARGET → FAILED)'
        self.stdout.write(f'\n--- {name} ---')
        run_id = uuid4().hex[:8]

        it = self._get_or_create_eu_bank(BANK_IT)
        fr = self._get_or_create_eu_bank(BANK_FR)
        for nm, bic, _ in (BANK_IT, BANK_FR):
            self._register_target_bank(bic, nm)
            self._inject(bic, Decimal('1000000.00'))

        self._set_blocked(BANK_IT[1], True)
        try:
            self._entry(
                from_bank=it, to_bank=fr, amount=Decimal('100.00'), run_id=run_id, label='it-fr'
            )
            result = run_settlement_session.apply(args=['EU']).get()
            self.stdout.write(f'  task result: {result}')

            session = SettlementSession.objects.get(id=result['session_id'])
            t = SettlementTransfer.objects.filter(session=session, from_bank=it).first()
            if not t:
                return ScenarioResult(name, False, 'brak transferu z IT')
            if t.status != SettlementTransferStatus.FAILED:
                return ScenarioResult(name, False, f'oczekiwano FAILED, jest {t.status}')
            if 'block' not in (t.failure_reason or '').lower():
                return ScenarioResult(
                    name, False, f'powód: {t.failure_reason!r} (oczekiwano blocked)'
                )

            self.stdout.write(f'  FAILED zgodnie z oczekiwaniem: {t.failure_reason!r}')
            return ScenarioResult(name, True)
        finally:
            self._set_blocked(BANK_IT[1], False)

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------

    def _summary(self, results: list[ScenarioResult]) -> None:
        self.stdout.write('\n========== TARGET SMOKE SUMMARY ==========')
        for r in results:
            tag = self.style.SUCCESS('PASS') if r.ok else self.style.ERROR('FAIL')
            line = f'  {tag}  {r.name}'
            if r.detail:
                line += f'  — {r.detail}'
            self.stdout.write(line)
        passed = sum(1 for r in results if r.ok)
        verdict = self.style.SUCCESS if passed == len(results) else self.style.ERROR
        self.stdout.write(verdict(f'\n{passed}/{len(results)} scenariuszy zaliczonych'))
