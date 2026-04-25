"""Testy XKlikAgentApiKeyAuthentication."""

from decimal import Decimal

import pytest
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.test import APIRequestFactory

from agents.authentication import (
    XKlikAgentApiKeyAuthentication,
    generate_api_key,
    hash_api_key,
)
from agents.models import Agent
from banks.models import Bank
from common.enums import Zone


@pytest.fixture
def factory():
    return APIRequestFactory()


@pytest.fixture
def auth():
    return XKlikAgentApiKeyAuthentication()


@pytest.fixture
def bank(db):
    return Bank.objects.create(
        name='Bank PL',
        api_key_hash='bank_hash',  # pragma: allowlist secret
        zone=Zone.PL,
        currency='PLN',
        debt_limit=Decimal('1000000.00'),
        webhook_url='https://bank.example.com/webhook',
    )


@pytest.fixture
def agent_with_key(db, bank):
    plaintext, hash_value = generate_api_key()
    agent = Agent.objects.create(
        name='Agent',
        api_key_hash=hash_value,
        settlement_bank=bank,
        iban='PL61109010140000071219812874',
        zone=Zone.PL,
    )
    return agent, plaintext


@pytest.mark.django_db
class TestAuthentication:
    def test_authenticate_valid_key(self, factory, auth, agent_with_key):
        agent, plaintext = agent_with_key
        request = factory.get('/', HTTP_X_KLIK_AGENT_API_KEY=plaintext)
        result = auth.authenticate(request)
        assert result is not None
        authenticated_agent, _ = result
        assert authenticated_agent.id == agent.id

    def test_authenticate_no_header_returns_none(self, factory, auth):
        request = factory.get('/')
        result = auth.authenticate(request)
        assert result is None

    def test_authenticate_invalid_key_raises(self, factory, auth, agent_with_key):
        request = factory.get('/', HTTP_X_KLIK_AGENT_API_KEY='wrong_key')
        with pytest.raises(AuthenticationFailed, match='Niepoprawny'):
            auth.authenticate(request)

    def test_authenticate_inactive_agent_raises(self, factory, auth, agent_with_key):
        agent, plaintext = agent_with_key
        agent.active = False
        agent.save()

        request = factory.get('/', HTTP_X_KLIK_AGENT_API_KEY=plaintext)
        with pytest.raises(AuthenticationFailed, match='nieaktywny'):
            auth.authenticate(request)


def test_hash_api_key_deterministic():
    key = 'test_key_123'
    assert hash_api_key(key) == hash_api_key(key)


def test_generate_api_key_returns_tuple():
    plaintext, hash_value = generate_api_key()
    assert plaintext.startswith('agent_')
    assert hash_value == hash_api_key(plaintext)
    assert plaintext != hash_value
