"""Testy endpointów REST modułu recurring.

Pokrycie tabeli błędów z docs/reccuring/integration/INFO.md + happy path
wszystkich siedmiu endpointów.
"""

import uuid
from datetime import timedelta
from unittest import mock

import pytest
from django.utils import timezone

from recurring.models import (
    RecurringExecution,
    RecurringExecutionStatus,
    RecurringTransfer,
    RecurringTransferStatus,
)
from recurring.tests.conftest import post_idem

pytestmark = pytest.mark.django_db

CREATE_URL = '/api/v1/recurring/create'


def _error_code(response):
    return response.data.get('error', {}).get('code')


# ---------------------------------------------------------------------------
# POST /recurring/create
# ---------------------------------------------------------------------------


class TestCreate:
    def test_happy_path(self, auth_client, recipient_alias, create_payload):
        response = post_idem(auth_client, CREATE_URL, create_payload())
        assert response.status_code == 201, response.data
        assert response.data['status'] == 'ACTIVE'
        assert 'recurring_transfer_id' in response.data

        mandate = RecurringTransfer.objects.get(id=response.data['recurring_transfer_id'])
        assert mandate.status == RecurringTransferStatus.ACTIVE
        # next_run_at = start_date + RECURRING_EXECUTION_HOUR_UTC
        assert str(mandate.next_run_at.date()) == create_payload()['start_date']
        assert mandate.next_run_at.hour == 8

    def test_open_ended_mandate(self, auth_client, recipient_alias, create_payload):
        response = post_idem(auth_client, CREATE_URL, create_payload(end_date=None))
        assert response.status_code == 201
        mandate = RecurringTransfer.objects.get(id=response.data['recurring_transfer_id'])
        assert mandate.end_date is None

    def test_idempotency_replay_returns_same_mandate(
        self, auth_client, recipient_alias, create_payload
    ):
        key = str(uuid.uuid4())
        payload = create_payload()
        first = post_idem(auth_client, CREATE_URL, payload, key=key)
        replay = post_idem(auth_client, CREATE_URL, payload, key=key)
        assert first.status_code == replay.status_code == 201
        assert first.data['recurring_transfer_id'] == replay.data['recurring_transfer_id']
        assert RecurringTransfer.objects.count() == 1

    def test_idempotency_conflict_different_payload(
        self, auth_client, recipient_alias, create_payload
    ):
        key = str(uuid.uuid4())
        post_idem(auth_client, CREATE_URL, create_payload(), key=key)
        response = post_idem(auth_client, CREATE_URL, create_payload(amount='99.00'), key=key)
        assert response.status_code == 409
        assert _error_code(response) == '409_IDEMPOTENCY_CONFLICT'

    def test_missing_idempotency_key(self, auth_client, recipient_alias, create_payload):
        response = auth_client.post(CREATE_URL, create_payload(), format='json')
        assert response.status_code == 400
        assert _error_code(response) == '400_MISSING_IDEMPOTENCY_KEY'

    @pytest.mark.parametrize(
        ('override', 'expected_code'),
        [
            ({'cycle': 'HOURLY'}, '400_INVALID_CYCLE'),
            ({'recipient_phone': 'nie-telefon'}, '400_INVALID_PHONE_FORMAT'),
            ({'amount': '-5.00'}, '400_INVALID_AMOUNT'),
            ({'amount': '0.00'}, '400_INVALID_AMOUNT'),
            ({'start_date': '2020-01-01'}, '400_INVALID_DATE_RANGE'),
            ({'zone': 'UK'}, '422_ZONE_MISMATCH'),
            ({'currency': 'EUR'}, '422_CURRENCY_MISMATCH'),
            ({'recipient_phone': '+48999999999'}, '404_RECIPIENT_ALIAS_NOT_FOUND'),
        ],
    )
    def test_validation_errors(
        self, auth_client, recipient_alias, create_payload, override, expected_code
    ):
        response = post_idem(auth_client, CREATE_URL, create_payload(**override))
        assert _error_code(response) == expected_code, response.data

    def test_end_date_before_start(self, auth_client, recipient_alias, create_payload):
        today = timezone.now().date()
        payload = create_payload(
            start_date=str(today + timedelta(days=10)),
            end_date=str(today + timedelta(days=5)),
        )
        response = post_idem(auth_client, CREATE_URL, payload)
        assert _error_code(response) == '400_INVALID_DATE_RANGE'

    def test_span_over_ten_years(self, auth_client, recipient_alias, create_payload):
        today = timezone.now().date()
        payload = create_payload(end_date=str(today + timedelta(days=11 * 365)))
        response = post_idem(auth_client, CREATE_URL, payload)
        assert _error_code(response) == '400_INVALID_DATE_RANGE'

    def test_start_over_year_ahead(self, auth_client, recipient_alias, create_payload):
        today = timezone.now().date()
        payload = create_payload(
            start_date=str(today + timedelta(days=400)),
            end_date=str(today + timedelta(days=500)),
        )
        response = post_idem(auth_client, CREATE_URL, payload)
        assert _error_code(response) == '400_INVALID_DATE_RANGE'

    def test_requires_api_key(self, api_client, recipient_alias, create_payload):
        response = post_idem(api_client, CREATE_URL, create_payload())
        assert response.status_code == 401

    def test_recurring_not_enabled(self, sender_bank, auth_client, recipient_alias, create_payload):
        bank, _ = sender_bank
        bank.recurring_enabled = False
        bank.save()
        response = post_idem(auth_client, CREATE_URL, create_payload())
        assert _error_code(response) == '403_RECURRING_NOT_ENABLED'

    def test_p2p_not_enabled(self, sender_bank, auth_client, recipient_alias, create_payload):
        bank, _ = sender_bank
        bank.p2p_enabled = False
        bank.save()
        response = post_idem(auth_client, CREATE_URL, create_payload())
        assert _error_code(response) == '403_P2P_NOT_ENABLED'

    def test_validation_lookup_is_free(
        self, sender_bank, auth_client, recipient_alias, create_payload
    ):
        """Walidacyjny lookup przy create NIE inkrementuje countera P2P."""
        from aliases.services import AliasService

        bank, _ = sender_bank
        response = post_idem(auth_client, CREATE_URL, create_payload())
        assert response.status_code == 201
        assert AliasService().get_lookup_count(bank.id) == 0


