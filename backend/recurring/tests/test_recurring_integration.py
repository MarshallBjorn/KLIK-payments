"""Testy integracyjne modułu Recurring — pełny workflow przez realne granice.

W odróżnieniu od test_views.py (endpointy w izolacji) i test_tasks.py
(`execute_recurring_run` wołany wprost na mandacie z `make_mandate`), tutaj
przechodzimy CAŁY przepływ zlecenia stałego tak, jak dzieje się w produkcji:

    POST /recurring/create            (bank, realny HTTP + walidacyjny lookup)
        → dispatch_due_recurring_transfers()   (cron Beat — wybiera due)
            → execute_recurring_run.delay()    (Celery eager, synchronicznie)
                → AliasService.lookup_for_bank  (PŁATNY — counter P2P +1 w Redis)
                → webhook POST {bank}/recurring/execute   (JEDYNY mock — httpx.post)
                → finalize (SUCCESS / FAILED + klasyfikacja reason)
        → GET /recurring/{id}                  (executions_summary)
        → GET /recurring/{id}/executions       (historia runów)
        → POST /recurring/{id}/pause|resume|cancel   (lifecycle przez HTTP)

Granica integracji (jak w codes/tests/test_c2b_integration.py): realny Postgres
+ Redis + Celery eager. JEDYNYM mockiem jest wychodzące HTTP do banku
(`recurring.tasks.httpx.post`) — dzięki temu pokrywamy budowę payloadu webhooka,
naliczanie lookup fee, claim/advance next_run_at i spójność stanu mandate-a
end-to-end, czego testy komponentowe nie dotykają razem.

Fixturki (sender_bank, recipient_alias, auth_client, create_payload, ...)
pochodzą z recurring/tests/conftest.py.
"""

import uuid
from datetime import timedelta
from decimal import Decimal
from unittest import mock

import pytest
from django.conf import settings
from django.utils import timezone

from aliases.models import Alias
from aliases.services import AliasService
from recurring import tasks
from recurring.models import (
    ExecutionFailureReason,
    RecurringExecution,
    RecurringExecutionStatus,
    RecurringTransfer,
    RecurringTransferStatus,
)

pytestmark = pytest.mark.django_db

CREATE_URL = '/api/v1/recurring/create'

EXECUTED_RESPONSE = {
    'status': 'EXECUTED',
    'rtp_reference': 'ELIXIR-EXP-12345',
    'executed_at': '2026-06-01T08:02:13Z',
}


# ---------------------------------------------------------------------------
# Fixtures lokalne
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def celery_eager(settings):
    """Celery synchronicznie — `task.delay()` wykonuje się w wątku testu, więc
    dispatch → execute → notify lecą w jednym wywołaniu (jak realny worker)."""
    settings.CELERY_TASK_ALWAYS_EAGER = True
    settings.CELERY_TASK_EAGER_PROPAGATES = True
    yield


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_response(json_data, status_code=200):
    response = mock.Mock()
    response.status_code = status_code
    response.json.return_value = json_data
    response.raise_for_status.return_value = None
    return response


def _rejected(reason):
    return {'status': 'REJECTED', 'reject_reason': reason}


def _webhook_router(execute_response):
    """side_effect dla httpx.post: routing po sufiksie URL.

    /execute → odpowiedź banku na run; /auto-paused i /cancelled → zwykłe ACK.
    Pozwala w jednym dispatchu obsłużyć run (reject) + następujący po nim
    webhook powiadamiający (auto-paused / cancelled).
    """

    def _side_effect(url, *args, **kwargs):
        if url.endswith('/execute'):
            return _fake_response(execute_response)
        return _fake_response({'received': True})

    return _side_effect


def _create(auth_client, create_payload, **over):
    resp = auth_client.post(
        CREATE_URL,
        create_payload(**over),
        format='json',
        headers={'Idempotency-Key': str(uuid.uuid4())},
    )
    assert resp.status_code == 201, resp.data
    return resp.data


def _make_due(mandate_id):
    """Symuluje nadejście slotu — przesuwa next_run_at w przeszłość, żeby
    następny dispatch potraktował mandate jako due."""
    RecurringTransfer.objects.filter(id=mandate_id).update(
        next_run_at=timezone.now() - timedelta(minutes=1)
    )


