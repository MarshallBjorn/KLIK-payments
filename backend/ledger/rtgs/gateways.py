"""
Wspólna baza dla 4 implementacji HTTP gateway-ów.

Wyciąga DRY: wszystkie 4 systemy RTGS w MVP rozmawiają HTTP-em z mockiem
(patrz `rtgs_mock` w docker-compose). Różnią się:
- URL bazowym (per strefa, z `.env`)
- formatem payloadu (`build_payload`, `parse_response`) — to nadpisują podklasy
- nazwą systemu (`system_name`) — do logów i identyfikacji w mocku

Implementacje konkretne (SORBNET3/TARGET2/CHAPS/FedNow) dziedziczą stąd
i tylko nadpisują metody formatujące. Logika sieciowa, retry per-transfer,
healthcheck są tu raz.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any
from uuid import UUID

import requests

from ledger.rtgs.exceptions import RTGSUnavailableError
from ledger.rtgs.gateway import (
    RTGSGateway,
    TransferRequest,
    TransferResult,
    TransferStatus,
)

logger = logging.getLogger('klik')


class HTTPRTGSGateway(RTGSGateway):
    """Bazowa implementacja HTTP gateway-a — wspólna dla wszystkich 4 systemów.

    Dlaczego wspólna baza zamiast 4 niezależnych implementacji:
    - W MVP wszystkie 4 RTGS są mockowane jednym serwisem FastAPI.
    - Realnie różnią się tylko formatem payloadu i nazwą systemu.
    - Wyciągnięcie wspólnej logiki HTTP redukuje ryzyko bugów (raz testujemy
      timeout/retry/healthcheck zamiast 4 razy).

    Podklasa MUSI nadpisać:
    - `system_name` (str)
    - `build_payload(transfer)` — jak zmapować TransferRequest na dict
    - `parse_response(transfer, response_json)` — jak odczytać status z odpowiedzi

    Endpoint protokół (mock RTGS):
        POST {base_url}/settle  (per transfer)
            request:  {transfer_id, from, to, amount, currency, ...}
            response: {status: "SUCCESS"|"FAILED", rtgs_reference?, failure_reason?}
        GET  {base_url}/healthz
            response: 200 OK
    """

    system_name: str = 'GENERIC_HTTP_RTGS'

    # Per transfer wykonujemy POST. Dla MVP nie robimy batch — każdy transfer
    # osobno, bo wymóg "częściowego commitu" z A5 (jeden fail nie wywala innych).
    SETTLE_PATH: str = '/settle'
    HEALTH_PATH: str = '/healthz'

    # ------------------------------------------------------------------
    # Public API (z RTGSGateway)
    # ------------------------------------------------------------------

    def settle(
        self,
        session_id: UUID,
        transfers: list[TransferRequest],
    ) -> list[TransferResult]:
        """Wysyła każdy transfer osobno; agreguje wyniki."""
        results: list[TransferResult] = []

        # Pre-flight healthcheck — jeśli RTGS w ogóle nie odpowiada, nie ma
        # sensu próbować każdego transferu z osobna i zbierać 100 timeoutów.
        # Worker dostanie wyjątek i oznaczy sesję FAILED jednym update'em.
        if not self.healthcheck():
            raise RTGSUnavailableError(
                self.system_name,
                f'healthcheck failed na {self.base_url}{self.HEALTH_PATH}',
            )

        for transfer in transfers:
            result = self._settle_one(session_id, transfer)
            results.append(result)

        successes = sum(1 for r in results if r.status == TransferStatus.SUCCESS)
        logger.info(
            'RTGS %s: sesja %s, %d/%d transferów OK',
            self.system_name,
            session_id,
            successes,
            len(results),
        )
        return results

    def healthcheck(self) -> bool:
        """GET {base_url}/healthz, krótki timeout — to ma być szybki check."""
        url = f'{self.base_url}{self.HEALTH_PATH}'
        try:
            response = requests.get(url, timeout=min(5, self.timeout_seconds))
            return response.status_code == 200
        except requests.RequestException as exc:
            logger.warning('RTGS %s healthcheck failed: %s', self.system_name, exc)
            return False

    # ------------------------------------------------------------------
    # Hooks dla podklas
    # ------------------------------------------------------------------

    def build_payload(self, session_id: UUID, transfer: TransferRequest) -> dict[str, Any]:
        """Zmapuj TransferRequest na payload zgodny z protokołem RTGS.

        Default: wspólny format dla mocka. Realne implementacje nadpisują:
        - SORBNET3 zwraca XML z `<NCBR_ID>`
        - TARGET2 używa ISO 20022 pacs.008
        - CHAPS/FedNow każdy swój format
        Dla MVP wszystkie idą do tego samego mocka (rozróżnia po `system`).
        """
        return {
            'session_id': str(session_id),
            'transfer_id': str(transfer.transfer_id),
            'system': self.system_name,
            'from': transfer.from_bank_code,
            'to': transfer.to_bank_code,
            'amount': str(transfer.amount),  # Decimal → string żeby nie tracić precyzji
            'currency': transfer.currency,
        }

    def parse_response(
        self,
        transfer: TransferRequest,
        response_json: dict[str, Any],
    ) -> TransferResult:
        """Zmapuj odpowiedź RTGS na TransferResult.

        Default zakłada protokół mocka: {status: "SUCCESS"|"FAILED", rtgs_reference, failure_reason}.
        Realne implementacje parsują XML/MX/JSON specyficzny dla systemu.
        """
        status_str = response_json.get('status', 'FAILED')
        try:
            status = TransferStatus(status_str)
        except ValueError:
            # Nieznany status → traktujemy jak FAILED, ale logujemy wprost
            logger.error(
                'RTGS %s zwrócił nieznany status "%s" dla transferu %s',
                self.system_name,
                status_str,
                transfer.transfer_id,
            )
            status = TransferStatus.FAILED

        return TransferResult(
            transfer_id=transfer.transfer_id,
            status=status,
            rtgs_reference=response_json.get('rtgs_reference', ''),
            failure_reason=response_json.get('failure_reason', ''),
        )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _settle_one(self, session_id: UUID, transfer: TransferRequest) -> TransferResult:
        """Wysyła jeden transfer; mapuje wyjątki sieciowe na TIMEOUT."""
        url = f'{self.base_url}{self.SETTLE_PATH}'
        payload = self.build_payload(session_id, transfer)
        headers = {'Content-Type': 'application/json'}
        if self.api_key:
            headers['X-RTGS-Api-Key'] = self.api_key

        try:
            response = requests.post(
                url,
                json=payload,
                headers=headers,
                timeout=self.timeout_seconds,
            )
        except requests.Timeout:
            logger.warning(
                'RTGS %s: timeout dla transferu %s (>%ds)',
                self.system_name,
                transfer.transfer_id,
                self.timeout_seconds,
            )
            return TransferResult(
                transfer_id=transfer.transfer_id,
                status=TransferStatus.TIMEOUT,
                failure_reason=f'Timeout {self.timeout_seconds}s',
            )
        except requests.RequestException as exc:
            logger.warning(
                'RTGS %s: błąd sieci dla transferu %s: %s',
                self.system_name,
                transfer.transfer_id,
                exc,
            )
            return TransferResult(
                transfer_id=transfer.transfer_id,
                status=TransferStatus.TIMEOUT,
                failure_reason=f'Network error: {exc}',
            )

        # HTTP 4xx/5xx — z perspektywy biznesowej najczęściej FAILED
        # (np. 422 bank niewypłacalny, 403 limit przekroczony). Worker
        # to interpretuje jako "transfer nie wszedł, ledger entries wracają".
        if response.status_code >= 400:
            logger.warning(
                'RTGS %s: HTTP %d dla transferu %s, body=%s',
                self.system_name,
                response.status_code,
                transfer.transfer_id,
                response.text[:200],
            )
            try:
                body = response.json()
                reason = body.get('failure_reason') or body.get('detail') or response.text[:100]
            except ValueError:
                reason = response.text[:100] or f'HTTP {response.status_code}'
            return TransferResult(
                transfer_id=transfer.transfer_id,
                status=TransferStatus.FAILED,
                failure_reason=reason,
            )

        try:
            return self.parse_response(transfer, response.json())
        except (ValueError, KeyError) as exc:
            # Niewłaściwy JSON od RTGS — traktujemy jak fail, ale logujemy
            # bo to wskazuje na problem integracyjny, nie biznesowy.
            logger.error(
                'RTGS %s: niewłaściwa odpowiedź dla transferu %s: %s',
                self.system_name,
                transfer.transfer_id,
                exc,
            )
            return TransferResult(
                transfer_id=transfer.transfer_id,
                status=TransferStatus.FAILED,
                failure_reason=f'Invalid response: {exc}',
            )


# ----------------------------------------------------------------------
# 4 konkretne implementacje — dla MVP różnią się tylko `system_name`.
# Realny RTGS wymagałby nadpisania `build_payload`/`parse_response`.
# Dlatego klasy są oddzielne mimo identycznego ciała: czytelność stack
# trace, otwarte rozszerzenie (open-closed), zgodność z diagramem klas C2.
# ----------------------------------------------------------------------


class SORBNET3Gateway(HTTPRTGSGateway):
    """SORBNET3 — system rozliczeń międzybankowych NBP (PLN)."""

    system_name = 'SORBNET3'


class TARGET2Gateway(HTTPRTGSGateway):
    """TARGET2 — system EBC dla strefy euro (EUR)."""

    system_name = 'TARGET2'


class CHAPSGateway(HTTPRTGSGateway):
    """CHAPS — Clearing House Automated Payment System, Bank of England (GBP)."""

    system_name = 'CHAPS'


class FedNowGateway(HTTPRTGSGateway):
    """FedNow — instant payment service Fed (USD).

    W realnym świecie FedNow ma format ISO 20022 i 24/7 settlement. Dla MVP
    zachowujemy się jak pozostałe RTGS-y — różnica tylko w nazwie systemu.
    """

    system_name = 'FedNow'
