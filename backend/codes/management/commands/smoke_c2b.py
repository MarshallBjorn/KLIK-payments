"""Smoke test C2B end-to-end — realny ruch przez sieć między kontenerami.

Uruchamia pełen przepływ KLIK Kody przeciwko żywemu stackowi docker-compose
(`make dev-d`): web ↔ celery worker ↔ bank-mock-backend ↔ Postgres ↔ Redis.
W odróżnieniu od pytestowych testów integracyjnych Poziomu 1, TUTAJ:

- żaden komponent nie jest mockowany (ani Celery, ani HTTP banku),
- Celery działa w trybie produkcyjnym (worker + broker), nie eager,
- webhook autoryzacyjny przechodzi REALNĄ trasą przez Docker network,
- mock-bank steruje decyzją (PIN, balance) — tak samo jak operator w UI.

Użycie z hosta:

    make smoke

Albo bezpośrednio (z dowolnego miejsca w którym dostępny jest stack):

    docker compose ... exec web python manage.py smoke_c2b
    docker compose ... exec web python manage.py smoke_c2b --scenario happy
    docker compose ... exec web python manage.py smoke_c2b --scenario insufficient

Exit codes:
    0 — wszystkie scenariusze zaliczone
    1 — przynajmniej jeden scenariusz NIE przeszedł (asercja runtime)
    2 — błąd techniczny (mock-bank nieosiągalny, seed failed, exception)

Seed jest idempotentny — można odpalać wielokrotnie. Klucze API banku/agenta
są rotowane przy każdym runie i wstrzykiwane do mock-banku przez jego
endpoint `POST /api/config/api-key`.
"""

from __future__ import annotations

import sys
import time
import traceback
import uuid
from dataclasses import dataclass
from decimal import Decimal

import requests
from django.core.management.base import BaseCommand
from django.utils import timezone

from agents.authentication import generate_api_key as generate_agent_key
from agents.models import Agent, MSCAgreement
from banks.models import Bank
from codes.enums import TransactionStatus
from codes.models import Transaction
from common.enums import Zone
from ledger.models import LedgerEntry
from merchants.models import Merchant

# --------------------------------------------------------------------------
# Konfiguracja
# --------------------------------------------------------------------------

# Nazwy serwisów w sieci docker-compose (klik_net):
MOCK_BANK_URL_DEFAULT = 'http://bank-mock-backend:8100'
KLIK_BASE_DEFAULT = 'http://web:8000/api/v1'

# Musi się zgadzać z BANK_MOCK_NAME w docker-compose.yml — operator-mock
# raportuje pod tą nazwą i my musimy ją seedować po stronie KLIK.
SMOKE_SENDER_BANK_NAME = 'BANK_MOCK'
SMOKE_ACQUIRER_BANK_NAME = 'SMOKE_ACQUIRER'
SMOKE_AGENT_NAME = 'SMOKE_AGENT'
SMOKE_MERCHANT_NAME = 'SMOKE_MERCHANT'

PL_IBAN = {'type': 'iban', 'value': 'PL61109010140000071219812874'}
PL_IBAN_2 = {'type': 'iban', 'value': 'PL27114020040000300201355387'}

# Klienci predefiniowani w mock-banku (patrz bank_IO/backend/main.py):
USER_HAPPY = 'user-1'  # balance 5000 — wystarczy na 150
USER_INSUFFICIENT = 'user-3'  # balance 80 — NIE wystarczy na 150
USER_PIN = '1234'


@dataclass
class ScenarioResult:
    name: str
    ok: bool
    detail: str = ''


