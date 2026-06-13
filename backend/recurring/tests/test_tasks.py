"""Testy tasków Celery modułu recurring.

Scenariusze z diagramów R2 (execution), R5 (auto-pause), R6 (end_date).
Webhook do banku mockowany przez httpx.post — nie strzelamy w sieć.
"""

from datetime import timedelta
from unittest import mock

import pytest
from celery.exceptions import Retry
from django.test import override_settings
from django.utils import timezone

from recurring import tasks
from recurring.models import (
    ExecutionFailureReason,
    RecurringExecution,
    RecurringExecutionStatus,
    RecurringTransferStatus,
)

pytestmark = pytest.mark.django_db


def _fake_response(json_data, status_code=200):
    response = mock.Mock()
    response.status_code = status_code
    response.json.return_value = json_data
    response.raise_for_status.return_value = None
    return response


EXECUTED_RESPONSE = {
    'status': 'EXECUTED',
    'rtp_reference': 'ELIXIR-EXP-12345',
    'executed_at': '2026-06-01T08:02:13Z',
}


def _make_due(make_mandate, **over):
    over.setdefault('next_run_at', timezone.now() - timedelta(minutes=1))
    return make_mandate(**over)


# ---------------------------------------------------------------------------
# dispatch_due_recurring_transfers
# ---------------------------------------------------------------------------


class TestDispatch:
    def test_queues_only_due_active_mandates(self, make_mandate):
        due = _make_due(make_mandate)
        _make_due(make_mandate, status=RecurringTransferStatus.PAUSED)
        make_mandate(next_run_at=timezone.now() + timedelta(days=1))

        with mock.patch.object(tasks.execute_recurring_run, 'delay') as delay:
            queued = tasks.dispatch_due_recurring_transfers()

        assert queued == 1
        delay.assert_called_once_with(str(due.id))

    def test_noop_when_nothing_due(self, make_mandate):
        make_mandate(next_run_at=timezone.now() + timedelta(days=1))
        with mock.patch.object(tasks.execute_recurring_run, 'delay') as delay:
            assert tasks.dispatch_due_recurring_transfers() == 0
        delay.assert_not_called()


# ---------------------------------------------------------------------------
# execute_recurring_run — happy path
# ---------------------------------------------------------------------------


