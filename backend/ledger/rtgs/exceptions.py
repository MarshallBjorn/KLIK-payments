"""Wyjątki domenowe dispatcher RTGS."""


class RTGSError(Exception):
    """Bazowa klasa dla błędów RTGS."""


class RTGSUnavailableError(RTGSError):
    """RTGS całkowicie niedostępny (np. CB system down).

    Worker łapie i oznacza całą sesję jako FAILED — żadne transfery nie zostały
    wykonane, więc entries pozostają nie-settled i wracają do następnej sesji.
    """

    def __init__(self, system_name: str, reason: str = ''):
        self.system_name = system_name
        self.reason = reason
        super().__init__(f'RTGS {system_name} niedostępny: {reason or "brak szczegółów"}')


class UnknownZoneError(RTGSError):
    """Próba dispatcher.dispatch() ze strefą której nie ma w mapowaniu."""

    def __init__(self, zone: str):
        self.zone = zone
        super().__init__(f'Brak skonfigurowanego gateway-a dla strefy {zone}.')
