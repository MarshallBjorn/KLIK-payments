"""
Custom DRF permissions dla apki banks.

`BankHasP2PEnabled` blokuje dostęp do endpointów P2P bankom które nie
podpisały warunków P2P (flaga `bank.p2p_enabled` z T1).

Permissiona używamy w stacku po `IsAuthenticated` — kolejność ma znaczenie,
bo bez auth `request.user` nie jest jeszcze instancją Bank-a.
"""

from rest_framework import status
from rest_framework.exceptions import APIException
from rest_framework.permissions import BasePermission


class P2PNotEnabled(APIException):
    """Bank uwierzytelniony, ale nie ma włączonego modułu P2P.

    Rzucamy z `BankHasP2PEnabled` przez `permission_denied` — DRF zwróci
    response z odpowiednim formatem (przepuszczonym przez exception handler).

    Status 403 + dedykowany code, żeby bank dostał jednoznaczną informację
    "podpisz warunki P2P", a nie generyczne "permission denied".
    """

    status_code = status.HTTP_403_FORBIDDEN
    default_detail = (
        'Bank nie ma włączonego modułu P2P. ' 'Skontaktuj się z operatorem KLIK żeby aktywować.'
    )
    default_code = '403_P2P_NOT_ENABLED'


class BankHasP2PEnabled(BasePermission):
    """Pozwala na dostęp tylko bankom z włączonym modułem P2P.

    Sprawdza `request.user.p2p_enabled`. Zakłada że `request.user` to instancja
    `banks.Bank` ustawiona przez `XKlikBankApiKeyAuthentication`. Nie obsługuje
    innych typów user-a — w MVP ten permission jest używany tylko za bankowym
    auth.
    """

    # DRF rzuca z tym message w response gdy `has_permission` zwróci False.
    # Ale my chcemy własny code (403_P2P_NOT_ENABLED), więc zamiast zwracać
    # False rzucamy własny APIException.

    def has_permission(self, request, view) -> bool:
        # `IsAuthenticated` powinien być wcześniej w stacku, ale defensywnie
        # sprawdzamy czy mamy obiekt Bank w request.user.
        user = getattr(request, 'user', None)
        if user is None or not getattr(user, 'is_authenticated', False):
            # Niech DRF zwróci 401 przez stack permission/auth — nie rzucamy
            # P2PNotEnabled, bo to mylące.
            return False

        # `p2p_enabled` to BooleanField na modelu Bank (T1). getattr żeby nie
        # wybuchać brzydko jeśli ktoś przepuści tu np. Agent zamiast Bank-a
        # w testach — wtedy traktujemy jako "nie ma P2P".
        if not getattr(user, 'p2p_enabled', False):
            raise P2PNotEnabled()

        return True