class TestExecutionSuccess:
    def test_full_happy_path(self, make_mandate, recipient_alias, sender_bank):
        bank, _ = sender_bank
        mandate = _make_due(make_mandate, failed_runs_count=2)
        scheduled_for = mandate.next_run_at

        with mock.patch.object(
            tasks.httpx, 'post', return_value=_fake_response(EXECUTED_RESPONSE)
        ) as post:
            tasks.execute_recurring_run(str(mandate.id))

        mandate.refresh_from_db()
        execution = mandate.executions.get()

        # Execution: SUCCESS z danymi od banku + snapshot lookupu
        assert execution.status == RecurringExecutionStatus.SUCCESS
        assert execution.rtp_reference == 'ELIXIR-EXP-12345'
        assert execution.executed_at is not None
        assert execution.scheduled_for == scheduled_for
        assert execution.lookup_response_snapshot['phone'] == mandate.recipient_phone
        assert execution.lookup_response_snapshot['account_identifier']['type'] == 'iban'

        # Mandate: advance + reset failed_runs_count + last_*
        assert mandate.next_run_at > scheduled_for
        assert mandate.failed_runs_count == 0
        assert mandate.last_run_at is not None
        assert mandate.last_execution_id == execution.id
        assert mandate.status == RecurringTransferStatus.ACTIVE

        # Webhook: poprawny URL (fallback webhook_url + /recurring) i payload
        url = post.call_args[0][0]
        assert url == 'https://bank.example.com/webhook/recurring/execute'
        payload = post.call_args[1]['json']
        assert payload['recurring_transfer_id'] == str(mandate.id)
        assert payload['execution_id'] == str(execution.id)
        assert payload['amount'] == '50.00'
        assert payload['recipient']['bank_code'] == 'Bank Odbiorca'

    def test_lookup_increments_p2p_counter(self, make_mandate, recipient_alias, sender_bank):
        """Execution jest PŁATNA — counter P2P banku nadawcy +1 (klucz jak w P2P)."""
        from aliases.services import AliasService

        bank, _ = sender_bank
        mandate = _make_due(make_mandate)
        with mock.patch.object(tasks.httpx, 'post', return_value=_fake_response(EXECUTED_RESPONSE)):
            tasks.execute_recurring_run(str(mandate.id))
        assert AliasService().get_lookup_count(bank.id) == 1

    def test_rejected_execution_still_billed(self, make_mandate, recipient_alias, sender_bank):
        """Lookup wykonany → naliczamy, nawet jeśli bank potem odrzucił run."""
        from aliases.services import AliasService

        bank, _ = sender_bank
        mandate = _make_due(make_mandate)
        rejected = _fake_response({'status': 'REJECTED', 'reject_reason': 'INSUFFICIENT_FUNDS'})
        with mock.patch.object(tasks.httpx, 'post', return_value=rejected):
            tasks.execute_recurring_run(str(mandate.id))
        assert AliasService().get_lookup_count(bank.id) == 1

    def test_completion_after_last_run(self, make_mandate, recipient_alias):
        """R6 — next_run_at po runie > end_date → COMPLETED + webhook /cancelled."""
        today = timezone.now().date()
        mandate = _make_due(
            make_mandate,
            cycle='DAILY',
            start_date=today - timedelta(days=10),
            end_date=today,
        )
        with (
            mock.patch.object(tasks.httpx, 'post', return_value=_fake_response(EXECUTED_RESPONSE)),
            mock.patch.object(tasks.notify_recurring_cancelled, 'delay') as notify,
        ):
            tasks.execute_recurring_run(str(mandate.id))

        mandate.refresh_from_db()
        assert mandate.status == RecurringTransferStatus.COMPLETED
        assert mandate.executions.get().status == RecurringExecutionStatus.SUCCESS
        notify.assert_called_once_with(str(mandate.id), 'END_DATE_REACHED')


# ---------------------------------------------------------------------------
# execute_recurring_run — reject path i klasyfikacja
# ---------------------------------------------------------------------------