def _run_due_cycle(mandate_id, *, execute_response=EXECUTED_RESPONSE):
    """Jeden tick crona dla danego mandate-a: make-due → dispatch → (eager)
    execute + ewentualny webhook powiadamiający. Zwraca mock httpx.post."""
    _make_due(mandate_id)
    with mock.patch.object(
        tasks.httpx, 'post', side_effect=_webhook_router(execute_response)
    ) as post:
        tasks.dispatch_due_recurring_transfers()
    return post


def _lifecycle(auth_client, mandate_id, action):
    """POST /recurring/{id}/{action} z wymaganym Idempotency-Key.

    Mockuje httpx.post, bo cancel wysyła webhook /cancelled (eager)."""
    with mock.patch.object(tasks.httpx, 'post', return_value=_fake_response({'received': True})):
        return auth_client.post(
            f'/api/v1/recurring/{mandate_id}/{action}',
            {},
            format='json',
            headers={'Idempotency-Key': str(uuid.uuid4())},
        )


def _execute_calls(post_mock):
    """Wywołania httpx.post kierowane na webhook /execute."""
    return [c for c in post_mock.call_args_list if c.args and c.args[0].endswith('/execute')]


def _calls_ending(post_mock, suffix):
    return [c for c in post_mock.call_args_list if c.args and c.args[0].endswith(suffix)]


# ---------------------------------------------------------------------------
# Happy path — pełny łańcuch
# ---------------------------------------------------------------------------


class TestRecurringWorkflowHappyPath:
    def test_create_dispatch_execute_full_chain(
        self, auth_client, sender_bank, recipient_alias, create_payload
    ):
        """create (HTTP) → cron dispatch → execute (eager) → webhook → finalize.

        Sprawdza całą trasę: status mandate, payload webhooka do banku, zapis
        execution SUCCESS, naliczenie lookup fee i widoczność w GET detail /
        executions.
        """
        bank, _ = sender_bank

        data = _create(auth_client, create_payload)
        rid = data['recurring_transfer_id']
        assert data['status'] == RecurringTransferStatus.ACTIVE

        post = _run_due_cycle(rid)

        # Dokładnie jeden webhook /execute do banku nadawcy.
        execute_calls = _execute_calls(post)
        assert len(execute_calls) == 1
        url = execute_calls[0].args[0]
        assert url == 'https://bank.example.com/webhook/recurring/execute'

        # Kontrakt payloadu (to konsumuje mock-bank w /webhook/recurring/execute).
        payload = execute_calls[0].kwargs['json']
        assert payload['recurring_transfer_id'] == str(rid)
        assert uuid.UUID(payload['execution_id'])  # poprawny UUID
        assert Decimal(payload['amount']) == Decimal('50.00')
        assert payload['currency'] == 'PLN'
        assert payload['recipient']['phone'] == recipient_alias.phone
        assert payload['recipient']['account_identifier']['type'] == 'iban'

        # Execution zapisany jako SUCCESS z danymi od banku.
        execution = RecurringExecution.objects.get(recurring_transfer_id=rid)
        assert execution.status == RecurringExecutionStatus.SUCCESS
        assert execution.rtp_reference == 'ELIXIR-EXP-12345'
        assert str(execution.id) == payload['execution_id']

        # Mandate: zarejestrowany run + przesunięty slot (claim advance).
        mandate = RecurringTransfer.objects.get(id=rid)
        assert mandate.status == RecurringTransferStatus.ACTIVE
        assert mandate.last_run_at is not None
        assert mandate.failed_runs_count == 0
        assert mandate.next_run_at > execution.scheduled_for

        # Lookup był PŁATNY — counter P2P banku nadawcy +1.
        assert AliasService().get_lookup_count(bank.id) == 1

        # Widoczność przez API.
        detail = auth_client.get(f'/api/v1/recurring/{rid}')
        assert detail.status_code == 200
        assert detail.data['executions_summary']['succeeded'] == 1
        assert detail.data['executions_summary']['failed'] == 0

        execs = auth_client.get(f'/api/v1/recurring/{rid}/executions')
        assert execs.status_code == 200
        assert execs.data['count'] == 1
        item = execs.data['items'][0]
        assert item['status'] == RecurringExecutionStatus.SUCCESS
        assert item['rtp_reference'] == 'ELIXIR-EXP-12345'
        assert item['failure_reason'] is None

    def test_multi_cycle_advances_and_bills_each_run(
        self, auth_client, sender_bank, recipient_alias, create_payload
    ):
        """Trzy kolejne ticki crona: każdy = osobny run (nowy execution_id) +
        osobne naliczenie lookup fee. Mandate zostaje ACTIVE między cyklami."""
        bank, _ = sender_bank
        data = _create(auth_client, create_payload, cycle='DAILY')
        rid = data['recurring_transfer_id']

        for expected_count in (1, 2, 3):
            post = _run_due_cycle(rid)
            assert len(_execute_calls(post)) == 1
            assert (
                RecurringExecution.objects.filter(
                    recurring_transfer_id=rid,
                    status=RecurringExecutionStatus.SUCCESS,
                ).count()
                == expected_count
            )
            assert RecurringTransfer.objects.get(id=rid).status == RecurringTransferStatus.ACTIVE

        # Trzy osobne wykonania → trzy naliczone (płatne) lookupy P2P.
        assert RecurringExecution.objects.filter(recurring_transfer_id=rid).count() == 3
        assert AliasService().get_lookup_count(bank.id) == 3


