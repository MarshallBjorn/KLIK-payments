"""Testy RTGSDispatcher i gateway-ów.

Strategia testów:
- Dispatcher: in-process fake gateway (bez HTTP).
- HTTPRTGSGateway: mockowane requests z `requests_mock` lub patch.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import patch
from uuid import uuid4

import pytest
import requests

from ledger.rtgs import RTGSDispatcher, TransferRequest, TransferResult, TransferStatus
from ledger.rtgs.exceptions import RTGSUnavailableError, UnknownZoneError
from ledger.rtgs.gateway import RTGSGateway
from ledger.rtgs.gateways import (
    CHAPSGateway,
    FedNowGateway,
    SORBNET3Gateway,
    TARGET2Gateway,
)

# ----------------------------------------------------------------------
# Fake gateway dla testów dispatchera
# ----------------------------------------------------------------------


class FakeGateway(RTGSGateway):
    """Gateway in-memory — zwraca zaprogramowane odpowiedzi."""

    system_name = 'FAKE'

    def __init__(self, results: list[TransferResult] | None = None, healthy: bool = True):
        super().__init__(base_url='http://fake')
        self._results = results or []
        self._healthy = healthy
        self.calls: list[tuple] = []

    def settle(self, session_id, transfers):
        self.calls.append((session_id, list(transfers)))
        if self._results:
            return self._results
        # Default: SUCCESS dla wszystkich
        return [
            TransferResult(
                transfer_id=t.transfer_id,
                status=TransferStatus.SUCCESS,
                rtgs_reference=f'FAKE-{t.transfer_id.hex[:8]}',
            )
            for t in transfers
        ]

    def healthcheck(self):
        return self._healthy


# ----------------------------------------------------------------------
# Dispatcher
# ----------------------------------------------------------------------


class TestRTGSDispatcher:
    def test_dispatch_routes_by_zone(self):
        pl_gw = FakeGateway()
        eu_gw = FakeGateway()
        dispatcher = RTGSDispatcher({'PL': pl_gw, 'EU': eu_gw})

        transfers = [
            TransferRequest(
                transfer_id=uuid4(),
                from_bank_code='A',
                to_bank_code='B',
                amount=Decimal('100'),
                currency='PLN',
            )
        ]
        session_id = uuid4()
        results = dispatcher.dispatch('PL', session_id, transfers)

        assert len(results) == 1
        assert results[0].status == TransferStatus.SUCCESS
        # Tylko PL gateway dostał call
        assert len(pl_gw.calls) == 1
        assert len(eu_gw.calls) == 0

    def test_unknown_zone_raises(self):
        dispatcher = RTGSDispatcher({'PL': FakeGateway()})
        with pytest.raises(UnknownZoneError):
            dispatcher.dispatch('XX', uuid4(), [])

    def test_empty_transfers_returns_empty(self):
        gw = FakeGateway()
        dispatcher = RTGSDispatcher({'PL': gw})
        assert dispatcher.dispatch('PL', uuid4(), []) == []
        # Gateway nie został wywołany
        assert len(gw.calls) == 0

    def test_healthcheck_all_returns_per_zone(self):
        dispatcher = RTGSDispatcher(
            {
                'PL': FakeGateway(healthy=True),
                'EU': FakeGateway(healthy=False),
            }
        )
        result = dispatcher.healthcheck_all()
        assert result == {'PL': True, 'EU': False}

    def test_from_settings_uses_correct_classes(self, settings):
        settings.SORBNET3_URL = 'http://test:9000/sorbnet3'
        settings.TARGET2_URL = 'http://test:9000/target2'
        settings.CHAPS_URL = 'http://test:9000/chaps'
        settings.FEDNOW_URL = 'http://test:9000/fednow'
        settings.RTGS_TIMEOUT_SECONDS = 10

        dispatcher = RTGSDispatcher.from_settings()
        assert isinstance(dispatcher._gateways['PL'], SORBNET3Gateway)
        assert isinstance(dispatcher._gateways['EU'], TARGET2Gateway)
        assert isinstance(dispatcher._gateways['UK'], CHAPSGateway)
        assert isinstance(dispatcher._gateways['US'], FedNowGateway)


# ----------------------------------------------------------------------
# HTTP gateway — mock requests
# ----------------------------------------------------------------------


def _make_transfer():
    return TransferRequest(
        transfer_id=uuid4(),
        from_bank_code='Bank A',
        to_bank_code='Bank B',
        amount=Decimal('150.00'),
        currency='PLN',
    )


class TestHTTPRTGSGateway:
    def test_settle_success(self):
        gw = SORBNET3Gateway(base_url='http://mock:9000/sorbnet3', timeout_seconds=5)
        transfer = _make_transfer()
        session_id = uuid4()

        with (
            patch.object(gw, 'healthcheck', return_value=True),
            patch('ledger.rtgs.gateways.requests.post') as mock_post,
        ):
            mock_post.return_value.status_code = 200
            mock_post.return_value.json.return_value = {
                'transfer_id': str(transfer.transfer_id),
                'status': 'SUCCESS',
                'rtgs_reference': 'SORBNET3-ABCDEF',
                'failure_reason': '',
            }
            results = gw.settle(session_id, [transfer])

        assert len(results) == 1
        assert results[0].status == TransferStatus.SUCCESS
        assert results[0].rtgs_reference == 'SORBNET3-ABCDEF'

        # Sprawdzenie payloadu — system_name w body
        call = mock_post.call_args
        assert call.kwargs['json']['system'] == 'SORBNET3'
        assert call.kwargs['json']['from'] == 'Bank A'

    def test_settle_failure_from_rtgs(self):
        gw = TARGET2Gateway(base_url='http://mock:9000/target2', timeout_seconds=5)
        transfer = _make_transfer()

        with (
            patch.object(gw, 'healthcheck', return_value=True),
            patch('ledger.rtgs.gateways.requests.post') as mock_post,
        ):
            mock_post.return_value.status_code = 200
            mock_post.return_value.json.return_value = {
                'transfer_id': str(transfer.transfer_id),
                'status': 'FAILED',
                'rtgs_reference': '',
                'failure_reason': 'Bank insolvent',
            }
            results = gw.settle(uuid4(), [transfer])

        assert results[0].status == TransferStatus.FAILED
        assert results[0].failure_reason == 'Bank insolvent'

    def test_settle_timeout_per_transfer(self):
        gw = SORBNET3Gateway(base_url='http://mock:9000/sorbnet3', timeout_seconds=1)
        transfer = _make_transfer()

        with (
            patch.object(gw, 'healthcheck', return_value=True),
            patch('ledger.rtgs.gateways.requests.post', side_effect=requests.Timeout('timeout!')),
        ):
            results = gw.settle(uuid4(), [transfer])

        assert results[0].status == TransferStatus.TIMEOUT
        assert 'Timeout' in results[0].failure_reason

    def test_settle_http_4xx_treated_as_failure(self):
        gw = CHAPSGateway(base_url='http://mock:9000/chaps')
        transfer = _make_transfer()

        with (
            patch.object(gw, 'healthcheck', return_value=True),
            patch('ledger.rtgs.gateways.requests.post') as mock_post,
        ):
            mock_post.return_value.status_code = 422
            mock_post.return_value.text = 'Currency mismatch'
            mock_post.return_value.json.side_effect = ValueError('not json')
            results = gw.settle(uuid4(), [transfer])

        assert results[0].status == TransferStatus.FAILED
        assert 'Currency mismatch' in results[0].failure_reason

    def test_unhealthy_rtgs_raises_unavailable(self):
        gw = FedNowGateway(base_url='http://down:9000/fednow')
        with patch.object(gw, 'healthcheck', return_value=False):
            with pytest.raises(RTGSUnavailableError) as exc:
                gw.settle(uuid4(), [_make_transfer()])
            assert exc.value.system_name == 'FedNow'

    def test_healthcheck_calls_correct_url(self):
        gw = SORBNET3Gateway(base_url='http://mock:9000/sorbnet3')
        with patch('ledger.rtgs.gateways.requests.get') as mock_get:
            mock_get.return_value.status_code = 200
            assert gw.healthcheck() is True
            mock_get.assert_called_once()
            assert 'sorbnet3/healthz' in mock_get.call_args[0][0]

    def test_healthcheck_returns_false_on_exception(self):
        gw = SORBNET3Gateway(base_url='http://down:9000/sorbnet3')
        with patch(
            'ledger.rtgs.gateways.requests.get',
            side_effect=requests.ConnectionError('refused'),
        ):
            assert gw.healthcheck() is False

    def test_unknown_status_treated_as_failed(self):
        gw = SORBNET3Gateway(base_url='http://mock:9000/sorbnet3')
        transfer = _make_transfer()

        with (
            patch.object(gw, 'healthcheck', return_value=True),
            patch('ledger.rtgs.gateways.requests.post') as mock_post,
        ):
            mock_post.return_value.status_code = 200
            mock_post.return_value.json.return_value = {
                'transfer_id': str(transfer.transfer_id),
                'status': 'UNKNOWN_GIBBERISH',
            }
            results = gw.settle(uuid4(), [transfer])

        assert results[0].status == TransferStatus.FAILED
