"""
Abstrakcja gateway-a RTGS oraz DTO dla wymiany danych dispatcher ↔ gateway.

Każda strefa ma swój RTGS (SORBNET3 / TARGET2 / CHAPS / FedNow) z innym
formatem komunikatu, ale wszystkie sprowadzają się do tego samego kontraktu:
"weź listę przelewów, spróbuj je wykonać, oddaj per-transfer status".

Dlatego mamy `RTGSGateway` jako klasę abstrakcyjną z dwiema metodami:
- `settle(session_id, transfers)` → list[TransferResult]
- `healthcheck()` → bool

Dispatcher trzyma cztery instancje (po jednej per strefa) i wybiera odpowiednią.
Gateway nie wie nic o Django ORM — operuje na DTO. Mapowanie do/z `SettlementTransfer`
robi worker (patrz `ledger.tasks`).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from uuid import UUID


class TransferStatus(str, Enum):
    """Status pojedynczego przelewu RTGS po dispatch.

    Symetria z `SettlementTransferStatus` z `ledger.enums`, ale jako czysty
    Python enum — gateway nie wymusza zależności od Django.
    """

    SUCCESS = 'SUCCESS'  # RTGS potwierdził wykonanie
    FAILED = 'FAILED'  # Bank niewypłacalny / RTGS odrzucił z powodu biznesowego
    TIMEOUT = 'TIMEOUT'  # RTGS niedostępny / połączenie wygasło


@dataclass(frozen=True)
class TransferRequest:
    """DTO opisujące pojedynczy przelew który ma być zlecony do RTGS.

    Frozen — nie zmieniamy po zbudowaniu, łatwiej śledzić co poszło do gateway.
    UUID pochodzi z `SettlementTransfer.id` — używamy go jako idempotency-key
    po stronie RTGS oraz korelacji wyniku.
    """

    transfer_id: UUID
    from_bank_code: str  # Bank.name — w realu BIC/sort code, dla mocka wystarczy nazwa
    to_bank_code: str
    amount: Decimal
    currency: str


@dataclass(frozen=True)
class TransferResult:
    """DTO opisujące wynik pojedynczego przelewu zwrócony przez RTGS.

    `rtgs_reference` — ID nadane przez RTGS (np. SORBNET3 transaction reference).
    Pusty string gdy TIMEOUT/FAILED bez referencji.

    `failure_reason` — czytelny powód błędu (do `SettlementTransfer.failure_reason`,
    do raportów dla banków, do logów).
    """

    transfer_id: UUID
    status: TransferStatus
    rtgs_reference: str = ''
    failure_reason: str = ''


class RTGSGateway(ABC):
    """Interfejs gatewaya RTGS (SORBNET3 / TARGET2 / CHAPS / FedNow).

    Implementacje muszą być bezstanowe (poza `base_url`/`api_key` ustawianymi
    w konstruktorze) — dispatcher trzyma jedną instancję per strefa na cały
    cykl życia procesu workera.
    """

    #: Identyfikator systemu RTGS dla logów/raportów. Nadpisywany w podklasie.
    system_name: str = 'GENERIC_RTGS'

    def __init__(self, base_url: str, api_key: str = '', timeout_seconds: int = 30):
        self.base_url = base_url.rstrip('/')
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds

    @abstractmethod
    def settle(
        self,
        session_id: UUID,
        transfers: list[TransferRequest],
    ) -> list[TransferResult]:
        """Zleca listę przelewów do RTGS i zwraca per-transfer wynik.

        Implementacja MUSI:
        - Wysłać każdy transfer (równolegle lub po kolei — implementacja decyduje).
        - Zwrócić listę o tej samej długości co `transfers`, w dowolnej kolejności.
          Korelacja po `TransferResult.transfer_id`.
        - Przy timeout/błędzie sieci ustawić status TIMEOUT (nie podnosić wyjątku
          dla pojedynczego transferu — fail jednego nie wywala całej sesji).
        - Przy całkowitej niedostępności RTGS może podnieść wyjątek
          `RTGSUnavailableError` (worker wtedy oznaczy całą sesję FAILED).

        Args:
            session_id: ID sesji rozliczeniowej (do korelacji w logach RTGS).
            transfers: lista przelewów do wykonania.

        Returns:
            Lista wyników (jeden per transfer).
        """

    @abstractmethod
    def healthcheck(self) -> bool:
        """Sprawdza czy RTGS odpowiada. Używane przez `RTGSDispatcher.healthcheck_all()`
        oraz health endpoint operatora KLIK.

        Returns:
            True gdy gateway odpowiada, False przy timeout/błędzie.
        """
