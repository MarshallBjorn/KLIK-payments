"""
Dispatcher RTGS — strategy pattern dla 4 systemów bankowości centralnej.

Architektura (patrz docs/c2b/diagrams/STATE.md, sekcja C2):

    RTGSGateway (interface)
        ├── SORBNET3Gateway   (Polska, PLN)
        ├── TARGET2Gateway    (Strefa euro, EUR)
        ├── CHAPSGateway      (UK, GBP)
        └── FedNowGateway     (USA, USD)

    RTGSDispatcher
        ├── trzyma 4 instancje gateway-ów (mapa Zone → Gateway)
        └── wybiera odpowiedni po strefie sesji

Eksportujemy fasadę — dispatcher i klasy DTO. Gateway-e są dostępne
przez dispatcher.
"""

from ledger.rtgs.dispatcher import RTGSDispatcher
from ledger.rtgs.gateway import RTGSGateway, TransferRequest, TransferResult, TransferStatus

__all__ = [
    'RTGSDispatcher',
    'RTGSGateway',
    'TransferRequest',
    'TransferResult',
    'TransferStatus',
]
