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
from typing import Any
from uuid import UUID

import requests
from defusedxml.ElementTree import ParseError
from defusedxml.ElementTree import fromstring as parse_xml

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
        """GET {base_url}{HEALTH_PATH}, krótki timeout — to ma być szybki check."""
        url = f'{self.base_url}{self.HEALTH_PATH}'
        try:
            response = requests.get(
                url, timeout=min(5, self.timeout_seconds), **self._request_kwargs()
            )
            return response.status_code == 200
        except requests.RequestException as exc:
            logger.warning('RTGS %s healthcheck failed: %s', self.system_name, exc)
            return False

    # ------------------------------------------------------------------
    # Hooks dla podklas
    # ------------------------------------------------------------------

    def _request_kwargs(self) -> dict:
        """Dodatkowe kwargs do requests (np. cert/verify dla mTLS). Default: brak."""
        return {}

    def _validate_transfer(self, transfer: TransferRequest) -> str | None:
        """Walidacja lokalna PRZED wysyłką. Zwraca powód błędu lub None.

        Pozwala odrzucić transfer jako FAILED bez rzucania wyjątku (który
        wywaliłby całą pętlę settle). Default: brak walidacji.
        """
        return None

    def _post_settle(self, url: str, session_id: UUID, transfer: TransferRequest):
        """Wykonuje POST settlementu. Default: JSON (format mocka).

        Podklasy o innym formacie (TARGET2 → ISO 20022 XML) nadpisują tę metodę.
        """
        payload = self.build_payload(session_id, transfer)
        headers = {'Content-Type': 'application/json'}
        if self.api_key:
            headers['X-RTGS-Api-Key'] = self.api_key
        return requests.post(
            url,
            json=payload,
            headers=headers,
            timeout=self.timeout_seconds,
            **self._request_kwargs(),
        )

    def _settle_one(self, session_id: UUID, transfer: TransferRequest) -> TransferResult:
        """Wysyła jeden transfer; mapuje wyjątki sieciowe na TIMEOUT."""
        # Walidacja lokalna (np. brak BIC/IBAN dla TARGET) → FAILED bez HTTP.
        error = self._validate_transfer(transfer)
        if error:
            logger.warning(
                'RTGS %s: transfer %s odrzucony lokalnie: %s',
                self.system_name,
                transfer.transfer_id,
                error,
            )
            return TransferResult(
                transfer_id=transfer.transfer_id,
                status=TransferStatus.FAILED,
                failure_reason=error,
            )

        url = f'{self.base_url}{self.SETTLE_PATH}'
        try:
            response = self._post_settle(url, session_id, transfer)
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
            return self.parse_response(transfer, self._decode_response(response))
        except (ValueError, KeyError) as exc:
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

    def _decode_response(self, response) -> dict[str, Any]:
        """Dekoduje ciało odpowiedzi RTGS do dict. Default: JSON (format mocka).

        Podklasy o innym formacie odpowiedzi (TARGET2 → ISO 20022 XML pain.002)
        nadpisują tę metodę. Wyjątki dekodowania zamieniamy na ValueError, żeby
        złapał je wspólny handler w `_settle_one` (→ FAILED 'Invalid response').
        """
        return response.json()

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
    """TARGET — RTGS strefy euro (EUR), ISO 20022 pacs.008-like XML.

    W odróżnieniu od pozostałych systemów (JSON do mocka), TARGET:
    - przyjmuje XML na POST /transfers/xml (Content-Type: application/xml),
    - identyfikuje banki po BIC (DbtrAgt/CdtrAgt) + IBAN-y kont (DbtrAcct/CdtrAcct),
    - zwraca JSON {status:"settled", transfer_id, created_at} lub {detail:"..."},
    - nie ma /healthz → liveness sprawdzamy przez GET /banks,
    - opcjonalnie wymaga mTLS (client cert + CA).
    """

    system_name = 'TARGET2'
    SETTLE_PATH = '/transfers/xml'
    HEALTH_PATH = '/banks'
    _SETTLED_TX_STS = {'ACSC', 'ACCC'}

    def __init__(
        self,
        base_url: str,
        api_key: str = '',
        timeout_seconds: int = 30,
        *,
        client_cert: str = '',
        client_key: str = '',
        ca_cert: str = '',
    ):
        super().__init__(base_url, api_key, timeout_seconds)
        self._client_cert = client_cert
        self._client_key = client_key
        self._ca_cert = ca_cert

    def _request_kwargs(self) -> dict:
        """mTLS: client cert do uwierzytelnienia + CA do weryfikacji serwera.

        Gdy certy nie są skonfigurowane (puste ścieżki) → zwykłe http/https
        bez client-certa. Dzięki temu działa zarówno na http (bez mTLS),
        jak i na https+mTLS — zależnie od configu.
        """
        kwargs: dict = {}
        if self._client_cert and self._client_key:
            kwargs['cert'] = (self._client_cert, self._client_key)
        if self._ca_cert:
            kwargs['verify'] = self._ca_cert
        return kwargs

    def _validate_transfer(self, transfer: TransferRequest) -> str | None:
        """TARGET wymaga BIC obu agentów i IBAN-ów obu kont."""
        missing = [
            field
            for field, value in (
                ('from_bic', transfer.from_bic),
                ('to_bic', transfer.to_bic),
                ('from_iban', transfer.from_iban),
                ('to_iban', transfer.to_iban),
            )
            if not value
        ]
        if missing:
            return f'Brak danych TARGET (uzupełnij bic/settlement_iban banku): {", ".join(missing)}'
        return None

    def _post_settle(self, url: str, session_id: UUID, transfer: TransferRequest):
        xml = self.build_xml(session_id, transfer)
        headers = {'Content-Type': 'application/xml'}
        return requests.post(
            url,
            data=xml.encode('utf-8'),
            headers=headers,
            timeout=self.timeout_seconds,
            **self._request_kwargs(),
        )

    def build_xml(self, session_id: UUID, transfer: TransferRequest) -> str:
        """Buduje komunikat ISO 20022 (CstmrCdtTrfInitn) dla pojedynczego netto-transferu."""
        amount = f'{transfer.amount:.2f}'
        return (
            '<Document>'
            '<CstmrCdtTrfInitn>'
            f'<PmtId><EndToEndId>{transfer.transfer_id.hex}</EndToEndId></PmtId>'
            f'<Amt><InstdAmt Ccy="{transfer.currency}">{amount}</InstdAmt></Amt>'
            f'<DbtrAcct><Id><IBAN>{transfer.from_iban}</IBAN></Id></DbtrAcct>'
            f'<CdtrAcct><Id><IBAN>{transfer.to_iban}</IBAN></Id></CdtrAcct>'
            f'<DbtrAgt><FinInstnId><BIC>{transfer.from_bic}</BIC></FinInstnId></DbtrAgt>'
            f'<CdtrAgt><FinInstnId><BIC>{transfer.to_bic}</BIC></FinInstnId></CdtrAgt>'
            f'<RmtInf><Ustrd>KLIK netting {session_id}</Ustrd></RmtInf>'
            '</CstmrCdtTrfInitn>'
            '</Document>'
        )

    def _decode_response(self, response) -> dict[str, Any]:
        """TARGET zwraca ISO 20022 pain.002 (CstmrPmtStsRpt) jako XML, nie JSON.

        Parsujemy przez defusedxml — odpowiedź pochodzi z zewnętrznego RTGS,
        więc nie ufamy stdlib `xml` (S314: billion-laughs / XXE).
        """
        try:
            root = parse_xml(response.text)
        except ParseError as exc:
            raise ValueError(f'Niepoprawny XML od TARGET: {exc}') from exc
        # Odpowiedź bez namespace (tak buduje ją serwis EU).
        return {
            'status': root.findtext('.//TxSts') or '',
            'transfer_id': root.findtext('.//OrgnlEndToEndId') or '',
        }

    def parse_response(self, transfer, response_json) -> TransferResult:
        """TARGET: pain.002 TxSts ∈ {ACSC,ACCC} → SUCCESS; rtgs_reference = OrgnlEndToEndId."""
        status_str = (response_json.get('status') or '').upper()
        if status_str in self._SETTLED_TX_STS:
            return TransferResult(
                transfer_id=transfer.transfer_id,
                status=TransferStatus.SUCCESS,
                rtgs_reference=str(response_json.get('transfer_id', '')),
            )
        reason = response_json.get('detail') or status_str or 'TARGET: nieznany status'
        logger.warning(
            'RTGS TARGET2: transfer %s nie settled (TxSts=%s)', transfer.transfer_id, status_str
        )
        return TransferResult(
            transfer_id=transfer.transfer_id,
            status=TransferStatus.FAILED,
            failure_reason=str(reason),
        )


class CHAPSGateway(HTTPRTGSGateway):
    """CHAPS — Clearing House Automated Payment System, Bank of England (GBP)."""

    system_name = 'CHAPS'


class FedNowGateway(HTTPRTGSGateway):
    """FedNow — instant payment service Fed (USD).

    W realnym świecie FedNow ma format ISO 20022 i 24/7 settlement. Dla MVP
    zachowujemy się jak pozostałe RTGS-y — różnica tylko w nazwie systemu.
    """

    system_name = 'FedNow'
