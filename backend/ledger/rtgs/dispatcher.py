"""
RTGSDispatcher — fasada wybierająca odpowiedni gateway po strefie sesji.

Worker nie powinien wiedzieć która strefa idzie do którego RTGS — to wiedza
infrastrukturalna. Dispatcher trzyma 4 instancje gateway-ów (raz zbudowane
w `from_settings()`), wybiera po `Zone` i deleguje.

Wzór: Strategy + Factory.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from uuid import UUID

from common.enums import Zone
from ledger.rtgs.exceptions import UnknownZoneError
from ledger.rtgs.gateways import (
    CHAPSGateway,
    FedNowGateway,
    SORBNET3Gateway,
    TARGET2Gateway,
)

if TYPE_CHECKING:
    from ledger.rtgs.gateway import RTGSGateway, TransferRequest, TransferResult

logger = logging.getLogger('klik')


class RTGSDispatcher:
    """Wybiera gateway RTGS po strefie i deleguje settlement.

    Konstrukcja: w MVP używamy `from_settings()` (czyta `.env`). Można też
    podać własną mapę gateway-ów przez konstruktor — ułatwia to testy
    z fake gateway-em (patrz `tests/test_dispatcher.py`).
    """

    def __init__(self, gateways: dict[str, RTGSGateway]):
        # Klucze trzymamy jako string (np. 'PL'), nie Zone, bo strefa
        # przychodzi do dispatch() z DB jako string i unikamy konwersji.
        self._gateways = gateways

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def from_settings(cls) -> RTGSDispatcher:
        """Buduje dispatcher z URL-i w Django settings (które czytają z `.env`).

        Wymagane settings:
            SORBNET3_URL, TARGET2_URL, CHAPS_URL, FEDNOW_URL
            RTGS_TIMEOUT_SECONDS  (default 30)
        """
        from django.conf import settings

        timeout = getattr(settings, 'RTGS_TIMEOUT_SECONDS', 30)

        gateways: dict[str, RTGSGateway] = {
            Zone.PL: SORBNET3Gateway(
                base_url=settings.SORBNET3_URL,
                timeout_seconds=timeout,
            ),
            Zone.EU: TARGET2Gateway(
                base_url=settings.TARGET2_URL,
                timeout_seconds=timeout,
                client_cert=getattr(settings, 'TARGET_CLIENT_CERT', ''),
                client_key=getattr(settings, 'TARGET_CLIENT_KEY', ''),
                ca_cert=getattr(settings, 'TARGET_CA_CERT', ''),
            ),
            Zone.UK: CHAPSGateway(
                base_url=settings.CHAPS_URL,
                timeout_seconds=timeout,
            ),
            Zone.US: FedNowGateway(
                base_url=settings.FEDNOW_URL,
                timeout_seconds=timeout,
            ),
        }
        logger.info('RTGSDispatcher: skonfigurowano %d gateway-ów', len(gateways))
        return cls(gateways)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def dispatch(
        self,
        zone: str,
        session_id: UUID,
        transfers: list[TransferRequest],
    ) -> list[TransferResult]:
        """Wybiera gateway dla strefy i wysyła wszystkie transfery.

        Args:
            zone: kod strefy (PL/EU/UK/US).
            session_id: ID sesji rozliczeniowej.
            transfers: lista przelewów (po nettingu).

        Returns:
            Lista wyników (jeden per transfer).

        Raises:
            UnknownZoneError: brak gateway-a dla podanej strefy.
            RTGSUnavailableError: cały RTGS nieosiągalny (z gateway.settle()).
        """
        gateway = self._gateways.get(zone)
        if gateway is None:
            raise UnknownZoneError(zone)

        if not transfers:
            logger.info('dispatch: pusta lista transferów dla strefy %s — no-op', zone)
            return []

        logger.info(
            'dispatch: strefa=%s, gateway=%s, sesja=%s, transferów=%d',
            zone,
            gateway.system_name,
            session_id,
            len(transfers),
        )
        return gateway.settle(session_id, transfers)

    def healthcheck_all(self) -> dict[str, bool]:
        """Sprawdza dostępność wszystkich 4 gateway-ów.

        Używane przez health endpoint operatora i przed ręcznym triggerem sesji.
        """
        return {zone: gw.healthcheck() for zone, gw in self._gateways.items()}
