"""
Wspólna baza dla 4 implementacji HTTP gateway-ów.

Wyciąga DRY: wszystkie 4 systemy RTGS w MVP rozmawiają HTTP-em. Różnią się:
- URL bazowym (per strefa, z `.env`)
- formatem payloadu (`build_payload`, `parse_response`) — to nadpisują podklasy
- nazwą systemu (`system_name`) — do logów i identyfikacji

Implementacje konkretne (SORBNET3/TARGET2/CHAPS/FedNow) dziedziczą stąd
i tylko nadpisują metody formatujące. Logika sieciowa, retry per-transfer,
healthcheck są tu raz.

FedNow różni się istotnie:
- Używa protokołu ISO 20022 pacs.008 (XML) jak TARGET2, ale wysyła multipart
  do /collect zamiast /transfers/xml
- Identyfikuje banki przez ABA Routing Number (9 cyfr), nie BIC
- Healthcheck przez GET /health (nie /healthz)
- Odpowiedź /collect to ACK ("received and sent"), nie finał settlement
  — FedNow jest systemem gwarantowanego settlement dla zarejestrowanych banków
"""

from __future__ import annotations

import logging
import uuid as uuid_module
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
    """Bazowa implementacja HTTP gateway-a — wspólna dla SORBNET3/TARGET2/CHAPS.

    FedNow ma osobną klasę (FedNowGateway) — różni się mechanizmem wysyłki.

    Dlaczego wspólna baza zamiast 4 niezależnych implementacji:
    - W MVP SORBNET3/CHAPS korzystają z tego samego mocka (rozróżnia po `system`).
    - Wyciągnięcie wspólnej logiki HTTP redukuje ryzyko bugów.
    - FedNow i TARGET2 mają własne klasy dziedziczące — nadpisują _post_settle i parse.

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
    SETTLE_PATH: str = '/settle'
    HEALTH_PATH: str = '/healthz'

    def settle(
        self,
        session_id: UUID,
        transfers: list[TransferRequest],
    ) -> list[TransferResult]:
        """Wysyła każdy transfer osobno; agreguje wyniki."""
        results: list[TransferResult] = []

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
        """GET {base_url}{HEALTH_PATH}, krótki timeout."""
        url = f'{self.base_url}{self.HEALTH_PATH}'
        try:
            response = requests.get(
                url, timeout=min(5, self.timeout_seconds), **self._request_kwargs()
            )
            return response.status_code == 200
        except requests.RequestException as exc:
            logger.warning('RTGS %s healthcheck failed: %s', self.system_name, exc)
            return False

    def _request_kwargs(self) -> dict:
        """Dodatkowe kwargs do requests (np. cert/verify dla mTLS). Default: brak."""
        return {}

    def _validate_transfer(self, transfer: TransferRequest) -> str | None:
        """Walidacja lokalna PRZED wysyłką. Zwraca powód błędu lub None."""
        return None

    def _post_settle(self, url: str, session_id: UUID, transfer: TransferRequest):
        """Wykonuje POST settlementu. Default: JSON (format mocka)."""
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
            return self.parse_response(transfer, response.json())
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

    def build_payload(self, session_id: UUID, transfer: TransferRequest) -> dict[str, Any]:
        """Zmapuj TransferRequest na payload zgodny z protokołem RTGS (mock JSON)."""
        return {
            'session_id': str(session_id),
            'transfer_id': str(transfer.transfer_id),
            'system': self.system_name,
            'from': transfer.from_bank_code,
            'to': transfer.to_bank_code,
            'amount': str(transfer.amount),
            'currency': transfer.currency,
        }

    def parse_response(
        self,
        transfer: TransferRequest,
        response_json: dict[str, Any],
    ) -> TransferResult:
        """Zmapuj odpowiedź RTGS na TransferResult (protokół mocka)."""
        status_str = response_json.get('status', 'FAILED')
        try:
            status = TransferStatus(status_str)
        except ValueError:
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
# SORBNET3 — Polska, PLN (mock RTGS)
# ----------------------------------------------------------------------


class SORBNET3Gateway(HTTPRTGSGateway):
    """SORBNET3 — system rozliczeń międzybankowych NBP (PLN)."""

    system_name = 'SORBNET3'


# ----------------------------------------------------------------------
# TARGET2 — Strefa euro, EUR, ISO 20022 pacs.008-like XML
# ----------------------------------------------------------------------


class TARGET2Gateway(HTTPRTGSGateway):
    """TARGET — RTGS strefy euro (EUR), ISO 20022 pacs.008-like XML.

    W odróżnieniu od SORBNET3/CHAPS (JSON do mocka), TARGET:
    - przyjmuje XML na POST /transfers/xml (Content-Type: application/xml),
    - identyfikuje banki po BIC (DbtrAgt/CdtrAgt) + IBAN-y kont (DbtrAcct/CdtrAcct),
    - zwraca JSON {status:"settled", transfer_id, created_at} lub {detail:"..."},
    - nie ma /healthz → liveness sprawdzamy przez GET /banks,
    - opcjonalnie wymaga mTLS (client cert + CA).
    """

    system_name = 'TARGET2'
    SETTLE_PATH = '/transfers/xml'
    HEALTH_PATH = '/banks'

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
        """mTLS: client cert do uwierzytelnienia + CA do weryfikacji serwera."""
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

    def parse_response(self, transfer, response_json) -> TransferResult:
        """TARGET: status=='settled' → SUCCESS; rtgs_reference = jego transfer_id."""
        status_str = (response_json.get('status') or '').lower()
        if status_str == 'settled':
            return TransferResult(
                transfer_id=transfer.transfer_id,
                status=TransferStatus.SUCCESS,
                rtgs_reference=str(response_json.get('transfer_id', '')),
            )
        reason = (
            response_json.get('detail') or response_json.get('status') or 'TARGET: nieznany status'
        )
        logger.warning('RTGS TARGET2: transfer %s nie settled: %s', transfer.transfer_id, reason)
        return TransferResult(
            transfer_id=transfer.transfer_id,
            status=TransferStatus.FAILED,
            failure_reason=str(reason),
        )


# ----------------------------------------------------------------------
# CHAPS — UK, GBP (mock RTGS)
# ----------------------------------------------------------------------


class CHAPSGateway(HTTPRTGSGateway):
    """CHAPS — Clearing House Automated Payment System, Bank of England (GBP)."""

    system_name = 'CHAPS'


# ----------------------------------------------------------------------
# FedNow — USA, USD, ISO 20022 pacs.008 przez /collect (multipart)
# ----------------------------------------------------------------------

# Namespace ISO 20022 pacs.008.001.08
_PACS008_NS = 'urn:iso:std:iso:20022:tech:xsd:pacs.008.001.08'

# ABA RTN: dokładnie 9 cyfr
import re as _re
_ABA_RTN_RE = _re.compile(r'^\d{9}$')


class FedNowGateway(RTGSGateway):
    """FedNow — Federal Reserve instant payment system (USD).

    Protokół (z FedSystems/FedNow/main.py):
    - Banki identyfikowane przez ABA Routing Transit Number (RTN, 9 cyfr)
    - Transfery jako XML pacs.008.001.08 przez POST /collect (multipart form-data)
    - Health check przez GET /health (nie /healthz jak SORBNET3/CHAPS)
    - Odpowiedź /collect: {"status": "received and sent", ...} przy sukcesie
    - Przy błędzie (nieznany RTN, zły XML): HTTP 400 {"detail": "..."}

    Podejście KLIK (synchroniczny gateway):
        1. Walidacja lokalna: oba banki muszą mieć fednow_routing_number + fednow_account_number
        2. Wyślij pacs.008 → POST /collect (multipart: field 'file' + 'sender_port')
        3. "received and sent" → SUCCESS z FEDNOW-{ref}
        4. HTTP 4xx/5xx → FAILED z powodem z {"detail": "..."}

    Uzasadnienie "received = SUCCESS":
        FedNow jest systemem gwarantowanego settlement dla zarejestrowanych
        uczestników — wysłanie pacs.008 uruchamia nieodwracalny transfer,
        analogicznie do TARGET2 gdzie "settled" = przyjęcie przez clearing.

    Pola wymagane w Bank (strefa US):
        fednow_routing_number  → TransferRequest.from_bic / to_bic
        fednow_account_number  → TransferRequest.from_iban / to_iban

    Te pola są przekazywane przez RTGSDispatcher.from_settings() przy budowaniu
    TransferRequest z SettlementTransfer — tak samo jak bic/settlement_iban dla TARGET2.
    """

    system_name = 'FedNow'
    HEALTH_PATH = '/health'
    COLLECT_PATH = '/collect'

    def __init__(
        self,
        base_url: str,
        api_key: str = '',
        timeout_seconds: int = 30,
        *,
        sender_port: str = '8000',
    ):
        super().__init__(base_url, api_key, timeout_seconds)
        # sender_port: port pod którym FedNow zna KLIK jako uczestnika.
        # FedNow waliduje że sender_port zgadza się z zarejestrowanym bankiem nadawcy.
        # W KLIK jako operator clearingowy używamy jednego globalnego portu.
        self.sender_port = sender_port

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def settle(
        self,
        session_id: UUID,
        transfers: list[TransferRequest],
    ) -> list[TransferResult]:
        """Wysyła każdy transfer jako pacs.008 do FedNow /collect."""
        if not self.healthcheck():
            raise RTGSUnavailableError(
                self.system_name,
                f'healthcheck failed na {self.base_url}{self.HEALTH_PATH}',
            )

        results: list[TransferResult] = []
        for transfer in transfers:
            result = self._settle_one(session_id, transfer)
            results.append(result)

        successes = sum(1 for r in results if r.status == TransferStatus.SUCCESS)
        logger.info(
            'FedNow: sesja %s, %d/%d transferów OK',
            session_id,
            successes,
            len(results),
        )
        return results

    def healthcheck(self) -> bool:
        """GET /health — FedNow zwraca {"status": "FedNow API is healthy"}."""
        url = f'{self.base_url}{self.HEALTH_PATH}'
        try:
            resp = requests.get(url, timeout=min(5, self.timeout_seconds))
            return resp.status_code == 200
        except requests.RequestException as exc:
            logger.warning('FedNow healthcheck failed: %s', exc)
            return False

    # ------------------------------------------------------------------
    # Walidacja lokalna
    # ------------------------------------------------------------------

    def _validate_transfer(self, transfer: TransferRequest) -> str | None:
        """FedNow wymaga ABA RTN (9 cyfr) i numeru konta obu banków.

        Mapowanie (spójne z TARGET2Gateway — reużywa tych samych pól):
            transfer.from_bic  → fednow_routing_number nadawcy
            transfer.to_bic    → fednow_routing_number odbiorcy
            transfer.from_iban → fednow_account_number nadawcy
            transfer.to_iban   → fednow_account_number odbiorcy
        """
        missing = []
        for field, value in (
            ('from_bic (fednow_routing_number nadawcy)', transfer.from_bic),
            ('to_bic (fednow_routing_number odbiorcy)', transfer.to_bic),
            ('from_iban (fednow_account_number nadawcy)', transfer.from_iban),
            ('to_iban (fednow_account_number odbiorcy)', transfer.to_iban),
        ):
            if not value:
                missing.append(field)

        if missing:
            return (
                f'Brak danych FedNow (uzupełnij fednow_routing_number / fednow_account_number '
                f'w konfiguracji banku US): {", ".join(missing)}'
            )

        for field_name, rtn in (('from_bic', transfer.from_bic), ('to_bic', transfer.to_bic)):
            if not _ABA_RTN_RE.match(rtn):
                return (
                    f'Niepoprawny ABA routing number w {field_name}: {rtn!r}. '
                    f'Wymagane dokładnie 9 cyfr.'
                )

        return None

    # ------------------------------------------------------------------
    # Core: pacs.008 → /collect
    # ------------------------------------------------------------------

    def _settle_one(self, session_id: UUID, transfer: TransferRequest) -> TransferResult:
        """Waliduje, buduje pacs.008, wysyła do /collect, zwraca wynik."""
        error = self._validate_transfer(transfer)
        if error:
            logger.warning('FedNow: transfer %s odrzucony lokalnie: %s', transfer.transfer_id, error)
            return TransferResult(
                transfer_id=transfer.transfer_id,
                status=TransferStatus.FAILED,
                failure_reason=error,
            )

        xml_bytes = self._build_pacs008(session_id, transfer)
        filename = f'pacs008_{transfer.transfer_id.hex}.xml'
        url = f'{self.base_url}{self.COLLECT_PATH}'

        try:
            resp = self._post_collect(url, xml_bytes, filename)
        except requests.Timeout:
            logger.warning(
                'FedNow: timeout dla transferu %s (>%ds)',
                transfer.transfer_id,
                self.timeout_seconds,
            )
            return TransferResult(
                transfer_id=transfer.transfer_id,
                status=TransferStatus.TIMEOUT,
                failure_reason=f'Timeout {self.timeout_seconds}s do FedNow /collect',
            )
        except requests.RequestException as exc:
            logger.warning('FedNow: błąd sieci dla transferu %s: %s', transfer.transfer_id, exc)
            return TransferResult(
                transfer_id=transfer.transfer_id,
                status=TransferStatus.TIMEOUT,
                failure_reason=f'Network error: {exc}',
            )

        return self._parse_collect_response(transfer, resp)

    def _post_collect(self, url: str, xml_bytes: bytes, filename: str) -> requests.Response:
        """POST /collect jako multipart/form-data.

        FedNow /collect oczekuje:
            - file:        UploadFile (pole 'file', typ application/xml)
            - sender_port: str (pole 'sender_port')
        """
        files = {'file': (filename, xml_bytes, 'application/xml')}
        data = {'sender_port': self.sender_port}
        return requests.post(url, files=files, data=data, timeout=self.timeout_seconds)

    def _parse_collect_response(
        self,
        transfer: TransferRequest,
        resp: requests.Response,
    ) -> TransferResult:
        """Mapuje odpowiedź /collect na TransferResult.

        Sukces: HTTP 200 + {"status": "received and sent"} → SUCCESS
        Błąd:  HTTP 4xx/5xx + {"detail": "..."} → FAILED z powodem
        """
        if resp.status_code >= 400:
            try:
                body = resp.json()
                reason = body.get('detail') or resp.text[:200]
            except ValueError:
                reason = resp.text[:200] or f'HTTP {resp.status_code}'
            logger.warning(
                'FedNow: HTTP %d dla transferu %s: %s',
                resp.status_code,
                transfer.transfer_id,
                reason,
            )
            return TransferResult(
                transfer_id=transfer.transfer_id,
                status=TransferStatus.FAILED,
                failure_reason=str(reason),
            )

        # Sukces — budujemy referencję na podstawie transfer_id (unikalny,
        # trasowalny w logach FedNow przez EndToEndId z pacs.008)
        fednow_ref = f'FEDNOW-{transfer.transfer_id.hex[:16].upper()}'

        try:
            body = resp.json()
            status_str = body.get('status', 'received')
        except ValueError:
            status_str = 'received'

        if 'received' in status_str.lower():
            logger.info(
                'FedNow: transfer %s → %s OK (ref=%s, rtn_from=%s, rtn_to=%s)',
                transfer.from_bank_code,
                transfer.to_bank_code,
                fednow_ref,
                transfer.from_bic,
                transfer.to_bic,
            )
            return TransferResult(
                transfer_id=transfer.transfer_id,
                status=TransferStatus.SUCCESS,
                rtgs_reference=fednow_ref,
            )

        # Nieoczekiwany status
        logger.error(
            'FedNow: nieoczekiwany status "%s" dla transferu %s',
            status_str,
            transfer.transfer_id,
        )
        return TransferResult(
            transfer_id=transfer.transfer_id,
            status=TransferStatus.FAILED,
            failure_reason=f'FedNow nieoczekiwany status: {status_str}',
        )

    # ------------------------------------------------------------------
    # Budowanie XML pacs.008.001.08
    # ------------------------------------------------------------------

    def _build_pacs008(self, session_id: UUID, transfer: TransferRequest) -> bytes:
        """Buduje komunikat ISO 20022 pacs.008.001.08 zgodny z FedNow.

        Format zgodny z FedSystems/FedNow/example-pacs.008.xml:
            DbtrAgt/MmbId  = routing_number nadawcy (from_bic)
            CdtrAgt/MmbId  = routing_number odbiorcy (to_bic)
            DbtrAcct/Id    = account_number nadawcy (from_iban)
            CdtrAcct/Id    = account_number odbiorcy (to_iban)
            IntrBkSttlmAmt = kwota USD
            EndToEndId     = hex(transfer_id) — idempotency key
            MsgId          = hex(uuid4) — unikalny ID komunikatu per wysyłka
        """
        amount_str = f'{transfer.amount:.2f}'
        end_to_end_id = transfer.transfer_id.hex
        msg_id = uuid_module.uuid4().hex
        from_name = self._xml_escape(transfer.from_bank_code)
        to_name = self._xml_escape(transfer.to_bank_code)

        xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Document xmlns="{_PACS008_NS}">
  <FIToFICstmrCdtTrf>
    <GrpHdr>
      <MsgId>{msg_id}</MsgId>
      <CreDtTm>{self._now_iso()}</CreDtTm>
    </GrpHdr>
    <CdtTrfTxInf>
      <PmtId>
        <EndToEndId>{end_to_end_id}</EndToEndId>
      </PmtId>
      <IntrBkSttlmAmt Ccy="USD">{amount_str}</IntrBkSttlmAmt>
      <DbtrAgt>
        <FinInstnId>
          <ClrSysMmbId>
            <nm>{from_name}</nm>
            <MmbId>{transfer.from_bic}</MmbId>
          </ClrSysMmbId>
        </FinInstnId>
      </DbtrAgt>
      <Dbtr>
        <Nm>{from_name}</Nm>
      </Dbtr>
      <DbtrAcct>
        <Id>
          <Othr>
            <Id>{transfer.from_iban}</Id>
            <SchmeNm><Prtry>US_ACCT</Prtry></SchmeNm>
          </Othr>
        </Id>
      </DbtrAcct>
      <CdtrAgt>
        <FinInstnId>
          <ClrSysMmbId>
            <nm>{to_name}</nm>
            <MmbId>{transfer.to_bic}</MmbId>
          </ClrSysMmbId>
        </FinInstnId>
      </CdtrAgt>
      <Cdtr>
        <Nm>{to_name}</Nm>
      </Cdtr>
      <CdtrAcct>
        <Id>
          <Othr>
            <Id>{transfer.to_iban}</Id>
            <SchmeNm><Prtry>US_ACCT</Prtry></SchmeNm>
          </Othr>
        </Id>
      </CdtrAcct>
      <RmtInf>
        <Ustrd>KLIK netting session {str(session_id)[:8]}</Ustrd>
      </RmtInf>
    </CdtTrfTxInf>
  </FIToFICstmrCdtTrf>
</Document>"""
        return xml.encode('utf-8')

    @staticmethod
    def _now_iso() -> str:
        from datetime import UTC, datetime
        return datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%S')

    @staticmethod
    def _xml_escape(text: str) -> str:
        """Escape znaków specjalnych XML w nazwach banków."""
        return (
            text.replace('&', '&amp;')
                .replace('<', '&lt;')
                .replace('>', '&gt;')
                .replace('"', '&quot;')
                .replace("'", '&apos;')
        )