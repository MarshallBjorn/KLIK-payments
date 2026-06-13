"""Testy modeli recurring — RecurringTransfer + RecurringExecution."""

import uuid

import pytest
from django.db import IntegrityError
from django.utils import timezone

from recurring.models import (
    RecurringExecution,
    RecurringExecutionStatus,
    RecurringTransferStatus,
)

pytestmark = pytest.mark.django_db


class TestRecurringTransfer:
    def test_defaults(self, make_mandate):
        mandate = make_mandate()
        assert mandate.status == RecurringTransferStatus.ACTIVE
        assert mandate.is_active
        assert mandate.failed_runs_count == 0
        assert mandate.last_run_at is None
        assert mandate.last_execution is None

    def test_str(self, make_mandate):
        mandate = make_mandate()
        text = str(mandate)
        assert 'ACTIVE' in text
        assert 'MONTHLY' in text

    def test_idempotency_unique_per_bank(self, make_mandate):
        key = str(uuid.uuid4())
        make_mandate(idempotency_key=key)
        with pytest.raises(IntegrityError):
            make_mandate(idempotency_key=key)

    def test_same_idempotency_key_different_banks_ok(
        self, make_mandate, make_recurring_bank, recipient_alias
    ):
        key = str(uuid.uuid4())
        make_mandate(idempotency_key=key)
        other_bank, _ = make_recurring_bank(name='Inny Bank')
        # Ten sam klucz, inny bank — dozwolone (unique jest per (key, bank)).
        make_mandate(idempotency_key=key, payer_bank=other_bank)


class TestEffectiveWebhookUrl:
    def test_dedicated_recurring_url_wins(self, make_recurring_bank, make_mandate):
        bank, _ = make_recurring_bank(
            name='Bank Dedykowany',
            webhook_url='https://bank.example.com/webhook',
            recurring_webhook_url='https://bank.example.com/special/recurring/',
        )
        mandate = make_mandate(payer_bank=bank)
        # rstrip('/') — bez podwójnego slasha przy budowie {url}/execute
        assert mandate.get_effective_webhook_url() == 'https://bank.example.com/special/recurring'

    def test_fallback_to_webhook_url_plus_recurring(self, make_mandate):
        mandate = make_mandate()
        assert mandate.get_effective_webhook_url() == 'https://bank.example.com/webhook/recurring'

    def test_no_urls_returns_none(self, make_recurring_bank, make_mandate):
        bank, _ = make_recurring_bank(name='Bank Bez Webhooka', webhook_url='')
        mandate = make_mandate(payer_bank=bank)
        assert mandate.get_effective_webhook_url() is None


class TestRecurringExecution:
    def test_create_and_str(self, make_mandate):
        mandate = make_mandate()
        execution = RecurringExecution.objects.create(
            recurring_transfer=mandate,
            scheduled_for=timezone.now(),
        )
        assert execution.status == RecurringExecutionStatus.SCHEDULED
        assert str(mandate.id) in str(execution)

    def test_cascade_delete_with_mandate(self, make_mandate):
        mandate = make_mandate()
        RecurringExecution.objects.create(
            recurring_transfer=mandate,
            scheduled_for=timezone.now(),
        )
        mandate.delete()
        assert RecurringExecution.objects.count() == 0
