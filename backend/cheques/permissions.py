"""Uprawnienia dla modułu cheques."""

from rest_framework.permissions import BasePermission

from common.exceptions import BankInactiveError, ModuleNotEnabledError


class BankHasChequesEnabled(BasePermission):
    """Sprawdza czy bank wystawcy ma włączony moduł Cheques."""

    def has_permission(self, request, view):
        bank = request.user
        if not bank.active:
            raise BankInactiveError()
        if not bank.cheques_enabled:
            raise ModuleNotEnabledError('cheques')
        return True
