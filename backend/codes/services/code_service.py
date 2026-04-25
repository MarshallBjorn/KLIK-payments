"""
Service operujący na kodach KLIK w Redis.

Klucze:
- code:{6-cyfrowy-kod} — JSON {bank_id, user_id, zone, status, created_at}
- tx:{transaction_id} — JSON {status, ...} dla statusowego cache transakcji
"""

import json
import logging
import secrets
from datetime import UTC, datetime

from django.conf import settings
from django_redis import get_redis_connection

from codes.enums import CodeStatus
from codes.services.exceptions import (
    CodeAlreadyUsedError,
    CodeGenerationFailedError,
    CodeNotFoundError,
)
from codes.services.lua_scripts import MARK_USED_SCRIPT

logger = logging.getLogger('klik')

CODE_KEY_PREFIX = 'code'
TX_KEY_PREFIX = 'tx'

# Limit prób przy kolizji kodu. 1 mln kodów × TTL 120s = realnie zero kolizji,
# ale bez retry zawsze jest ryzyko niepowodzenia.
MAX_GENERATION_ATTEMPTS = 10

# TTL cache statusu transakcji (na potrzeby pollingu agenta).
# Dłuższe niż TTL kodu, bo polling trwa od inicjacji do confirm.
TX_STATUS_CACHE_TTL = 900  # 15 min


class CodeService:
    """Operacje na kodach i statusach transakcji w Redisie."""

    def __init__(self):
        # Bezpośredni klient Redis dla operacji niskopoziomowych (Lua, atomic ops).
        # django.core.cache nie wystawia raw API.
        self._redis = get_redis_connection('default')

    # ------------------------------------------------------------------
    # Generowanie i lookup kodów
    # ------------------------------------------------------------------

    def generate_code(self, bank_id: str, user_id: str, zone: str) -> dict:
        """
        Generuje 6-cyfrowy kod, zapisuje atomowo z TTL.

        Returns:
            dict z polami: code, expires_in, expires_at (ISO).

        Raises:
            CodeGenerationFailedError: po MAX_GENERATION_ATTEMPTS kolizjach.
        """
        ttl = settings.KLIK_CODE_TTL_SECONDS
        payload = {
            'bank_id': str(bank_id),
            'user_id': str(user_id),
            'zone': zone,
            'status': CodeStatus.ACTIVE,
            'created_at': datetime.now(UTC).isoformat(),
        }
        payload_json = json.dumps(payload)

        for attempt in range(1, MAX_GENERATION_ATTEMPTS + 1):
            code = _generate_random_code()
            key = self._code_key(code)

            # SET NX EX — atomowo: ustaw tylko gdy klucz nie istnieje, z TTL.
            # nx=True → tylko gdy nie istnieje. ex=ttl → TTL w sekundach.
            success = self._redis.set(key, payload_json, nx=True, ex=ttl)
            if success:
                expires_at = datetime.now(UTC).timestamp() + ttl
                logger.debug(
                    'Wygenerowano kod %s dla bank=%s user=%s zone=%s (próba %d)',
                    code,
                    bank_id,
                    user_id,
                    zone,
                    attempt,
                )
                return {
                    'code': code,
                    'expires_in': ttl,
                    'expires_at': datetime.fromtimestamp(expires_at, tz=UTC).isoformat(),
                }

        logger.error(
            'CodeGenerationFailed: %d kolizji dla bank=%s zone=%s',
            MAX_GENERATION_ATTEMPTS,
            bank_id,
            zone,
        )
        raise CodeGenerationFailedError(MAX_GENERATION_ATTEMPTS)

    def get_code(self, code: str) -> dict | None:
        """
        Zwraca payload kodu lub None jeśli nie istnieje.
        """
        key = self._code_key(code)
        data = self._redis.get(key)
        if data is None:
            return None
        return json.loads(data)

    def mark_used(self, code: str) -> dict:
        """
        Atomowo oznacza kod jako USED i zwraca jego payload.

        Raises:
            CodeNotFoundError: kod nie istnieje (wygasł).
            CodeAlreadyUsedError: kod ma już status USED.
        """
        key = self._code_key(code)
        result = self._redis.eval(MARK_USED_SCRIPT, 1, key)

        if isinstance(result, bytes):
            result = result.decode('utf-8')

        if result == 'NOT_FOUND':
            raise CodeNotFoundError(code)
        if result == 'ALREADY_USED':
            raise CodeAlreadyUsedError(code)

        return json.loads(result)

    # ------------------------------------------------------------------
    # Cache statusu transakcji (dla pollingu)
    # ------------------------------------------------------------------

    def cache_transaction_status(self, transaction_id: str, status: str, **extra) -> None:
        """Zapisuje status transakcji do Redisa dla szybkiego pollingu."""
        key = self._tx_key(transaction_id)
        payload = {'status': status, **extra}
        self._redis.set(key, json.dumps(payload), ex=TX_STATUS_CACHE_TTL)

    def get_transaction_status(self, transaction_id: str) -> dict | None:
        """Zwraca cached status transakcji lub None."""
        key = self._tx_key(transaction_id)
        data = self._redis.get(key)
        if data is None:
            return None
        return json.loads(data)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _code_key(code: str) -> str:
        return f'{CODE_KEY_PREFIX}:{code}'

    @staticmethod
    def _tx_key(transaction_id) -> str:
        return f'{TX_KEY_PREFIX}:{transaction_id}'


def _generate_random_code() -> str:
    """Losuje 6-cyfrowy kod jako string ('000000'-'999999')."""
    # secrets.randbelow zamiast random.randint — kryptograficznie bezpieczne
    return f'{secrets.randbelow(1_000_000):06d}'
