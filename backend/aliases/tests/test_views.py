"""
E2E testy widoków aliases — przez DRF APIClient.

Pokrywamy każdy endpoint:
- happy path (201/200/204)
- każdy error code z dokumentacji (`docs/c2b/integration/INFO.md`):
  * register: 400, 401, 403_BANK_INACTIVE, 403_P2P_NOT_ENABLED,
              409_ALIAS_ALREADY_EXISTS, 422_ZONE_MISMATCH
  * lookup:   401, 403_BANK_INACTIVE, 403_P2P_NOT_ENABLED, 404_ALIAS_NOT_FOUND
  * delete:   401, 403_BANK_INACTIVE, 403_P2P_NOT_ENABLED,
              403_INSUFFICIENT_PERMISSIONS, 404_ALIAS_NOT_FOUND

Format ciała błędu jest sprawdzany strukturalnie ({"error": {"code", "message",
"timestamp"}}) — to nasz publiczny kontrakt, więc smoke-testujemy go w jednym
wspólnym helperze i potem ufamy.
"""

import pytest
from rest_framework import status

from aliases.models import Alias
from aliases.tests.conftest import PL_IBAN

REGISTER_URL = '/api/v1/aliases/register'


def _lookup_url(phone: str) -> str:
    return f'/api/v1/aliases/lookup/{phone}'


def _delete_url(phone: str) -> str:
    return f'/api/v1/aliases/{phone}'


def _assert_error_envelope(response, *, expected_code: str) -> None:
    """Sprawdza że ciało błędu ma format {"error": {code, message, timestamp}}.

    Nie matchujemy konkretnego komunikatu — to lokalizacja, może się zmienić.
    Pilnujemy tylko strukturalnego kontraktu.
    """
    assert 'error' in response.data, f'Expected error envelope, got {response.data}'
    err = response.data['error']
    assert err['code'] == expected_code
    assert isinstance(err['message'], str) and err['message']
    assert 'timestamp' in err