class TestExecutionFailure:
    def _run_rejected(self, mandate, reason):
        rejected = _fake_response({'status': 'REJECTED', 'reject_reason': reason})
        with mock.patch.object(tasks.httpx, 'post', return_value=rejected):
            tasks.execute_recurring_run(str(mandate.id))

    def test_insufficient_funds_increments_counter_mandate_continues(
        self, make_mandate, recipient_alias
    ):
        mandate = _make_due(make_mandate)
        self._run_rejected(mandate, 'INSUFFICIENT_FUNDS')
        mandate.refresh_from_db()
        assert mandate.status == RecurringTransferStatus.ACTIVE
        assert mandate.failed_runs_count == 1
        execution = mandate.executions.get()
        assert execution.status == RecurringExecutionStatus.FAILED
        assert execution.failure_reason == ExecutionFailureReason.INSUFFICIENT_FUNDS
        # Cykl idzie dalej — next_run_at przesunięty mimo faila
        assert mandate.next_run_at > timezone.now()

    @override_settings(RECURRING_AUTO_PAUSE_FAILURE_THRESHOLD=3)
    def test_auto_pause_after_three_consecutive_failures(self, make_mandate, recipient_alias):
        """R5 — trzeci FAILED z rzędu pauzuje mandate + webhook /auto-paused."""
        mandate = _make_due(make_mandate)
        with mock.patch.object(tasks.notify_recurring_auto_paused, 'delay') as notify:
            for _ in range(3):
                mandate.refresh_from_db()
                mandate.next_run_at = timezone.now() - timedelta(minutes=1)
                mandate.save(update_fields=['next_run_at'])
                self._run_rejected(mandate, 'INSUFFICIENT_FUNDS')

        mandate.refresh_from_db()
        assert mandate.status == RecurringTransferStatus.PAUSED
        assert mandate.failed_runs_count == 3
        assert mandate.paused_at is not None
        notify.assert_called_once_with(str(mandate.id), 'INSUFFICIENT_FUNDS')

    @override_settings(RECURRING_AUTO_PAUSE_FAILURE_THRESHOLD=3)
    def test_success_resets_streak(self, make_mandate, recipient_alias):
        """SUCCESS pomiędzy failami resetuje licznik — auto-pause liczy Z RZĘDU."""
        mandate = _make_due(make_mandate, failed_runs_count=2)
        with mock.patch.object(tasks.httpx, 'post', return_value=_fake_response(EXECUTED_RESPONSE)):
            tasks.execute_recurring_run(str(mandate.id))
        mandate.refresh_from_db()
        assert mandate.failed_runs_count == 0
        assert mandate.status == RecurringTransferStatus.ACTIVE

    @pytest.mark.parametrize('reason', ['MANDATE_REVOKED_LOCALLY', 'ACCOUNT_CLOSED'])
    def test_revoked_or_closed_cancels_mandate(self, make_mandate, recipient_alias, reason):
        mandate = _make_due(make_mandate)
        with mock.patch.object(tasks.notify_recurring_cancelled, 'delay') as notify:
            self._run_rejected(mandate, reason)
        mandate.refresh_from_db()
        assert mandate.status == RecurringTransferStatus.CANCELLED
        assert mandate.cancelled_at is not None
        notify.assert_called_once_with(str(mandate.id), reason)

    def test_aml_block_pauses_immediately(self, make_mandate, recipient_alias):
        """AML_BLOCK pauzuje od razu (bez czekania na threshold)."""
        mandate = _make_due(make_mandate)
        with mock.patch.object(tasks.notify_recurring_auto_paused, 'delay') as notify:
            self._run_rejected(mandate, 'AML_BLOCK')
        mandate.refresh_from_db()
        assert mandate.status == RecurringTransferStatus.PAUSED
        notify.assert_called_once_with(str(mandate.id), 'AML_BLOCK')

    def test_unknown_reject_reason_maps_to_other(self, make_mandate, recipient_alias):
        mandate = _make_due(make_mandate)
        self._run_rejected(mandate, 'COSMIC_RAYS')
        execution = mandate.executions.get()
        assert execution.failure_reason == ExecutionFailureReason.OTHER

    def test_recipient_alias_gone(self, make_mandate, sender_bank):
        """Alias usunięty między runami → FAILED bez naliczenia lookupu."""
        from aliases.services import AliasService

        bank, _ = sender_bank
        mandate = _make_due(make_mandate)  # celowo brak fixture recipient_alias
        with mock.patch.object(tasks.httpx, 'post') as post:
            tasks.execute_recurring_run(str(mandate.id))

        post.assert_not_called()  # webhook nie poszedł
        mandate.refresh_from_db()
        execution = mandate.executions.get()
        assert execution.status == RecurringExecutionStatus.FAILED
        assert execution.failure_reason == ExecutionFailureReason.RECIPIENT_ALIAS_GONE
        assert mandate.failed_runs_count == 1
        assert AliasService().get_lookup_count(bank.id) == 0


# ---------------------------------------------------------------------------
# execute_recurring_run — race'y i guardy
# ---------------------------------------------------------------------------


