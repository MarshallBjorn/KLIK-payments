"""Testy CodeService — Redis operations."""

from unittest.mock import patch

import pytest
from django_redis import get_redis_connection

from codes.enums import CodeStatus
from codes.services import CodeService
from codes.services.exceptions import (
    CodeAlreadyUsedError,
    CodeGenerationFailedError,
    CodeNotFoundError,
)
from common.enums import Zone


@pytest.fixture(autouse=True)
def flush_redis():
    """Czyszczenie Redisa przed i po każdym teście."""
    redis = get_redis_connection('default')
    redis.flushdb()
    yield
    redis.flushdb()


@pytest.fixture
def service():
    return CodeService()


class TestGenerateCode:
    def test_generates_six_digit_code(self, service):
        result = service.generate_code(
            bank_id='bank-123',
            user_id='user-456',
            zone=Zone.PL,
        )
        assert 'code' in result
        assert len(result['code']) == 6
        assert result['code'].isdigit()
        assert result['expires_in'] == 120

    def test_code_stored_with_active_status(self, service):
        result = service.generate_code(
            bank_id='bank-123',
            user_id='user-456',
            zone=Zone.PL,
        )
        payload = service.get_code(result['code'])
        assert payload is not None
        assert payload['status'] == CodeStatus.ACTIVE
        assert payload['bank_id'] == 'bank-123'
        assert payload['zone'] == Zone.PL

    def test_collision_retry_succeeds(self, service):
        # Wymuszamy kolizję dla pierwszego kodu, potem unique
        codes_iter = iter(['111111', '111111', '222222'])
        with patch(
            'codes.services.code_service._generate_random_code',
            side_effect=lambda: next(codes_iter),
        ):
            # Pre-fill kolizji
            service._redis.set('code:111111', '{"status":"ACTIVE"}', ex=120)
            result = service.generate_code(
                bank_id='b',
                user_id='u',
                zone=Zone.PL,
            )
            assert result['code'] == '222222'

    def test_too_many_collisions_raises(self, service):
        # Wszystkie próby kolidują
        with patch(
            'codes.services.code_service._generate_random_code',
            return_value='999999',
        ):
            service._redis.set('code:999999', '{"status":"ACTIVE"}', ex=120)
            with pytest.raises(CodeGenerationFailedError):
                service.generate_code(
                    bank_id='b',
                    user_id='u',
                    zone=Zone.PL,
                )


class TestGetCode:
    def test_returns_none_for_missing(self, service):
        assert service.get_code('999999') is None

    def test_returns_payload_for_existing(self, service):
        result = service.generate_code(
            bank_id='b1',
            user_id='u1',
            zone=Zone.PL,
        )
        payload = service.get_code(result['code'])
        assert payload['bank_id'] == 'b1'


class TestMarkUsed:
    def test_marks_active_code_as_used(self, service):
        result = service.generate_code(
            bank_id='b1',
            user_id='u1',
            zone=Zone.PL,
        )
        payload = service.mark_used(result['code'])
        assert payload['status'] == CodeStatus.USED
        # Kod nadal istnieje w Redis (do czasu wygaśnięcia TTL),
        # ale ma już status USED.
        cached = service.get_code(result['code'])
        assert cached['status'] == CodeStatus.USED

    def test_mark_used_twice_raises(self, service):
        result = service.generate_code(
            bank_id='b1',
            user_id='u1',
            zone=Zone.PL,
        )
        service.mark_used(result['code'])
        with pytest.raises(CodeAlreadyUsedError):
            service.mark_used(result['code'])

    def test_mark_used_nonexistent_raises(self, service):
        with pytest.raises(CodeNotFoundError):
            service.mark_used('999999')


class TestTransactionStatusCache:
    def test_cache_and_retrieve(self, service):
        service.cache_transaction_status(
            'tx-123',
            'PENDING',
            merchant='Sklep',
        )
        result = service.get_transaction_status('tx-123')
        assert result['status'] == 'PENDING'
        assert result['merchant'] == 'Sklep'

    def test_returns_none_for_missing(self, service):
        assert service.get_transaction_status('nonexistent') is None

    def test_overwrite_status(self, service):
        service.cache_transaction_status('tx-1', 'PENDING')
        service.cache_transaction_status('tx-1', 'COMPLETED')
        assert service.get_transaction_status('tx-1')['status'] == 'COMPLETED'
