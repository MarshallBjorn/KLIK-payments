"""Testy permission BankHasP2PEnabled."""

from decimal import Decimal

import pytest
from rest_framework.test import APIRequestFactory

from banks.models import Bank
from banks.permissions import BankHasP2PEnabled, P2PNotEnabled


@pytest.fixture
def factory():
    return APIRequestFactory()


@pytest.fixture
def perm():
    return BankHasP2PEnabled()


def _bank(*, p2p_enabled: bool, name: str = 'Test Bank PL') -> Bank:
    """Lokalny helper — bank PL z ustawioną flagą P2P. Nie używamy `make_bank`
    z conftest banks/, bo on nie obsługuje flag T1."""
    bank = Bank(
        name=name,
        zone='PL',
        currency='PLN',
        active=True,
        debt_limit=Decimal('1000.00'),
        webhook_url='https://bank.example.com/webhook',
        p2p_enabled=p2p_enabled,
    )
    bank.rotate_api_key()
    bank.save()
    return bank


@pytest.mark.django_db
class TestBankHasP2PEnabled:
    def test_returns_true_for_p2p_enabled_bank(self, factory, perm):
        request = factory.get('/dummy/')
        request.user = _bank(p2p_enabled=True)

        assert perm.has_permission(request, view=None) is True

    def test_raises_for_p2p_disabled_bank(self, factory, perm):
        request = factory.get('/dummy/')
        request.user = _bank(p2p_enabled=False)

        with pytest.raises(P2PNotEnabled) as exc:
            perm.has_permission(request, view=None)
        assert exc.value.status_code == 403
        assert exc.value.default_code == '403_P2P_NOT_ENABLED'

    def test_returns_false_for_unauthenticated(self, factory, perm):
        """Bez auth zwracamy False żeby DRF dało 401, nie nasze 403_P2P."""
        from django.contrib.auth.models import AnonymousUser

        request = factory.get('/dummy/')
        request.user = AnonymousUser()

        assert perm.has_permission(request, view=None) is False
