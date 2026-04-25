"""Testy AgentService."""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from agents.exceptions import NoActiveMSCAgreementError
from agents.models import Agent, MSCAgreement
from agents.services import AgentService
from banks.models import Bank
from common.enums import Zone


@pytest.fixture
def bank(db):
    return Bank.objects.create(
        name='Bank PL',
        api_key_hash='dummy_hash',  # pragma: allowlist secret
        zone=Zone.PL,
        currency='PLN',
        debt_limit=Decimal('1000000.00'),
        webhook_url='https://bank.example.com/webhook',
    )


@pytest.fixture
def agent(db, bank):
    return Agent.objects.create(
        name='Agent Test',
        api_key_hash='hash',  # pragma: allowlist secret
        settlement_bank=bank,
        iban='PL61109010140000071219812874',
        zone=Zone.PL,
    )


@pytest.fixture
def active_msc(db, agent):
    return MSCAgreement.objects.create(
        agent=agent,
        klik_fee_perc=Decimal('0.30'),
        agent_fee_perc=Decimal('1.00'),
        valid_from=timezone.now() - timedelta(hours=1),
        valid_to=timezone.now() + timedelta(days=30),
    )


@pytest.mark.django_db
class TestGetActiveMSC:
    def test_returns_active_msc(self, agent, active_msc):
        result = AgentService.get_active_msc(agent)
        assert result.id == active_msc.id

    def test_raises_when_no_msc(self, agent):
        with pytest.raises(NoActiveMSCAgreementError):
            AgentService.get_active_msc(agent)

    def test_raises_when_msc_expired(self, agent):
        MSCAgreement.objects.create(
            agent=agent,
            klik_fee_perc=Decimal('0.30'),
            agent_fee_perc=Decimal('1.00'),
            valid_from=timezone.now() - timedelta(days=10),
            valid_to=timezone.now() - timedelta(days=1),
        )
        with pytest.raises(NoActiveMSCAgreementError):
            AgentService.get_active_msc(agent)


@pytest.mark.django_db
class TestCalculateSplit:
    def test_split_for_150_pln(self, agent, active_msc):
        result = AgentService.calculate_split(agent, Decimal('150.00'))
        assert result['klik_fee'] == Decimal('0.45')
        assert result['agent_fee'] == Decimal('1.50')
        assert result['merchant_net'] == Decimal('148.05')

    def test_split_sums_to_gross(self, agent, active_msc):
        result = AgentService.calculate_split(agent, Decimal('100.00'))
        total = result['klik_fee'] + result['agent_fee'] + result['merchant_net']
        assert total == Decimal('100.00')

    def test_rounding(self, agent):
        # MSC z procentem który daje wartości wymagające zaokrąglenia
        MSCAgreement.objects.create(
            agent=agent,
            klik_fee_perc=Decimal('0.33'),
            agent_fee_perc=Decimal('1.67'),
            valid_from=timezone.now() - timedelta(hours=1),
        )
        result = AgentService.calculate_split(agent, Decimal('100.00'))
        # 0.33% z 100 = 0.33, 1.67% z 100 = 1.67
        assert result['klik_fee'] == Decimal('0.33')
        assert result['agent_fee'] == Decimal('1.67')
        assert result['merchant_net'] == Decimal('98.00')