# ---------------------------------------------------------------------------
# Ścieżki odrzuceń i klasyfikacja reason
# ---------------------------------------------------------------------------


class TestRecurringWorkflowFailures:
    def test_insufficient_funds_rejects_but_keeps_mandate_active(
        self, auth_client, sender_bank, recipient_alias, create_payload
    ):
        """Bank odrzuca run (brak środków) — execution FAILED, ale mandate dalej
        ACTIVE i lookup PŁATNY (KLIK naliczył opłatę mimo rejectu)."""
        bank, _ = sender_bank
        rid = _create(auth_client, create_payload)['recurring_transfer_id']

        post = _run_due_cycle(rid, execute_response=_rejected('INSUFFICIENT_FUNDS'))
        assert len(_execute_calls(post)) == 1

        execution = RecurringExecution.objects.get(recurring_transfer_id=rid)
        assert execution.status == RecurringExecutionStatus.FAILED
        assert execution.failure_reason == ExecutionFailureReason.INSUFFICIENT_FUNDS

        mandate = RecurringTransfer.objects.get(id=rid)
        assert mandate.status == RecurringTransferStatus.ACTIVE
        assert mandate.failed_runs_count == 1
        assert AliasService().get_lookup_count(bank.id) == 1  # reject też billowany

    def test_three_consecutive_failures_auto_pause_and_notify_bank(
        self, auth_client, sender_bank, recipient_alias, create_payload
    ):
        """Próg failów (default 3) → mandate PAUSED + webhook /auto-paused do banku."""
        threshold = settings.RECURRING_AUTO_PAUSE_FAILURE_THRESHOLD
        rid = _create(auth_client, create_payload)['recurring_transfer_id']

        last_post = None
        for _ in range(threshold):
            last_post = _run_due_cycle(rid, execute_response=_rejected('INSUFFICIENT_FUNDS'))

        mandate = RecurringTransfer.objects.get(id=rid)
        assert mandate.status == RecurringTransferStatus.PAUSED
        assert mandate.failed_runs_count == threshold

        # Ostatni dispatch po przekroczeniu progu wysłał webhook /auto-paused.
        paused_calls = _calls_ending(last_post, '/auto-paused')
        assert len(paused_calls) == 1
        notify_payload = paused_calls[0].kwargs['json']
        assert notify_payload['recurring_transfer_id'] == str(rid)
        assert notify_payload['failed_runs_count'] == threshold

    def test_bank_revokes_locally_cancels_mandate_and_notifies(
        self, auth_client, sender_bank, recipient_alias, create_payload
    ):
        """Bank zwraca MANDATE_REVOKED_LOCALLY → KLIK od razu CANCELLED mandate
        i wysyła webhook /cancelled z tym reason."""
        rid = _create(auth_client, create_payload)['recurring_transfer_id']

        post = _run_due_cycle(rid, execute_response=_rejected('MANDATE_REVOKED_LOCALLY'))

        mandate = RecurringTransfer.objects.get(id=rid)
        assert mandate.status == RecurringTransferStatus.CANCELLED
        assert mandate.cancelled_at is not None

        cancelled_calls = _calls_ending(post, '/cancelled')
        assert len(cancelled_calls) == 1
        assert cancelled_calls[0].kwargs['json']['reason'] == 'MANDATE_REVOKED_LOCALLY'

    def test_recipient_alias_gone_fails_without_billing_or_webhook(
        self, auth_client, sender_bank, recipient_alias, create_payload
    ):
        """Alias odbiorcy usunięty po create — run FAILED(RECIPIENT_ALIAS_GONE),
        bez webhooka do banku i bez naliczenia lookupu (miss = 404)."""
        bank, _ = sender_bank
        rid = _create(auth_client, create_payload)['recurring_transfer_id']

        Alias.objects.filter(phone=recipient_alias.phone).delete()

        post = _run_due_cycle(rid)

        post.assert_not_called()  # webhook do banku NIE wyszedł
        execution = RecurringExecution.objects.get(recurring_transfer_id=rid)
        assert execution.status == RecurringExecutionStatus.FAILED
        assert execution.failure_reason == ExecutionFailureReason.RECIPIENT_ALIAS_GONE
        assert AliasService().get_lookup_count(bank.id) == 0