class TestExecutionGuards:
    def test_skips_paused_mandate_without_execution(self, make_mandate, recipient_alias):
        """Race z pause — pause wygrał, run nie wchodzi (R3)."""
        mandate = _make_due(make_mandate, status=RecurringTransferStatus.PAUSED)
        with mock.patch.object(tasks.httpx, 'post') as post:
            tasks.execute_recurring_run(str(mandate.id))
        post.assert_not_called()
        assert mandate.executions.count() == 0

    def test_stale_scheduled_marked_skipped(self, make_mandate, recipient_alias):
        """Wisząca SCHEDULED (crash workera) → SKIPPED gdy mandate nie-ACTIVE."""
        mandate = _make_due(make_mandate, status=RecurringTransferStatus.CANCELLED)
        stale = RecurringExecution.objects.create(
            recurring_transfer=mandate,
            scheduled_for=timezone.now(),
            status=RecurringExecutionStatus.SCHEDULED,
        )
        tasks.execute_recurring_run(str(mandate.id))
        stale.refresh_from_db()
        assert stale.status == RecurringExecutionStatus.SKIPPED
        assert stale.failure_reason == ''  # SKIPPED nie liczy się do auto-pause

    def test_not_due_yet_is_noop(self, make_mandate, recipient_alias):
        mandate = make_mandate(next_run_at=timezone.now() + timedelta(hours=1))
        with mock.patch.object(tasks.httpx, 'post') as post:
            tasks.execute_recurring_run(str(mandate.id))
        post.assert_not_called()
        assert mandate.executions.count() == 0

    def test_end_date_passed_completes_without_run(self, make_mandate, recipient_alias):
        """next_run_at za end_date (np. po serii failów) → COMPLETED bez runu."""
        today = timezone.now().date()
        mandate = _make_due(
            make_mandate,
            start_date=today - timedelta(days=60),
            end_date=today - timedelta(days=30),
        )
        with (
            mock.patch.object(tasks.httpx, 'post') as post,
            mock.patch.object(tasks.notify_recurring_cancelled, 'delay') as notify,
        ):
            tasks.execute_recurring_run(str(mandate.id))
        post.assert_not_called()
        mandate.refresh_from_db()
        assert mandate.status == RecurringTransferStatus.COMPLETED
        notify.assert_called_once_with(str(mandate.id), 'END_DATE_REACHED')


# ---------------------------------------------------------------------------
# Network failure — retry / NETWORK_TIMEOUT
# ---------------------------------------------------------------------------


class _FakeTask:
    """Atrapa bound-taska Celery do testów _call_bank_and_finalize."""

    def __init__(self, retries=0, max_retries=3):
        self.request = mock.Mock(retries=retries)
        self.max_retries = max_retries
        self.retry_call = None

    def retry(self, exc=None, countdown=None, kwargs=None):
        self.retry_call = {'countdown': countdown, 'kwargs': kwargs}
        raise Retry('retry requested')


