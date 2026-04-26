"""
Custom DRF permissions dla apki banks.

`BankHasP2PEnabled` blokuje dostęp do endpointów P2P bankom które nie
podpisały warunków P2P (flaga `bank.p2p_enabled` z T1).

Permissiona używamy w stacku po `IsAuthenticated` — kolejność ma znaczenie,
bo bez auth `request.user` nie jest jeszcze instancją Bank-a.
"""

from rest_framework.permissions import BasePermission

from common.exceptions import P2PNotEnabledError


class BankHasP2PEnabled(BasePermission):
    """Pozwala na dostęp tylko bankom z włączonym modułem P2P.

    Sprawdza `request.user.p2p_enabled`. Zakłada że `request.user` to instancja
    `banks.Bank` ustawiona przez `XKlikBankApiKeyAuthentication`. Nie obsługuje
    innych typów user-a — w MVP ten permission jest używany tylko za bankowym
    auth.
    """

    def has_permission(self, request, view) -> bool:
        user = getattr(request, 'user', None)
        if user is None or not getattr(user, 'is_authenticated', False):
            return False

        if not getattr(user, 'p2p_enabled', False):
            raise P2PNotEnabledError()

        return True