# ---------------------------------------------------------------------------
# Lifecycle przez HTTP + interakcja z dispatch
# ---------------------------------------------------------------------------


class TestRecurringWorkflowLifecycle:
    def test_paused_mandate_is_not_dispatched(
        self, auth_client, sender_bank, recipient_alias, create_payload
    ):
        rid = _create(auth_client, create_payload)['recurring_transfer_id']

        paused = _lifecycle(auth_client, rid, 'pause')
        assert paused.status_code == 200
        assert paused.data['status'] == RecurringTransferStatus.PAUSED

        post = _run_due_cycle(rid)  # mimo due — PAUSED nie jest queued
        post.assert_not_called()
        assert RecurringExecution.objects.filter(recurring_transfer_id=rid).count() == 0

    def test_pause_then_resume_then_runs_again(
        self, auth_client, sender_bank, recipient_alias, create_payload
    ):
        rid = _create(auth_client, create_payload, cycle='DAILY')['recurring_transfer_id']

        # Run #1 — sukces.
        _run_due_cycle(rid)
        assert RecurringExecution.objects.filter(recurring_transfer_id=rid).count() == 1

        assert _lifecycle(auth_client, rid, 'pause').status_code == 200
        resumed = _lifecycle(auth_client, rid, 'resume')
        assert resumed.status_code == 200
        assert resumed.data['status'] == RecurringTransferStatus.ACTIVE
        assert RecurringTransfer.objects.get(id=rid).failed_runs_count == 0

        # Run #2 — po wznowieniu znowu się wykonuje.
        _run_due_cycle(rid)
        assert (
            RecurringExecution.objects.filter(
                recurring_transfer_id=rid, status=RecurringExecutionStatus.SUCCESS
            ).count()
            == 2
        )

    def test_cancel_is_terminal_and_blocks_dispatch_and_resume(
        self, auth_client, sender_bank, recipient_alias, create_payload
    ):
        rid = _create(auth_client, create_payload)['recurring_transfer_id']

        cancelled = _lifecycle(auth_client, rid, 'cancel')
        assert cancelled.status_code == 200
        assert cancelled.data['status'] == RecurringTransferStatus.CANCELLED

        # Po cancelu dispatch nic nie robi…
        post = _run_due_cycle(rid)
        post.assert_not_called()
        assert RecurringExecution.objects.filter(recurring_transfer_id=rid).count() == 0

        # …a pause/resume na terminalnym mandacie → 409.
        blocked = _lifecycle(auth_client, rid, 'pause')
        assert blocked.status_code == 409