# ---------------------------------------------------------------------------
# POST /aliases/register
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestRegisterEndpoint:
    def test_201_happy_path(self, auth_client, bank_pl_p2p):
        bank, _ = bank_pl_p2p

        response = auth_client.post(
            REGISTER_URL,
            data={
                'phone': '+48501234567',
                'iban': 'PL61109010140000071219812874',
                'zone': 'PL',
            },
            format='json',
        )

        assert response.status_code == status.HTTP_201_CREATED
        assert response.data['phone'] == '+48501234567'
        assert 'alias_id' in response.data
        assert 'registered_at' in response.data

        # Persistence check
        alias = Alias.objects.get(phone='+48501234567')
        assert alias.bank_id == bank.id

    def test_201_with_explicit_account_identifier(self, auth_client):
        """US-style: strukturalne account_identifier zamiast iban."""
        # Zone PL, więc i tak iban — ale klient może podać account_identifier
        # zamiast iban, oba akceptujemy zgodnie z serializerem.
        response = auth_client.post(
            REGISTER_URL,
            data={
                'phone': '+48501234567',
                'account_identifier': PL_IBAN,
                'zone': 'PL',
            },
            format='json',
        )
        assert response.status_code == status.HTTP_201_CREATED

    def test_400_malformed_payload(self, auth_client):
        """Brak wymaganego pola `phone`."""
        response = auth_client.post(
            REGISTER_URL,
            data={'zone': 'PL', 'iban': 'PL61109010140000071219812874'},
            format='json',
        )
        # DRF default dla ValidationError to 400 — nasz klucz code to '400'
        # (fallback ze status code, bo DRF ValidationError nie ma
        # default_code='400_BAD_REQUEST').
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert 'error' in response.data

    def test_400_when_both_iban_and_account_identifier(self, auth_client):
        response = auth_client.post(
            REGISTER_URL,
            data={
                'phone': '+48501234567',
                'iban': 'PL61109010140000071219812874',
                'account_identifier': PL_IBAN,
                'zone': 'PL',
            },
            format='json',
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_401_no_api_key(self, api_client):
        response = api_client.post(
            REGISTER_URL,
            data={
                'phone': '+48501234567',
                'iban': 'PL61109010140000071219812874',
                'zone': 'PL',
            },
            format='json',
        )
        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_401_wrong_api_key(self, api_client):
        api_client.credentials(
            HTTP_X_KLIK_BANK_API_KEY='klik_definitely-not-real'  # pragma: allowlist secret
        )
        response = api_client.post(
            REGISTER_URL,
            data={
                'phone': '+48501234567',
                'iban': 'PL61109010140000071219812874',
                'zone': 'PL',
            },
            format='json',
        )
        assert response.status_code == status.HTTP_401_UNAUTHORIZED
        _assert_error_envelope(response, expected_code='authentication_failed')

    def test_403_bank_inactive(self, api_client, make_p2p_bank):
        _, plaintext = make_p2p_bank(active=False)
        api_client.credentials(HTTP_X_KLIK_BANK_API_KEY=plaintext)

        response = api_client.post(
            REGISTER_URL,
            data={
                'phone': '+48501234567',
                'iban': 'PL61109010140000071219812874',
                'zone': 'PL',
            },
            format='json',
        )
        assert response.status_code == status.HTTP_403_FORBIDDEN
        _assert_error_envelope(response, expected_code='403_BANK_INACTIVE')

    def test_403_p2p_not_enabled(self, api_client, make_p2p_bank):
        """Bank aktywny, klucz OK — ale flaga P2P=False → 403_P2P_NOT_ENABLED."""
        _, plaintext = make_p2p_bank(p2p_enabled=False)
        api_client.credentials(HTTP_X_KLIK_BANK_API_KEY=plaintext)

        response = api_client.post(
            REGISTER_URL,
            data={
                'phone': '+48501234567',
                'iban': 'PL61109010140000071219812874',
                'zone': 'PL',
            },
            format='json',
        )
        assert response.status_code == status.HTTP_403_FORBIDDEN
        _assert_error_envelope(response, expected_code='403_P2P_NOT_ENABLED')

    def test_409_alias_already_exists(self, auth_client):
        # Pierwsza rejestracja — happy
        auth_client.post(
            REGISTER_URL,
            data={
                'phone': '+48501234567',
                'iban': 'PL61109010140000071219812874',
                'zone': 'PL',
            },
            format='json',
        )

        # Druga z tym samym phone — 409
        response = auth_client.post(
            REGISTER_URL,
            data={
                'phone': '+48501234567',
                'iban': 'PL61109010140000071219812874',
                'zone': 'PL',
            },
            format='json',
        )
        assert response.status_code == status.HTTP_409_CONFLICT
        _assert_error_envelope(response, expected_code='409_ALIAS_ALREADY_EXISTS')

    def test_422_zone_mismatch_phone_prefix(self, api_client, bank_uk_p2p):
        """Numer +48 (PL prefix) rejestrowany w UK banku → 422."""
        _, plaintext = bank_uk_p2p
        api_client.credentials(HTTP_X_KLIK_BANK_API_KEY=plaintext)

        response = api_client.post(
            REGISTER_URL,
            data={
                'phone': '+48501234567',
                'iban': 'GB82WEST12345698765432',
                'zone': 'UK',
            },
            format='json',
        )
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        _assert_error_envelope(response, expected_code='422_ZONE_MISMATCH')

    def test_422_zone_mismatch_zone_vs_bank_zone(self, auth_client):
        """Bank PL, ale klient wysyła zone=UK → 422."""
        response = auth_client.post(
            REGISTER_URL,
            data={
                'phone': '+44501234567',
                'iban': 'GB82WEST12345698765432',
                'zone': 'UK',
            },
            format='json',
        )
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        _assert_error_envelope(response, expected_code='422_ZONE_MISMATCH')


# ---------------------------------------------------------------------------
# GET /aliases/lookup/<phone>
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestLookupEndpoint:
    def test_200_happy_path(self, auth_client, bank_pl_p2p):
        bank, _ = bank_pl_p2p
        # Setup: rejestrujemy alias
        auth_client.post(
            REGISTER_URL,
            data={
                'phone': '+48501234567',
                'iban': 'PL61109010140000071219812874',
                'zone': 'PL',
            },
            format='json',
        )

        response = auth_client.get(_lookup_url('+48501234567'))

        assert response.status_code == status.HTTP_200_OK
        assert response.data['phone'] == '+48501234567'
        assert response.data['bank_id'] == str(bank.id)
        assert response.data['iban'] == 'PL61109010140000071219812874'

    def test_200_increments_counter(self, auth_client, bank_pl_p2p):
        from aliases.services import AliasService

        bank, _ = bank_pl_p2p
        auth_client.post(
            REGISTER_URL,
            data={
                'phone': '+48501234567',
                'iban': 'PL61109010140000071219812874',
                'zone': 'PL',
            },
            format='json',
        )

        service = AliasService()
        assert service.get_lookup_count(bank.id) == 0

        auth_client.get(_lookup_url('+48501234567'))
        assert service.get_lookup_count(bank.id) == 1

        auth_client.get(_lookup_url('+48501234567'))
        assert service.get_lookup_count(bank.id) == 2

    def test_404_not_inc_counter(self, auth_client, bank_pl_p2p):
        from aliases.services import AliasService

        bank, _ = bank_pl_p2p
        response = auth_client.get(_lookup_url('+48999999999'))
        assert response.status_code == status.HTTP_404_NOT_FOUND

        # Counter nie inkrementowany dla 404 — kontrakt z T2 ("po 200")
        assert AliasService().get_lookup_count(bank.id) == 0

    def test_401_no_api_key(self, api_client):
        response = api_client.get(_lookup_url('+48501234567'))
        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_403_bank_inactive(self, api_client, make_p2p_bank):
        _, plaintext = make_p2p_bank(active=False)
        api_client.credentials(HTTP_X_KLIK_BANK_API_KEY=plaintext)

        response = api_client.get(_lookup_url('+48501234567'))
        assert response.status_code == status.HTTP_403_FORBIDDEN
        _assert_error_envelope(response, expected_code='403_BANK_INACTIVE')

    def test_403_p2p_not_enabled(self, api_client, make_p2p_bank):
        _, plaintext = make_p2p_bank(p2p_enabled=False)
        api_client.credentials(HTTP_X_KLIK_BANK_API_KEY=plaintext)

        response = api_client.get(_lookup_url('+48501234567'))
        assert response.status_code == status.HTTP_403_FORBIDDEN
        _assert_error_envelope(response, expected_code='403_P2P_NOT_ENABLED')

    def test_404_alias_not_found(self, auth_client):
        response = auth_client.get(_lookup_url('+48999999999'))
        assert response.status_code == status.HTTP_404_NOT_FOUND
        _assert_error_envelope(response, expected_code='404_ALIAS_NOT_FOUND')

    def test_lookup_works_across_banks(self, api_client, bank_pl_p2p, make_p2p_bank):
        """Sanity: bank A może lookupować alias zarejestrowany przez bank B
        (P2P zakłada że *każdy* bank z P2P może pytać).
        """
        bank_a, plaintext_a = bank_pl_p2p
        bank_b, plaintext_b = make_p2p_bank(name='Bank B PL')

        # Bank A rejestruje
        api_client.credentials(HTTP_X_KLIK_BANK_API_KEY=plaintext_a)
        api_client.post(
            REGISTER_URL,
            data={
                'phone': '+48501234567',
                'iban': 'PL61109010140000071219812874',
                'zone': 'PL',
            },
            format='json',
        )

        # Bank B lookup-uje
        api_client.credentials(HTTP_X_KLIK_BANK_API_KEY=plaintext_b)
        response = api_client.get(_lookup_url('+48501234567'))

        assert response.status_code == status.HTTP_200_OK
        assert response.data['bank_id'] == str(bank_a.id)


# ---------------------------------------------------------------------------
# DELETE /aliases/<phone>
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestDeleteEndpoint:
    def test_204_happy_path(self, auth_client):
        auth_client.post(
            REGISTER_URL,
            data={
                'phone': '+48501234567',
                'iban': 'PL61109010140000071219812874',
                'zone': 'PL',
            },
            format='json',
        )

        response = auth_client.delete(_delete_url('+48501234567'))

        assert response.status_code == status.HTTP_204_NO_CONTENT
        assert not Alias.objects.filter(phone='+48501234567').exists()

    def test_401_no_api_key(self, api_client):
        response = api_client.delete(_delete_url('+48501234567'))
        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_403_bank_inactive(self, api_client, make_p2p_bank):
        _, plaintext = make_p2p_bank(active=False)
        api_client.credentials(HTTP_X_KLIK_BANK_API_KEY=plaintext)

        response = api_client.delete(_delete_url('+48501234567'))
        assert response.status_code == status.HTTP_403_FORBIDDEN
        _assert_error_envelope(response, expected_code='403_BANK_INACTIVE')

    def test_403_p2p_not_enabled(self, api_client, make_p2p_bank):
        _, plaintext = make_p2p_bank(p2p_enabled=False)
        api_client.credentials(HTTP_X_KLIK_BANK_API_KEY=plaintext)

        response = api_client.delete(_delete_url('+48501234567'))
        assert response.status_code == status.HTTP_403_FORBIDDEN
        _assert_error_envelope(response, expected_code='403_P2P_NOT_ENABLED')

    def test_403_insufficient_permissions(self, api_client, bank_pl_p2p, make_p2p_bank):
        """Bank A rejestruje alias, bank B próbuje go usunąć → 403."""
        _, plaintext_a = bank_pl_p2p
        _, plaintext_b = make_p2p_bank(name='Sneaky Bank')

        # Bank A rejestruje
        api_client.credentials(HTTP_X_KLIK_BANK_API_KEY=plaintext_a)
        api_client.post(
            REGISTER_URL,
            data={
                'phone': '+48501234567',
                'iban': 'PL61109010140000071219812874',
                'zone': 'PL',
            },
            format='json',
        )

        # Bank B próbuje usunąć
        api_client.credentials(HTTP_X_KLIK_BANK_API_KEY=plaintext_b)
        response = api_client.delete(_delete_url('+48501234567'))

        assert response.status_code == status.HTTP_403_FORBIDDEN
        _assert_error_envelope(response, expected_code='403_INSUFFICIENT_PERMISSIONS')

        # Alias nie został skasowany
        assert Alias.objects.filter(phone='+48501234567').exists()

    def test_404_alias_not_found(self, auth_client):
        response = auth_client.delete(_delete_url('+48999999999'))
        assert response.status_code == status.HTTP_404_NOT_FOUND
        _assert_error_envelope(response, expected_code='404_ALIAS_NOT_FOUND')


# ---------------------------------------------------------------------------
# Smoke tests dla error envelope — globalny exception handler
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestErrorEnvelopeFormat:
    """Pojedynczy strukturalny test dla każdego głównego error code.

    Reszta testów powyżej tylko sprawdza `expected_code` — tutaj sprawdzamy
    pełen kształt: {"error": {"code", "message", "timestamp"}} z poprawnymi
    typami. Jeśli ktoś zmieni format ciała błędu, te testy odpalą alarm.
    """

    def test_404_envelope_complete(self, auth_client):
        response = auth_client.delete(_delete_url('+48999999999'))
        assert response.status_code == 404
        body = response.data
        assert set(body.keys()) == {'error'}
        assert set(body['error'].keys()) == {'code', 'message', 'timestamp'}
        assert body['error']['code'] == '404_ALIAS_NOT_FOUND'

    def test_409_envelope_complete(self, auth_client):
        auth_client.post(
            REGISTER_URL,
            data={
                'phone': '+48501234567',
                'iban': 'PL61109010140000071219812874',
                'zone': 'PL',
            },
            format='json',
        )
        response = auth_client.post(
            REGISTER_URL,
            data={
                'phone': '+48501234567',
                'iban': 'PL61109010140000071219812874',
                'zone': 'PL',
            },
            format='json',
        )
        assert response.status_code == 409
        assert response.data['error']['code'] == '409_ALIAS_ALREADY_EXISTS'
