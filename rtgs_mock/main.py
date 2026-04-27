"""
Mock RTGS — symulator 4 systemów bankowości centralnej dla developmentu/demo KLIK.

Wystawia 4 prefiksy URL:
    /sorbnet3   — Polska, PLN
    /target2    — Strefa euro, EUR
    /chaps      — UK, GBP
    /fednow     — USA, USD

Każdy prefix ma identyczny protokół (KLIK używa wspólnej bazy gateway-a):
    POST /{system}/settle  → wykonaj transfer
    GET  /{system}/healthz → liveness check

Symulator wprowadza realistyczne zachowania, które chcemy testować w KLIK:
- Latency: opóźnienie 50-300ms per transfer (settable env).
- Failure injection: konfigurowane prawdopodobieństwo FAILED i TIMEOUT.
- Bank blacklist: można oznaczyć banki jako "niewypłacalne" dla danej strefy.
- Idempotency: ten sam transfer_id zwraca ten sam wynik (łatwiej testować
  retry-y).

Konfiguracja przez env:
    RTGS_LATENCY_MIN_MS  — default 50
    RTGS_LATENCY_MAX_MS  — default 300
    RTGS_FAIL_RATE       — default 0.0  (0.0 = nigdy, 1.0 = zawsze)
    RTGS_TIMEOUT_RATE    — default 0.0
    RTGS_BLACKLIST       — comma-separated lista bank_code (FAILED dla each)

Zarządzanie blacklist live (bez restartu) — endpoint admin:
    POST /admin/blacklist  body: {"banks": ["BadBank"]}
    GET  /admin/blacklist
"""

from __future__ import annotations

import logging
import os
import random
import time
import uuid
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Body, FastAPI, HTTPException, Path
from pydantic import BaseModel, Field

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger('rtgs-mock')


# ----------------------------------------------------------------------
# Konfiguracja z env
# ----------------------------------------------------------------------


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, default))
    except (TypeError, ValueError):
        return default


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, default))
    except (TypeError, ValueError):
        return default


LATENCY_MIN_MS = _env_int('RTGS_LATENCY_MIN_MS', 50)
LATENCY_MAX_MS = _env_int('RTGS_LATENCY_MAX_MS', 300)
FAIL_RATE = _env_float('RTGS_FAIL_RATE', 0.0)
TIMEOUT_RATE = _env_float('RTGS_TIMEOUT_RATE', 0.0)
INITIAL_BLACKLIST = {
    b.strip()
    for b in os.environ.get('RTGS_BLACKLIST', '').split(',')
    if b.strip()
}


# ----------------------------------------------------------------------
# Stan in-memory (idempotency cache + dynamic blacklist)
# ----------------------------------------------------------------------
#
# Dla MVP/demo wystarczy in-memory (mock i tak nie ma persistence).
# W realnym RTGS byłaby DB, klastrowanie, audit log. Tu jeden proces.


_idempotency_cache: dict[str, dict] = {}  # transfer_id → odpowiedź
_blacklist: set[str] = set(INITIAL_BLACKLIST)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(
        'RTGS mock startuje. latency=%d-%dms, fail_rate=%.2f, timeout_rate=%.2f, blacklist=%s',
        LATENCY_MIN_MS,
        LATENCY_MAX_MS,
        FAIL_RATE,
        TIMEOUT_RATE,
        sorted(_blacklist),
    )
    yield
    logger.info('RTGS mock zamyka się.')


app = FastAPI(
    title='Mock RTGS (SORBNET3/TARGET2/CHAPS/FedNow)',
    version='1.0.0',
    description='Symulator systemów bankowości centralnej dla KLIK MVP.',
    lifespan=lifespan,
)


# ----------------------------------------------------------------------
# Schematy
# ----------------------------------------------------------------------


class SettleRequest(BaseModel):
    """Payload przychodzący od KLIK gateway (HTTPRTGSGateway.build_payload)."""

    session_id: str
    transfer_id: str
    system: str = Field(..., description='SORBNET3 | TARGET2 | CHAPS | FedNow')
    from_: str = Field(..., alias='from', description='Bank source code/name')
    to: str
    amount: str
    currency: str

    model_config = {'populate_by_name': True}