class Command(BaseCommand):
    help = 'Smoke test C2B end-to-end (real cross-container HTTP via docker-compose).'

    def add_arguments(self, parser):
        parser.add_argument('--mock-bank-url', default=MOCK_BANK_URL_DEFAULT)
        parser.add_argument('--klik-url', default=KLIK_BASE_DEFAULT)
        parser.add_argument(
            '--scenario',
            choices=['happy', 'insufficient', 'all'],
            default='all',
            help='Który scenariusz uruchomić.',
        )
        parser.add_argument(
            '--webhook-timeout',
            type=float,
            default=8.0,
            help='Maks sekund na pojawienie się autoryzacji w mock-banku po /initiate.',
        )

    def handle(self, *_args, **opts):
        mock_url = opts['mock_bank_url'].rstrip('/')
        klik_url = opts['klik_url'].rstrip('/')
        scenario = opts['scenario']
        webhook_timeout = opts['webhook_timeout']

        try:
            self._wait_for_mock_bank(mock_url)
            bank, bank_key, _agent_obj, agent_key, merchant = self._seed(mock_url)
            self._configure_mock_bank_key(mock_url, bank_key)

            results: list[ScenarioResult] = []
            if scenario in ('happy', 'all'):
                results.append(
                    self._scenario_happy(klik_url, mock_url, agent_key, merchant, webhook_timeout)
                )
            if scenario in ('insufficient', 'all'):
                results.append(
                    self._scenario_insufficient(
                        klik_url, mock_url, agent_key, merchant, webhook_timeout
                    )
                )
        except Exception as exc:  # noqa: BLE001
            self.stderr.write(self.style.ERROR(f'\nSMOKE ABORTED: {exc}'))
            traceback.print_exc()
            sys.exit(2)

        self._summary(results)
        sys.exit(0 if all(r.ok for r in results) else 1)

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------

    def _wait_for_mock_bank(self, mock_url: str, timeout: float = 15.0) -> None:
        """Czeka aż mock-bank odpowie na /healthz — daje czas na cold-start."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                r = requests.get(f'{mock_url}/healthz', timeout=2)
                if r.status_code == 200:
                    self.stdout.write(f'[setup] mock-bank healthy ({mock_url})')
                    return
            except requests.RequestException:
                pass
            time.sleep(0.3)
        raise RuntimeError(f'mock-bank nieosiągalny pod {mock_url}')

    def _seed(self, mock_url: str):
        """Idempotentny seed: bank-nadawca, bank-acquirer, agent, MSC, merchant.

        Klucze API banku/agenta są ROTOWANE przy każdym runie — dzięki temu
        smoke jest deterministyczny niezależnie od stanu poprzedniego runu.
        Webhook URL banku zawsze nadpisujemy na adres mock-banku w sieci compose.
        """

        webhook_base = f'{mock_url}/webhook'

        # Bank-nadawca (mock-bank). Webhook → realny mock-bank.
        sender, _ = Bank.objects.get_or_create(
            name=SMOKE_SENDER_BANK_NAME,
            defaults={
                'api_key_hash': 'placeholder',  # pragma: allowlist secret
                'zone': Zone.PL,
                'currency': 'PLN',
                'debt_limit': Decimal('1000000.00'),
                'webhook_url': webhook_base,
                'c2b_enabled': True,
                'p2p_enabled': True,
                'active': True,
            },
        )
        bank_plaintext = sender.rotate_api_key()
        sender.webhook_url = webhook_base
        sender.c2b_enabled = True
        sender.active = True
        sender.save()

        # Bank-acquirer (off-us settlement) — bez webhook, tylko zaksięgowanie.
        acquirer, _ = Bank.objects.get_or_create(
            name=SMOKE_ACQUIRER_BANK_NAME,
            defaults={
                'api_key_hash': 'smoke_acquirer_placeholder',  # pragma: allowlist secret
                'zone': Zone.PL,
                'currency': 'PLN',
                'debt_limit': Decimal('1000000.00'),
                'webhook_url': '',
                'c2b_enabled': True,
                'active': True,
            },
        )

        # Agent
        agent_plaintext, agent_hash = generate_agent_key()
        agent, _ = Agent.objects.get_or_create(
            name=SMOKE_AGENT_NAME,
            defaults={
                'api_key_hash': agent_hash,
                'settlement_bank': sender,
                'account_identifier': PL_IBAN,
                'zone': Zone.PL,
            },
        )
        agent.api_key_hash = agent_hash
        agent.settlement_bank = sender
        agent.save()

        # MSC — bez `valid_to` żeby pozostała aktywna na zawsze
        MSCAgreement.objects.get_or_create(
            agent=agent,
            defaults={
                'klik_fee_perc': Decimal('0.30'),
                'agent_fee_perc': Decimal('1.00'),
                'valid_from': timezone.now() - timezone.timedelta(days=1),
            },
        )

        # Merchant off-us (settlement w acquirer-banku)
        merchant, _ = Merchant.objects.get_or_create(
            name=SMOKE_MERCHANT_NAME,
            defaults={
                'settlement_bank': acquirer,
                'account_identifier': PL_IBAN_2,
                'zone': Zone.PL,
            },
        )

        self.stdout.write(
            f'[setup] seed OK: bank={sender.name} agent={agent.name} merchant={merchant.name}'
        )
        return sender, bank_plaintext, agent, agent_plaintext, merchant

    def _configure_mock_bank_key(self, mock_url: str, bank_key: str) -> None:
        """Wstrzykuje wyrotowany klucz API banku do runtime mock-banku."""
        r = requests.post(
            f'{mock_url}/api/config/api-key',
            json={'api_key': bank_key},
            timeout=10,
        )
        r.raise_for_status()
        self.stdout.write('[setup] mock-bank: klucz KLIK skonfigurowany')

    # ------------------------------------------------------------------
    # Scenariusze
    # ------------------------------------------------------------------

    def _scenario_happy(
        self, klik_url, mock_url, agent_key, merchant, webhook_timeout
    ) -> ScenarioResult:
        name = 'HAPPY (user-1, ACCEPTED → COMPLETED)'
        self.stdout.write(f'\n--- {name} ---')

        # 1. Operator generuje kod (mock-bank → KLIK)
        code = self._mock_generate_code(mock_url, USER_HAPPY)
        if code is None:
            return ScenarioResult(name, False, 'generate-code: brak odpowiedzi')
        self.stdout.write(f'  [1] kod wygenerowany: {code}')

        # 2. Agent inicjuje płatność (agent → KLIK)
        tx_id, err = self._agent_initiate(klik_url, agent_key, code, merchant.id, '150.00')
        if err:
            return ScenarioResult(name, False, f'initiate: {err}')
        self.stdout.write(f'  [2] transaction_id={tx_id} (PENDING)')

        # 3. Webhook musi dotrzeć do mock-banku (Celery worker, real broker)
        if not self._wait_for_pending(mock_url, tx_id, webhook_timeout):
            return ScenarioResult(name, False, 'webhook nie dotarł do mock-banku w timeout')
        self.stdout.write('  [3] webhook w mock-banku — pending')

        # 4. Operator akceptuje PIN-em (mock-bank → KLIK /payments/confirm)
        r = requests.post(
            f'{mock_url}/api/pending/{tx_id}/accept',
            json={'pin': USER_PIN},
            timeout=10,
        )
        if r.status_code != 200:
            return ScenarioResult(name, False, f'accept HTTP {r.status_code}: {r.text}')
        body = r.json()
        if body.get('auto_rejected'):
            return ScenarioResult(name, False, f'oczekiwano ACCEPTED, mock auto-rejected: {body}')
        self.stdout.write('  [4] mock-bank ACCEPTED → KLIK confirmed')

        # 5. Stan końcowy w DB
        tx = Transaction.objects.get(id=tx_id)
        if tx.status != TransactionStatus.COMPLETED:
            return ScenarioResult(name, False, f'status={tx.status} (oczekiwano COMPLETED)')
        entries = LedgerEntry.objects.filter(source_ref=tx.idempotency_key)
        if entries.count() != 3:
            return ScenarioResult(
                name, False, f'ledger entries={entries.count()} (oczekiwano 3 off-us)'
            )
        self.stdout.write(
            f'  [5] COMPLETED + 3 LedgerEntry. klik_fee={tx.klik_fee} '
            f'agent_fee={tx.agent_fee} net={tx.merchant_net}'
        )
        return ScenarioResult(name, True)

    def _scenario_insufficient(
        self, klik_url, mock_url, agent_key, merchant, webhook_timeout
    ) -> ScenarioResult:
        name = 'INSUFFICIENT_FUNDS (user-3, auto-REJECTED)'
        self.stdout.write(f'\n--- {name} ---')

        code = self._mock_generate_code(mock_url, USER_INSUFFICIENT)
        if code is None:
            return ScenarioResult(name, False, 'generate-code: brak odpowiedzi')
        self.stdout.write(f'  [1] kod wygenerowany: {code}')

        tx_id, err = self._agent_initiate(klik_url, agent_key, code, merchant.id, '150.00')
        if err:
            return ScenarioResult(name, False, f'initiate: {err}')
        self.stdout.write(f'  [2] transaction_id={tx_id}')

        if not self._wait_for_pending(mock_url, tx_id, webhook_timeout):
            return ScenarioResult(name, False, 'webhook nie dotarł')
        self.stdout.write('  [3] pending w mock-banku')

        # PIN OK, ale balance 80 < 150 → mock automatycznie REJECTED
        r = requests.post(
            f'{mock_url}/api/pending/{tx_id}/accept',
            json={'pin': USER_PIN},
            timeout=10,
        )
        if r.status_code != 200:
            return ScenarioResult(name, False, f'accept HTTP {r.status_code}: {r.text}')
        body = r.json()
        if not body.get('auto_rejected'):
            return ScenarioResult(name, False, f'oczekiwano auto-rejected, dostano: {body}')
        if body.get('reject_reason') != 'INSUFFICIENT_FUNDS':
            return ScenarioResult(name, False, f'zły reject_reason: {body.get("reject_reason")}')
        self.stdout.write('  [4] mock auto-REJECTED reason=INSUFFICIENT_FUNDS')

        tx = Transaction.objects.get(id=tx_id)
        if tx.status != TransactionStatus.REJECTED:
            return ScenarioResult(name, False, f'status={tx.status} (oczekiwano REJECTED)')
        if LedgerEntry.objects.filter(source_ref=tx.idempotency_key).exists():
            return ScenarioResult(
                name, False, 'znaleziono LedgerEntry dla REJECTED — nie powinno być'
            )
        self.stdout.write('  [5] REJECTED w DB, brak ledger entries — OK')
        return ScenarioResult(name, True)

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def _mock_generate_code(self, mock_url: str, user_id: str) -> str | None:
        r = requests.post(f'{mock_url}/api/clients/{user_id}/generate-code', timeout=10)
        if r.status_code != 200:
            self.stderr.write(f'    generate-code dla {user_id}: HTTP {r.status_code} {r.text}')
            return None
        return r.json().get('code')

    def _agent_initiate(
        self, klik_url, agent_key, code, merchant_id, amount
    ) -> tuple[str | None, str]:
        idem = f'smoke-{uuid.uuid4().hex[:12]}'
        r = requests.post(
            f'{klik_url}/payments/initiate',
            json={
                'code': code,
                'amount': amount,
                'currency': 'PLN',
                'merchant_id': str(merchant_id),
            },
            headers={
                'X-KLIK-Agent-Api-Key': agent_key,
                'Idempotency-Key': idem,
                'Content-Type': 'application/json',
            },
            timeout=10,
        )
        if r.status_code != 202:
            return None, f'HTTP {r.status_code}: {r.text}'
        return r.json().get('transaction_id'), ''

    def _wait_for_pending(self, mock_url: str, tx_id: str, timeout: float) -> bool:
        """Polluje /api/pending mock-banku aż transaction_id się tam pojawi."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                r = requests.get(f'{mock_url}/api/pending', timeout=5)
                if r.status_code == 200 and any(p.get('transaction_id') == tx_id for p in r.json()):
                    return True
            except requests.RequestException:
                pass
            time.sleep(0.25)
        return False

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------

    def _summary(self, results: list[ScenarioResult]) -> None:
        self.stdout.write('\n========== SMOKE SUMMARY ==========')
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
