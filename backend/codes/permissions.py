"""Custom permissions dla apki codes."""

from rest_framework.permissions import BasePermission

from common.exceptions import BankInactiveError, C2BNotEnabledError


class BankHasC2BEnabled(BasePermission):
    """Wymaga że request.user (Bank) ma c2b_enabled=True."""

    def has_permission(self, request, view):
        bank = request.user
        # Bank class duck-typing
        if not hasattr(bank, 'c2b_enabled'):
            return False
        if not bank.active:
            raise BankInactiveError()
        if not bank.c2b_enabled:
            raise C2BNotEnabledError()
        return True