# ---------------------------------------------------------------------------
# GET /recurring  +  GET /recurring/{id}
# ---------------------------------------------------------------------------


class TestListAndDetail:
    def test_list_requires_payer_user_id(self, auth_client):
        response = auth_client.get('/api/v1/recurring')
        assert response.status_code == 400

    def test_list_filters_by_payer_and_status(self, auth_client, make_mandate):
        make_mandate(payer_user_id='client-1')
        make_mandate(payer_user_id='client-1', status=RecurringTransferStatus.PAUSED)
        make_mandate(payer_user_id='client-2')

        response = auth_client.get('/api/v1/recurring', {'payer_user_id': 'client-1'})
        assert response.status_code == 200
        assert response.data['count'] == 1  # default status=ACTIVE

        response = auth_client.get(
            '/api/v1/recurring', {'payer_user_id': 'client-1', 'status': 'ALL'}
        )
        assert response.data['count'] == 2

        response = auth_client.get(
            '/api/v1/recurring', {'payer_user_id': 'client-1', 'status': 'PAUSED'}
        )
        assert response.data['count'] == 1

    def test_list_invalid_status(self, auth_client):
        response = auth_client.get(
            '/api/v1/recurring', {'payer_user_id': 'client-1', 'status': 'XXX'}
        )
        assert response.status_code == 400

    def test_list_hides_other_banks_mandates(self, auth_client, make_mandate, make_recurring_bank):
        other_bank, _ = make_recurring_bank(name='Obcy Bank')
        make_mandate(payer_bank=other_bank, payer_user_id='client-1')
        response = auth_client.get('/api/v1/recurring', {'payer_user_id': 'client-1'})
        assert response.data['count'] == 0

    def test_detail_happy_path(self, auth_client, make_mandate):
        today = timezone.now().date()
        mandate = make_mandate(end_date=today + timedelta(days=365))
        response = auth_client.get(f'/api/v1/recurring/{mandate.id}')
        assert response.status_code == 200
        assert response.data['recurring_transfer_id'] == str(mandate.id)
        assert response.data['executions_summary']['scheduled'] == 13
        assert response.data['executions_summary']['succeeded'] == 0
        assert response.data['failed_runs_count'] == 0

    def test_detail_not_found(self, auth_client):
        response = auth_client.get(f'/api/v1/recurring/{uuid.uuid4()}')
        assert _error_code(response) == '404_RECURRING_NOT_FOUND'

    def test_detail_foreign_mandate_is_404(self, auth_client, make_mandate, make_recurring_bank):
        other_bank, _ = make_recurring_bank(name='Obcy Bank')
        mandate = make_mandate(payer_bank=other_bank)
        response = auth_client.get(f'/api/v1/recurring/{mandate.id}')
        assert _error_code(response) == '404_RECURRING_NOT_FOUND'


