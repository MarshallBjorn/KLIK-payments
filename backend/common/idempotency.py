"""
Idempotency decorator dla DRF views.

Wymaga nagłówka `Idempotency-Key`. Cachuje response w Redisie z TTL 24h.
Powtórne wywołanie z tym samym kluczem i tym samym payloadem zwraca
oryginalny response. Z innym payloadem → 409_IDEMPOTENCY_CONFLICT.

Przestrzeń kluczy per typ klienta:
- idempotency:bank:{bank_id}:{key} dla banków
- idempotency:agent:{agent_id}:{key} dla agentów
"""

import hashlib
import json
import logging
from functools import wraps

from django_redis import get_redis_connection
from rest_framework.response import Response

from common.exceptions import BadRequestError, IdempotencyConflictError

logger = logging.getLogger('klik')

IDEMPOTENCY_TTL_SECONDS = 24 * 60 * 60  # 24h


def idempotent_endpoint(view_method):
    """
    Decorator dla DRF view methods (APIView.post / put).

    Użycie:
        class MyView(APIView):
            @idempotent_endpoint
            def post(self, request):
                ...

    Wymaga że request.user jest uwierzytelniony (Bank lub Agent z odpowiednim
    polem `id`). Nagłówek `Idempotency-Key` musi być obecny.
    """

    @wraps(view_method)
    def wrapper(self, request, *args, **kwargs):
        idempotency_key = request.headers.get('Idempotency-Key')
        if not idempotency_key:
            raise BadRequestError(
                detail='Nagłówek Idempotency-Key jest wymagany.',
                code='400_MISSING_IDEMPOTENCY_KEY',
            )

        client_type, client_id = _identify_client(request.user)
        redis_key = f'idempotency:{client_type}:{client_id}:{idempotency_key}'
        payload_hash = _hash_payload(request.data)

        redis = get_redis_connection('default')
        cached = redis.get(redis_key)

        if cached is not None:
            cached_data = json.loads(cached)
            if cached_data['payload_hash'] != payload_hash:
                logger.warning(
                    'Idempotency conflict for %s: same key, different payload',
                    redis_key,
                )
                raise IdempotencyConflictError()

            # Replay — zwróć cached response
            logger.debug('Idempotent replay for %s', redis_key)
            return Response(
                data=cached_data['response_data'],
                status=cached_data['status_code'],
            )

        # Wywołaj oryginalny view
        response = view_method(self, request, *args, **kwargs)

        # Cache odpowiedzi (tylko dla 2xx — błędów nie cachujemy)
        if 200 <= response.status_code < 300:
            cache_value = json.dumps(
                {
                    'payload_hash': payload_hash,
                    'status_code': response.status_code,
                    'response_data': response.data,
                }
            )
            redis.set(redis_key, cache_value, ex=IDEMPOTENCY_TTL_SECONDS)

        return response

    return wrapper


def _identify_client(user) -> tuple[str, str]:
    """
    Zwraca (typ_klienta, client_id).
    Bank → ('bank', uuid), Agent → ('agent', uuid).
    """
    # Sprawdzamy klasę przez import duck-typing zamiast isinstance
    # (uniknięcie circular imports)
    class_name = user.__class__.__name__
    if class_name == 'Bank':
        return ('bank', str(user.id))
    if class_name == 'Agent':
        return ('agent', str(user.id))
    raise RuntimeError(f'Nieznany typ klienta: {class_name}')


def _hash_payload(data) -> str:
    """Deterministyczny hash payloadu dla porównania."""
    # sort_keys=True zapewnia że {a:1,b:2} == {b:2,a:1}
    serialized = json.dumps(data, sort_keys=True, default=str)
    return hashlib.sha256(serialized.encode('utf-8')).hexdigest()
