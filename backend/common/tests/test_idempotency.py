"""Testy idempotency decorator."""

from decimal import Decimal

import pytest
from django_redis import get_redis_connection
from rest_framework import status
from rest_framework.response import Response
from rest_framework.test import APIRequestFactory, force_authenticate
from rest_framework.views import APIView

from banks.models import Bank
from common.enums import Zone
from common.idempotency import idempotent_endpoint


@pytest.fixture(autouse=True)
def flush_redis():
    redis = get_redis_connection('default')
    redis.flushdb()
    yield
    redis.flushdb()


@pytest.fixture
def bank(db):
    return Bank.objects.create(
        name='Bank Test',
        api_key_hash='test_hash',  # pragma: allowlist secret
        zone=Zone.PL,
        currency='PLN',
        debt_limit=Decimal('1000000.00'),
        webhook_url='https://bank.example.com/webhook',
    )


class _TestView(APIView):
    """View z licznikiem wywołań — żeby sprawdzić że replay nie wywołuje method."""

    call_count = 0
    permission_classes = []
    authentication_classes = []

    @idempotent_endpoint
    def post(self, request):
        _TestView.call_count += 1
        return Response(
            {'result': 'ok', 'call': _TestView.call_count},
            status=status.HTTP_201_CREATED,
        )


@pytest.mark.django_db
class TestIdempotencyDecorator:
    def setup_method(self):
        _TestView.call_count = 0

    def test_missing_idempotency_key_returns_400(self, bank):
        factory = APIRequestFactory()
        request = factory.post('/test/', {'foo': 'bar'}, format='json')
        force_authenticate(request, user=bank)

        response = _TestView.as_view()(request)

        assert response.status_code == 400

    def test_first_call_executes_view(self, bank):
        factory = APIRequestFactory()
        request = factory.post(
            '/test/',
            {'foo': 'bar'},
            format='json',
            HTTP_IDEMPOTENCY_KEY='test-key-1',
        )
        force_authenticate(request, user=bank)

        response = _TestView.as_view()(request)
        assert response.status_code == 201
        assert response.data['call'] == 1
        assert _TestView.call_count == 1

    def test_replay_returns_cached_response(self, bank):
        factory = APIRequestFactory()

        # Pierwsze wywołanie
        request1 = factory.post(
            '/test/',
            {'foo': 'bar'},
            format='json',
            HTTP_IDEMPOTENCY_KEY='test-key-2',
        )
        force_authenticate(request1, user=bank)
        response1 = _TestView.as_view()(request1)

        # Replay
        request2 = factory.post(
            '/test/',
            {'foo': 'bar'},
            format='json',
            HTTP_IDEMPOTENCY_KEY='test-key-2',
        )
        force_authenticate(request2, user=bank)
        response2 = _TestView.as_view()(request2)

        assert response1.status_code == response2.status_code
        assert response1.data == response2.data
        assert _TestView.call_count == 1

    def test_different_payload_same_key_returns_409(self, bank):
        factory = APIRequestFactory()
        request1 = factory.post(
            '/test/',
            {'foo': 'bar'},
            format='json',
            HTTP_IDEMPOTENCY_KEY='test-key-3',
        )
        force_authenticate(request1, user=bank)
        _TestView.as_view()(request1)

        request2 = factory.post(
            '/test/',
            {'foo': 'baz'},
            format='json',
            HTTP_IDEMPOTENCY_KEY='test-key-3',
        )
        force_authenticate(request2, user=bank)

        response = _TestView.as_view()(request2)
        assert response.status_code == 409