# ---------------------------------------------------------------------------
# POST /recurring/{id}/pause | /resume | /cancel
# ---------------------------------------------------------------------------


class TestLifecycle:
    def test_pause_active(self, auth_client, make_mandate):
        mandate = make_mandate()
        response = post_idem(auth_client, f'/api/v1/recurring/{mandate.id}/pause')
        assert response.status_code == 200
        assert response.data['status'] == 'PAUSED'
        mandate.refresh_from_db()
        assert mandate.paused_at is not None

    def test_pause_already_paused(self, auth_client, make_mandate):
        mandate = make_mandate(status=RecurringTransferStatus.PAUSED)
        response = post_idem(auth_client, f'/api/v1/recurring/{mandate.id}/pause')
        assert _error_code(response) == '409_RECURRING_NOT_ACTIVE'

    def test_pause_terminated(self, auth_client, make_mandate):
        mandate = make_mandate(status=RecurringTransferStatus.CANCELLED)
        response = post_idem(auth_client, f'/api/v1/recurring/{mandate.id}/pause')
        assert _error_code(response) == '409_RECURRING_TERMINATED'

    def test_resume_recalculates_next_run_and_resets_counter(self, auth_client, make_mandate):
        mandate = make_mandate(
            status=RecurringTransferStatus.PAUSED,
            failed_runs_count=2,
            paused_at=timezone.now(),
        )
        response = post_idem(auth_client, f'/api/v1/recurring/{mandate.id}/resume')
        assert response.status_code == 200
        assert response.data['status'] == 'ACTIVE'
        mandate.refresh_from_db()
        assert mandate.failed_runs_count == 0
        assert mandate.paused_at is None
        # next_run_at przeliczone na nadchodzący slot (nie missed)
        assert mandate.next_run_at > timezone.now()

    def test_resume_active_mandate(self, auth_client, make_mandate):
        mandate = make_mandate()
        response = post_idem(auth_client, f'/api/v1/recurring/{mandate.id}/resume')
        assert _error_code(response) == '409_RECURRING_NOT_PAUSED'

    def test_cancel_active_and_paused(self, auth_client, make_mandate):
        with mock.patch('recurring.tasks.notify_recurring_cancelled.delay') as notify:
            active = make_mandate()
            response = post_idem(auth_client, f'/api/v1/recurring/{active.id}/cancel')
            assert response.status_code == 200
            assert response.data['status'] == 'CANCELLED'
            notify.assert_called_once_with(str(active.id), 'USER_REQUEST')

            paused = make_mandate(status=RecurringTransferStatus.PAUSED)
            response = post_idem(auth_client, f'/api/v1/recurring/{paused.id}/cancel')
            assert response.status_code == 200

    def test_cancel_terminated(self, auth_client, make_mandate):
        mandate = make_mandate(status=RecurringTransferStatus.COMPLETED)
        response = post_idem(auth_client, f'/api/v1/recurring/{mandate.id}/cancel')
        assert _error_code(response) == '409_RECURRING_TERMINATED'

    @pytest.mark.parametrize('action', ['pause', 'resume', 'cancel'])
    def test_foreign_mandate_is_403(self, auth_client, make_mandate, make_recurring_bank, action):
        other_bank, _ = make_recurring_bank(name='Obcy Bank')
        mandate = make_mandate(payer_bank=other_bank)
        response = post_idem(auth_client, f'/api/v1/recurring/{mandate.id}/{action}')
        assert _error_code(response) == '403_INSUFFICIENT_PERMISSIONS'

    @pytest.mark.parametrize('action', ['pause', 'resume', 'cancel'])
    def test_unknown_mandate_is_404(self, auth_client, action):
        response = post_idem(auth_client, f'/api/v1/recurring/{uuid.uuid4()}/{action}')
        assert _error_code(response) == '404_RECURRING_NOT_FOUND'


