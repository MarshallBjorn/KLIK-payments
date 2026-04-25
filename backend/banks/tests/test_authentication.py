"""Testy XKlikApiKeyAuthentication.

Pokrywamy:
- happy path: poprawny klucz aktywnego banku → zwraca (Bank, None)
- brak nagłówka → None (DRF ma sam zdecydować o 401)
- nieprawidłowy klucz → AuthenticationFailed (401)
- bank inactive → BankInactive (403_BANK_INACTIVE)
- integracja z DRF widokiem: end-to-end przez APIClient
"""

import pytest
from rest_framework import exceptions, status
from rest_framework.decorators import (
    api_view,
    authentication_classes,
    permission_classes,
)
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.test import APIClient, APIRequestFactory

from banks.authentication import (
    BankInactive,
    XKlikApiKeyAuthentication,
)


# ----------------------------------------------------------------------
# Test view — używamy w testach integracyjnych
# ----------------------------------------------------------------------


@api_view(["GET"])
@authentication_classes([XKlikApiKeyAuthentication])
@permission_classes([IsAuthenticated])
def _protected_view(request):
    """Minimalny widok wymagający uwierzytelnienia. Zwraca id banku."""
    return Response({"bank_id": str(request.user.id), "zone": request.user.zone})


@pytest.fixture
def factory():
    return APIRequestFactory()


@pytest.fixture
def auth():
    return XKlikApiKeyAuthentication()


# ----------------------------------------------------------------------
# Unit tests — bezpośrednio na klasie auth
# ----------------------------------------------------------------------


@pytest.mark.django_db
class TestXKlikApiKeyAuthentication:
    def test_valid_key_returns_bank(self, factory, auth, make_bank):
        bank, plaintext = make_bank(active=True)
        request = factory.get("/dummy/", HTTP_X_KLIK_API_KEY=plaintext)

        result = auth.authenticate(request)

        assert result is not None
        user, token = result
        assert user.pk == bank.pk
        assert token is None

    def test_missing_header_returns_none(self, factory, auth):
        """Brak nagłówka != błąd auth. DRF zdecyduje czy widok wymaga auth."""
        request = factory.get("/dummy/")
        assert auth.authenticate(request) is None

    def test_empty_header_returns_none(self, factory, auth):
        request = factory.get("/dummy/", HTTP_X_KLIK_API_KEY="")
        assert auth.authenticate(request) is None

    def test_invalid_key_raises_authentication_failed(self, factory, auth, make_bank):
        # Stwórzmy bank, ale podajmy zły klucz
        make_bank(active=True)
        request = factory.get("/dummy/", HTTP_X_KLIK_API_KEY="klik_definitely-not-real")

        with pytest.raises(exceptions.AuthenticationFailed) as exc:
            auth.authenticate(request)

        # Nie powinno być BankInactive — to zupełnie inny scenariusz
        assert not isinstance(exc.value, BankInactive)
        assert exc.value.detail.code == "401_UNAUTHORIZED"

    def test_inactive_bank_raises_bank_inactive(self, factory, auth, make_bank):
        _, plaintext = make_bank(active=False)
        request = factory.get("/dummy/", HTTP_X_KLIK_API_KEY=plaintext)

        with pytest.raises(BankInactive) as exc:
            auth.authenticate(request)

        assert exc.value.status_code == 403
        assert exc.value.default_code == "403_BANK_INACTIVE"

    def test_authenticate_header_returned(self, auth):
        """DRF używa tego do nagłówka WWW-Authenticate przy 401."""
        assert auth.authenticate_header(request=None) == "X-KLIK-Api-Key"

    def test_key_lookup_uses_hash_not_plaintext(self, factory, auth, make_bank):
        """Sanity check: gdyby ktoś zaczął porównywać plaintexty, ten test
        wyłapie regression — wstawiamy do nagłówka hash, oczekujemy 401."""
        bank, _ = make_bank(active=True)
        request = factory.get("/dummy/", HTTP_X_KLIK_API_KEY=bank.api_key_hash)

        with pytest.raises(exceptions.AuthenticationFailed):
            auth.authenticate(request)


# ----------------------------------------------------------------------
# Integration tests — przez APIClient i DRF view
# ----------------------------------------------------------------------


@pytest.fixture
def urlconf(settings):
    """Wstrzykujemy lokalny urlconf z testowym widokiem.

    Pattern: tworzymy pseudomodul i rejestrujemy go w sys.modules, żeby
    `import_string` mogło go znaleźć po nazwie. Dzięki temu nie modyfikujemy
    `core/urls.py` i każdy test ma własny izolowany routing.
    """
    import sys
    import types

    from django.urls import clear_url_caches, path

    module_name = "banks.tests._urlconf_for_auth_test"
    module = types.ModuleType(module_name)
    module.urlpatterns = [path("protected/", _protected_view)]
    sys.modules[module_name] = module

    settings.ROOT_URLCONF = module_name
    clear_url_caches()
    yield module_name
    sys.modules.pop(module_name, None)
    clear_url_caches()


@pytest.mark.django_db
class TestAuthenticationIntegration:
    def test_protected_endpoint_accepts_valid_key(self, urlconf, make_bank):
        bank, plaintext = make_bank(active=True, zone="PL", currency="PLN")
        client = APIClient()

        response = client.get("/protected/", HTTP_X_KLIK_API_KEY=plaintext)

        assert response.status_code == status.HTTP_200_OK
        assert response.data["bank_id"] == str(bank.id)
        assert response.data["zone"] == "PL"

    def test_protected_endpoint_rejects_missing_key(self, urlconf, make_bank):
        make_bank(active=True)
        client = APIClient()

        response = client.get("/protected/")

        # Brak auth + IsAuthenticated permission → 401
        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_protected_endpoint_rejects_wrong_key(self, urlconf, make_bank):
        make_bank(active=True)
        client = APIClient()

        response = client.get("/protected/", HTTP_X_KLIK_API_KEY="klik_wrong")

        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_protected_endpoint_returns_403_for_inactive_bank(self, urlconf, make_bank):
        _, plaintext = make_bank(active=False)
        client = APIClient()

        response = client.get("/protected/", HTTP_X_KLIK_API_KEY=plaintext)

        assert response.status_code == status.HTTP_403_FORBIDDEN