class SettleResponse(BaseModel):
    """Wynik settlementu zwracany do KLIK."""

    transfer_id: str
    status: str  # SUCCESS | FAILED
    rtgs_reference: str = ''
    failure_reason: str = ''


class BlacklistUpdate(BaseModel):
    banks: list[str]


VALID_SYSTEMS = {
    'sorbnet3': ('SORBNET3', 'PLN'),
    'target2': ('TARGET2', 'EUR'),
    'chaps': ('CHAPS', 'GBP'),
    'fednow': ('FedNow', 'USD'),
}


# ----------------------------------------------------------------------
# Healthcheck — jeden per system
# ----------------------------------------------------------------------


@app.get('/{system}/healthz')
async def healthz(system: Annotated[str, Path(pattern='^(sorbnet3|target2|chaps|fednow)$')]):
    """Liveness/readiness check dla KLIK gateway-a.

    Zwraca 200 zawsze (chyba że dorobimy fail injection na poziomie healthchecka).
    """
    return {'status': 'ok', 'system': VALID_SYSTEMS[system][0]}


# ----------------------------------------------------------------------
# Settle — główny endpoint
# ----------------------------------------------------------------------


@app.post('/{system}/settle', response_model=SettleResponse)
async def settle(
    system: Annotated[str, Path(pattern='^(sorbnet3|target2|chaps|fednow)$')],
    payload: Annotated[SettleRequest, Body()],
):
    """Symuluje wykonanie pojedynczego transferu RTGS.

    Pipeline:
        1. Walidacja waluty (PLN dla SORBNET3, EUR dla TARGET2, ...)
        2. Idempotency: jeśli transfer_id już widziany → zwróć cached.
        3. Latency injection (sleep).
        4. Timeout injection (zwracamy 504 lub po prostu wisimy długo).
        5. Blacklist check: from_bank/to_bank → FAILED.
        6. Random fail injection.
        7. Domyślnie: SUCCESS z wygenerowanym rtgs_reference.

    Dla pełni realizmu: w realnym SORBNET3 byłaby walidacja BIC,
    sprawdzanie limitów banku, kolejkowanie. Tu dla MVP wystarczy
    happy path + injection zachowań które chcemy testować w KLIK.
    """
    system_name, expected_currency = VALID_SYSTEMS[system]

    # Walidacja waluty — sanity check po stronie mocka. KLIK by się
    # nie pomylił, ale jeśli ktoś pomyli URL, niech zobaczy 422.
    if payload.currency != expected_currency:
        raise HTTPException(
            status_code=422,
            detail=f'{system_name} obsługuje tylko {expected_currency}, otrzymano {payload.currency}',
        )

    # Idempotency: ten sam transfer_id → ta sama odpowiedź. Implementacje gateway
    # polegają na tym przy retry (chociaż w MVP gateway nie retryuje per transfer).
    cached = _idempotency_cache.get(payload.transfer_id)
    if cached:
        logger.info(
            '[%s] transfer %s replay (cached %s)',
            system_name,
            payload.transfer_id,
            cached['status'],
        )
        return SettleResponse(**cached)

    # Latency
    latency_ms = random.randint(LATENCY_MIN_MS, LATENCY_MAX_MS)
    time.sleep(latency_ms / 1000)

    # Timeout injection — symulujemy przez raise HTTPException(504), bo realny
    # timeout (długi sleep) blokowałby cały event loop. Dla testów timeout-handling
    # po stronie KLIK to jest wystarczające — KLIK gateway łapie 5xx jako FAILED
    # (lub TIMEOUT po stronie connection-level — to inny problem).
    if TIMEOUT_RATE > 0 and random.random() < TIMEOUT_RATE:
        # 504 → KLIK potraktuje jako FAILED z message, można też puścić requests.exceptions.Timeout
        # przez `time.sleep(timeout + 5)` jeśli chcemy testować realny timeout.
        raise HTTPException(status_code=504, detail='Gateway Timeout (injected)')

    # Blacklist check
    if payload.from_ in _blacklist:
        response_data = {
            'transfer_id': payload.transfer_id,
            'status': 'FAILED',
            'rtgs_reference': '',
            'failure_reason': f'Bank {payload.from_} jest niewypłacalny w {system_name}',
        }
        _idempotency_cache[payload.transfer_id] = response_data
        logger.info('[%s] transfer %s FAILED (blacklist from)', system_name, payload.transfer_id)
        return SettleResponse(**response_data)

    if payload.to in _blacklist:
        response_data = {
            'transfer_id': payload.transfer_id,
            'status': 'FAILED',
            'rtgs_reference': '',
            'failure_reason': f'Bank {payload.to} nie przyjmuje transferów w {system_name}',
        }
        _idempotency_cache[payload.transfer_id] = response_data
        logger.info('[%s] transfer %s FAILED (blacklist to)', system_name, payload.transfer_id)
        return SettleResponse(**response_data)

    # Random fail injection
    if FAIL_RATE > 0 and random.random() < FAIL_RATE:
        response_data = {
            'transfer_id': payload.transfer_id,
            'status': 'FAILED',
            'rtgs_reference': '',
            'failure_reason': f'{system_name}: random failure (injected)',
        }
        _idempotency_cache[payload.transfer_id] = response_data
        logger.info('[%s] transfer %s FAILED (random)', system_name, payload.transfer_id)
        return SettleResponse(**response_data)

    # Happy path
    rtgs_ref = f'{system_name}-{uuid.uuid4().hex[:12].upper()}'
    response_data = {
        'transfer_id': payload.transfer_id,
        'status': 'SUCCESS',
        'rtgs_reference': rtgs_ref,
        'failure_reason': '',
    }
    _idempotency_cache[payload.transfer_id] = response_data

    logger.info(
        '[%s] settle %s → %s, %s %s = %s (latency %dms)',
        system_name,
        payload.from_,
        payload.to,
        payload.amount,
        payload.currency,
        rtgs_ref,
        latency_ms,
    )
    return SettleResponse(**response_data)