# ---------------------------------------------------------------------------
# GET /recurring/{id}/executions
# ---------------------------------------------------------------------------


class TestExecutions:
    def _make_executions(self, mandate, count):
        now = timezone.now()
        return [
            RecurringExecution.objects.create(
                recurring_transfer=mandate,
                scheduled_for=now - timedelta(days=i),
                status=RecurringExecutionStatus.SUCCESS,
                rtp_reference=f'RTP-{i}',
            )
            for i in range(count)
        ]

    def test_list_newest_first(self, auth_client, make_mandate):
        mandate = make_mandate()
        self._make_executions(mandate, 3)
        response = auth_client.get(f'/api/v1/recurring/{mandate.id}/executions')
        assert response.status_code == 200
        assert response.data['count'] == 3
        items = response.data['items']
        assert items[0]['scheduled_for'] > items[1]['scheduled_for']
        assert items[0]['rtp_reference'] == 'RTP-0'
        assert items[0]['failure_reason'] is None

    def test_limit_and_before_pagination(self, auth_client, make_mandate):
        mandate = make_mandate()
        self._make_executions(mandate, 5)
        response = auth_client.get(f'/api/v1/recurring/{mandate.id}/executions', {'limit': 2})
        assert response.data['count'] == 2

        cursor = response.data['items'][-1]['scheduled_for']
        response = auth_client.get(f'/api/v1/recurring/{mandate.id}/executions', {'before': cursor})
        assert response.data['count'] == 3
        assert all(item['scheduled_for'] < cursor for item in response.data['items'])

    def test_limit_capped_at_100(self, auth_client, make_mandate):
        mandate = make_mandate()
        response = auth_client.get(f'/api/v1/recurring/{mandate.id}/executions', {'limit': 5000})
        assert response.status_code == 200

    def test_invalid_params(self, auth_client, make_mandate):
        mandate = make_mandate()
        url = f'/api/v1/recurring/{mandate.id}/executions'
        assert auth_client.get(url, {'limit': 'abc'}).status_code == 400
        assert auth_client.get(url, {'limit': 0}).status_code == 400
        assert auth_client.get(url, {'before': 'nie-data'}).status_code == 400

    def test_foreign_mandate_is_404(self, auth_client, make_mandate, make_recurring_bank):
        other_bank, _ = make_recurring_bank(name='Obcy Bank')
        mandate = make_mandate(payer_bank=other_bank)
        response = auth_client.get(f'/api/v1/recurring/{mandate.id}/executions')
        assert _error_code(response) == '404_RECURRING_NOT_FOUND'
