"""Testy modeli Agent i MSCAgreement."""

from datetime import timedelta
from decimal import Decimal

import pytest
from banks.models import Bank
from django.core.exceptions import ValidationError
from django.db.utils import IntegrityError
from django.utils import timezone

from agents.models import Agent, MSCAgreement
from common.enums import Zone


@pytest.fixture
def bank_pl(db):
    return Bank.objects.create(
        name='Bank PL',
        api_key_hash='dummy_hash_bank_pl',  # pragma: allowlist secret
        zone=Zone.PL,
        currency='PLN',
        debt_limit=Decimal('1000000.00'),
        webhook_url='https://bank-pl.example.com/webhook',
    )


@pytest.fixture
def bank_uk(db):
    return Bank.objects.create(
        name='Bank UK',
        api_key_hash='dummy_hash_bank_uk',  # pragma: allowlist secret
        zone=Zone.UK,
        currency='GBP',
        debt_limit=Decimal('1000000.00'),
        webhook_url='https://bank-uk.example.com/webhook',
    )


@pytest.fixture
def agent_pl(db, bank_pl):
    return Agent.objects.create(
        name='Agent Test',
        api_key_hash='dummy_hash_agent_pl',  # pragma: allowlist secret
        settlement_bank=bank_pl,
        iban='PL61109010140000071219812874',
        zone=Zone.PL,
    )


@pytest.mark.django_db
class TestAgentModel:
    def test_create_agent(self, bank_pl):
        agent = Agent.objects.create(
            name='New Agent',
            api_key_hash='hash123',  # pragma: allowlist secret
            settlement_bank=bank_pl,
            iban='PL61109010140000071219812874',
            zone=Zone.PL,
        )
        assert agent.id is not None
        assert agent.active is True

    def test_zone_must_match_settlement_bank_zone(self, bank_uk):
        agent = Agent(
            name='Mismatch Agent',
            api_key_hash='hash456',  # pragma: allowlist secret
            settlement_bank=bank_uk,
            iban='PL61109010140000071219812874',
            zone=Zone.PL,
        )
        with pytest.raises(ValidationError, match='Strefa agenta'):
            agent.save()

    def test_str_representation(self, agent_pl):
        assert str(agent_pl) == 'Agent Test (PL)'


@pytest.mark.django_db
class TestMSCAgreementModel:
    def test_create_msc(self, agent_pl):
        msc = MSCAgreement.objects.create(
            agent=agent_pl,
            klik_fee_perc=Decimal('0.30'),
            agent_fee_perc=Decimal('1.00'),
            valid_from=timezone.now(),
        )
        assert msc.id is not None

    def test_fees_sum_must_be_below_100(self, agent_pl):
        msc = MSCAgreement(
            agent=agent_pl,
            klik_fee_perc=Decimal('60.00'),
            agent_fee_perc=Decimal('50.00'),
            valid_from=timezone.now(),
        )
        with pytest.raises(ValidationError, match='Suma prowizji'):
            msc.save()

    def test_valid_to_must_be_after_valid_from(self, agent_pl):
        now = timezone.now()
        msc = MSCAgreement(
            agent=agent_pl,
            klik_fee_perc=Decimal('0.30'),
            agent_fee_perc=Decimal('1.00'),
            valid_from=now,
            valid_to=now - timedelta(days=1),
        )
        with pytest.raises(
            (ValidationError, IntegrityError)
        ):  # IntegrityError albo ValidationError
            msc.save()

    def test_overlap_detection(self, agent_pl):
        now = timezone.now()
        # Pierwsza umowa: dziś do za 30 dni
        MSCAgreement.objects.create(
            agent=agent_pl,
            klik_fee_perc=Decimal('0.30'),
            agent_fee_perc=Decimal('1.00'),
            valid_from=now,
            valid_to=now + timedelta(days=30),
        )
        # Druga umowa próbuje nakładać się: za 15 dni do za 45 dni
        msc2 = MSCAgreement(
            agent=agent_pl,
            klik_fee_perc=Decimal('0.40'),
            agent_fee_perc=Decimal('1.20'),
            valid_from=now + timedelta(days=15),
            valid_to=now + timedelta(days=45),
        )
        with pytest.raises(ValidationError, match='nakłada się'):
            msc2.save()

    def test_no_overlap_when_consecutive(self, agent_pl):
        now = timezone.now()
        MSCAgreement.objects.create(
            agent=agent_pl,
            klik_fee_perc=Decimal('0.30'),
            agent_fee_perc=Decimal('1.00'),
            valid_from=now,
            valid_to=now + timedelta(days=30),
        )
        # Następna zaczyna dokładnie gdzie poprzednia kończy — OK
        msc2 = MSCAgreement.objects.create(
            agent=agent_pl,
            klik_fee_perc=Decimal('0.40'),
            agent_fee_perc=Decimal('1.20'),
            valid_from=now + timedelta(days=30),
            valid_to=now + timedelta(days=60),
        )
        assert msc2.id is not None

    def test_is_active_at(self, agent_pl):
        now = timezone.now()
        msc = MSCAgreement.objects.create(
            agent=agent_pl,
            klik_fee_perc=Decimal('0.30'),
            agent_fee_perc=Decimal('1.00'),
            valid_from=now - timedelta(days=1),
            valid_to=now + timedelta(days=1),
        )
        assert msc.is_active_at(now) is True
        assert msc.is_active_at(now - timedelta(days=2)) is False
        assert msc.is_active_at(now + timedelta(days=2)) is False

    def test_is_active_at_with_null_valid_to(self, agent_pl):
        msc = MSCAgreement.objects.create(
            agent=agent_pl,
            klik_fee_perc=Decimal('0.30'),
            agent_fee_perc=Decimal('1.00'),
            valid_from=timezone.now() - timedelta(days=1),
            valid_to=None,
        )
        assert msc.is_active_at(timezone.now() + timedelta(days=365)) is True