class TestNetworkFailure:
    def _executing(self, make_mandate, recipient_alias):
        mandate = _make_due(make_mandate)
        execution = RecurringExecution.objects.create(
            recurring_transfer=mandate,
            scheduled_for=mandate.next_run_at,
            status=RecurringExecutionStatus.EXECUTING,
            lookup_response_snapshot={'phone': mandate.recipient_phone},
        )
        execution.recurring_transfer = mandate
        return mandate, execution

    def test_first_failure_schedules_retry_with_execution_id(self, make_mandate, recipient_alias):
        mandate, execution = self._executing(make_mandate, recipient_alias)
        task = _FakeTask(retries=0)
        with (
            mock.patch.object(tasks.httpx, 'post', side_effect=ConnectionError('boom')),
            pytest.raises(Retry),
        ):
            tasks._call_bank_and_finalize(task, execution, execution.lookup_response_snapshot)

        # Retry z tym samym execution_id (bank wykrywa duplikat) i backoff 5s
        assert task.retry_call['countdown'] == 5
        assert task.retry_call['kwargs']['execution_id'] == str(execution.id)
        execution.refresh_from_db()
        assert execution.status == RecurringExecutionStatus.EXECUTING

    def test_exhausted_retries_finalize_network_timeout(self, make_mandate, recipient_alias):
        mandate, execution = self._executing(make_mandate, recipient_alias)
        task = _FakeTask(retries=3, max_retries=3)
        with mock.patch.object(tasks.httpx, 'post', side_effect=ConnectionError('boom')):
            tasks._call_bank_and_finalize(task, execution, execution.lookup_response_snapshot)

        execution.refresh_from_db()
        mandate.refresh_from_db()
        assert execution.status == RecurringExecutionStatus.FAILED
        assert execution.failure_reason == ExecutionFailureReason.NETWORK_TIMEOUT
        assert mandate.failed_runs_count == 1

    def test_malformed_bank_response_is_retried(self, make_mandate, recipient_alias):
        """Odpowiedź bez EXECUTED/REJECTED traktujemy jak network failure."""
        mandate, execution = self._executing(make_mandate, recipient_alias)
        task = _FakeTask(retries=0)
        with (
            mock.patch.object(tasks.httpx, 'post', return_value=_fake_response({'status': 'WAT'})),
            pytest.raises(Retry),
        ):
            tasks._call_bank_and_finalize(task, execution, execution.lookup_response_snapshot)

    def test_retry_reuses_snapshot_without_new_lookup(
        self, make_mandate, recipient_alias, sender_bank
    ):
        """Retry NIE robi nowego lookupu — bank nie płaci drugi raz."""
        from aliases.services import AliasService

        bank, _ = sender_bank
        mandate, execution = self._executing(make_mandate, recipient_alias)
        with mock.patch.object(tasks.httpx, 'post', return_value=_fake_response(EXECUTED_RESPONSE)):
            tasks.execute_recurring_run(str(mandate.id), execution_id=str(execution.id))

        execution.refresh_from_db()
        assert execution.status == RecurringExecutionStatus.SUCCESS
        assert AliasService().get_lookup_count(bank.id) == 0

    def test_no_webhook_url_fails_immediately(
        self, make_recurring_bank, make_mandate, recipient_alias
    ):
        bank, _ = make_recurring_bank(name='Bank Bez Webhooka', webhook_url='')
        mandate = _make_due(make_mandate, payer_bank=bank)
        tasks.execute_recurring_run(str(mandate.id))
        execution = mandate.executions.get()
        assert execution.status == RecurringExecutionStatus.FAILED
        assert execution.failure_reason == ExecutionFailureReason.NETWORK_TIMEOUT


# ---------------------------------------------------------------------------
# Webhooki notyfikacyjne
# ---------------------------------------------------------------------------


class TestNotifyWebhooks:
    def test_auto_paused_payload(self, make_mandate):
        mandate = make_mandate(
            status=RecurringTransferStatus.PAUSED,
            failed_runs_count=3,
            paused_at=timezone.now(),
        )
        with mock.patch.object(
            tasks.httpx, 'post', return_value=_fake_response({'received': True})
        ) as post:
            tasks.notify_recurring_auto_paused(str(mandate.id), 'INSUFFICIENT_FUNDS')

        url = post.call_args[0][0]
        assert url == 'https://bank.example.com/webhook/recurring/auto-paused'
        payload = post.call_args[1]['json']
        assert payload['recurring_transfer_id'] == str(mandate.id)
        assert payload['failed_runs_count'] == 3
        assert payload['last_failure_reason'] == 'INSUFFICIENT_FUNDS'

    def test_cancelled_payload(self, make_mandate):
        mandate = make_mandate(
            status=RecurringTransferStatus.CANCELLED,
            cancelled_at=timezone.now(),
        )
        with mock.patch.object(
            tasks.httpx, 'post', return_value=_fake_response({'received': True})
        ) as post:
            tasks.notify_recurring_cancelled(str(mandate.id), 'USER_REQUEST')

        url = post.call_args[0][0]
        assert url == 'https://bank.example.com/webhook/recurring/cancelled'
        payload = post.call_args[1]['json']
        assert payload['reason'] == 'USER_REQUEST'
        assert payload['cancelled_at'] is not None

    def test_missing_mandate_is_noop(self):
        import uuid

        with mock.patch.object(tasks.httpx, 'post') as post:
            tasks.notify_recurring_auto_paused(str(uuid.uuid4()), 'OTHER')
            tasks.notify_recurring_cancelled(str(uuid.uuid4()), 'USER_REQUEST')
        post.assert_not_called()