# ----------------------------------------------------------------------
# Admin endpoints — manipulacja stanu mocka bez restartu
# ----------------------------------------------------------------------
# Dla DX: w trakcie demo można pokazać "Bank X nagle pada" przez POST /admin/blacklist


@app.get('/admin/blacklist')
async def get_blacklist():
    return {'banks': sorted(_blacklist)}


@app.post('/admin/blacklist')
async def add_to_blacklist(payload: Annotated[BlacklistUpdate, Body()]):
    _blacklist.update(payload.banks)
    logger.info('admin: dodano do blacklist: %s', payload.banks)
    return {'banks': sorted(_blacklist)}


@app.delete('/admin/blacklist')
async def clear_blacklist():
    _blacklist.clear()
    logger.info('admin: wyczyszczono blacklist')
    return {'banks': []}


@app.post('/admin/reset-cache')
async def reset_idempotency_cache():
    """Czyści cache idempotency — przydatne między testami."""
    cnt = len(_idempotency_cache)
    _idempotency_cache.clear()
    logger.info('admin: wyczyszczono cache idempotency (%d entries)', cnt)
    return {'cleared': cnt}


@app.get('/admin/stats')
async def stats():
    return {
        'idempotency_cache_size': len(_idempotency_cache),
        'blacklist': sorted(_blacklist),
        'config': {
            'latency_min_ms': LATENCY_MIN_MS,
            'latency_max_ms': LATENCY_MAX_MS,
            'fail_rate': FAIL_RATE,
            'timeout_rate': TIMEOUT_RATE,
        },
    }


# Healthcheck na root (dla docker healthcheck — nie wymaga konkretnej strefy)
@app.get('/healthz')
async def root_healthz():
    return {'status': 'ok'}
