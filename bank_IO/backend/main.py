"""
Mock Banku — symulator banku nadawcy w ekosystemie KLIK (moduł C2B).

Domyka happy-path C2B end-to-end bez ręcznego curl-a:
  operator → "Wygeneruj kod"      (mock → KLIK POST /codes/generate)
  agent (osobna apka)             (agent → KLIK POST /payments/initiate)
  KLIK → POST /webhook/authorize  (mock zapisuje do pending, odpowiada od razu)
  operator → "Autoryzuj PIN-em"   (mock → KLIK POST /payments/confirm status=ACCEPTED)

Stan in-memory — restart resetuje. Jeden deployment = jeden bank.
Wzorzec: rtgs_mock/main.py.

Uwagi implementacyjne:
- Webhook od KLIK (backend/codes/tasks.py) NIE zawiera user_id ani kodu — korelujemy
  webhook z klientem po najstarszym niewygasłym kodzie z kolejki _code_queue (FIFO).
  Jeśli KLIK kiedyś zacznie przekazywać user_id w payloadzie — użyjemy go.
- /payments/confirm w realnym KLIK używa pola `status` (ACCEPTED/REJECTED).
"""

from __future__ import annotations

import logging
import os
import threading
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Annotated, Optional

import httpx
from fastapi import Body, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("bank-mock")


# --------------------------------------------------------------------------
# Konfiguracja z env
# --------------------------------------------------------------------------
BANK_NAME = os.environ.get("BANK_NAME", "BANK_MOCK")
BANK_ZONE = os.environ.get("BANK_ZONE", "PL")
KLIK_BASE_URL = os.environ.get("KLIK_BASE_URL", "http://localhost:8000/api/v1").rstrip(
    "/"
)
_klik_bank_api_key: str = os.environ.get("KLIK_BANK_API_KEY", "")
KLIK_HTTP_TIMEOUT = float(os.environ.get("KLIK_HTTP_TIMEOUT", "10"))
CODE_TTL_SECONDS = int(os.environ.get("KLIK_CODE_TTL_SECONDS", "120"))

ZONE_CURRENCY = {"PL": "PLN", "EU": "EUR", "UK": "GBP", "US": "USD"}
REJECT_REASONS = {
    "INSUFFICIENT_FUNDS",
    "USER_DECLINED",
    "PIN_FAILED",
    "AML_BLOCK",
    "OTHER",
}


# --------------------------------------------------------------------------
# Stan in-memory
# --------------------------------------------------------------------------
clients: dict[str, dict] = {
    "user-1": {
        "name": "Jan Kowalski",
        "balance": 5000.00,
        "pin": "1234",
        "phone": "+48501111111",
        "iban": "PL61109010140000071219812874",
    },
    "user-2": {
        "name": "Anna Nowak",
        "balance": 1500.00,
        "pin": "1234",
        "phone": "+48502222222",
        "iban": "PL27114020040000300201355387",
    },
    "user-3": {
        "name": "Piotr Wiśniewski",
        "balance": 80.00,
        "pin": "1234",
        "phone": "+48503333333",
        "iban": "PL83102000810000060001234567",
    },
}

# transaction_id -> {amount, currency, merchant_name, is_on_us, expiry_time(datetime), zone, user_id, received_at}
pending_authorizations: dict[str, dict] = {}

# FIFO kodów wygenerowanych przez operatora (korelacja webhooka — KLIK nie wysyła user_id)
_code_queue: list[dict] = []  # {user_id, code, created_at, expires_at}

registered_aliases: dict[str, dict] = {}

# audit log (najstarsze pierwsze)
history: list[dict] = []

_lock = threading.Lock()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _client_name(user_id: Optional[str]) -> str:
    if user_id and user_id in clients:
        return clients[user_id]["name"]
    return "(nieznany klient)"


def _log_history(event_type: str, **fields) -> None:
    entry = {
        "id": str(uuid.uuid4()),
        "timestamp": _now().isoformat(),
        "type": event_type,
        **fields,
    }
    history.append(entry)
    logger.info(
        "history: %s %s", event_type, {k: v for k, v in fields.items() if k != "result"}
    )


def _prune_expired() -> None:
    """Usuwa wygasłe kody z kolejki i wygasłe autoryzacje z pending (z notą do historii)."""
    now = _now()
    with _lock:
        _code_queue[:] = [c for c in _code_queue if c["expires_at"] > now]
        expired = [
            tx
            for tx, p in pending_authorizations.items()
            if p["expiry_time"] and p["expiry_time"] <= now
        ]
        for tx in expired:
            p = pending_authorizations.pop(tx)
            _log_history(
                "EXPIRED",
                transaction_id=tx,
                user_id=p.get("user_id"),
                user_name=_client_name(p.get("user_id")),
                amount=p["amount"],
                currency=p["currency"],
                merchant_name=p["merchant_name"],
                result="Autoryzacja wygasła — operator nie zdążył zatwierdzić",
            )


# --------------------------------------------------------------------------
# Wywołania do KLIK
# --------------------------------------------------------------------------
def _klik_headers() -> dict:
    return {
        "X-KLIK-Bank-Api-Key": _klik_bank_api_key,
        "Content-Type": "application/json",
        "Idempotency-Key": str(uuid.uuid4()),
    }


def _klik_request(method: str, path: str, payload: Optional[dict] = None) -> dict:
    """Generyczne wywołanie do KLIK. Mapuje błędy upstream na HTTPException
    z czytelnym `code`/`message` z error envelope KLIK-a.
    """
    url = f"{KLIK_BASE_URL}{path}"
    try:
        resp = httpx.request(
            method,
            url,
            json=payload,
            headers=_klik_headers(),
            timeout=KLIK_HTTP_TIMEOUT
        )
    except httpx.RequestError as exc:
        logger.error("KLIK %s nieosiągalny: %s", url, exc)
        raise HTTPException(
            status_code=502,
            detail={"code": "KLIK_UNREACHABLE", "message": f"KLIK nieosiągalny: {exc}"},
        ) from exc
    body: object = None
    if resp.content:
        try:
            body = resp.json()
        except ValueError:
            body = {"raw": resp.text}
    if resp.status_code >= 400:
        err = body.get("error", {}) if isinstance(body, dict) else {}
        raise HTTPException(
            status_code=resp.status_code,
            detail={
                "code": err.get("code", f"HTTP_{resp.status_code}"),
                "message": err.get("message", f"KLIK zwrócił HTTP {resp.status_code}"),
            },
        )
    return body if isinstance(body, dict) else {}


def _klik_post(path: str, payload: Optional[dict] = None) -> dict:
    return _klik_request("POST", path, payload)


def _klik_get(path: str, payload: Optional[dict] = None) -> dict:
    return _klik_request("GET", path, payload)

def _klik_delete(path: str, payload: Optional[dict] = None) -> dict:
    return _klik_request("DELETE", path, payload)

# --------------------------------------------------------------------------
# App / lifespan / CORS
# --------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(
        'Bank mock "%s" startuje. zone=%s KLIK=%s api_key=%s',
        BANK_NAME,
        BANK_ZONE,
        KLIK_BASE_URL,
        "ustawiony" if _klik_bank_api_key else "BRAK",
    )
    stop = threading.Event()

    def _janitor():
        while not stop.wait(1.0):
            try:
                _prune_expired()
            except Exception:
                logger.exception("janitor error")

    t = threading.Thread(target=_janitor, daemon=True)
    t.start()
    yield
    stop.set()
    logger.info("Bank mock zamyka się.")


app = FastAPI(
    title=f"Mock Banku KLIK — {BANK_NAME}", version="1.0.0", lifespan=lifespan
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# --------------------------------------------------------------------------
# Schematy
# --------------------------------------------------------------------------
class AuthorizeWebhookIn(BaseModel):
    transaction_id: str
    amount: str
    currency: str
    merchant_name: str = ""
    is_on_us: bool = False
    expiry_time: Optional[str] = None
    zone: Optional[str] = None
    user_id: Optional[str] = None  # KLIK obecnie nie wysyła; jeśli wyśle — użyjemy
    model_config = {"extra": "allow"}


class PingIn(BaseModel):
    timestamp: Optional[str] = None
    nonce: Optional[str] = None
    model_config = {"extra": "allow"}


class AcceptIn(BaseModel):
    pin: str


class RejectIn(BaseModel):
    reject_reason: str = "USER_DECLINED"


class RegisterAliasIn(BaseModel):
    """Opcjonalnie nadpisany telefon (domyślnie bierzemy z clients[user_id]['phone'])."""

    phone: Optional[str] = None
    model_config = {"extra": "allow"}


class LookupIn(BaseModel):
    phone: str


# --------------------------------------------------------------------------
# Webhooki od KLIK
# --------------------------------------------------------------------------
@app.post("/webhook/authorize")
async def webhook_authorize(payload: Annotated[AuthorizeWebhookIn, Body()]):
    _prune_expired()
    expiry_dt = _parse_iso(payload.expiry_time) or (
        _now() + timedelta(seconds=CODE_TTL_SECONDS)
    )

    with _lock:
        user_id = payload.user_id
        if not user_id:
            # KLIK nie przekazuje user_id w webhooku — bierzemy najstarszy niewygasły kod.
            while _code_queue:
                cand = _code_queue.pop(0)
                if cand["expires_at"] > _now():
                    user_id = cand["user_id"]
                    break
        pending_authorizations[payload.transaction_id] = {
            "transaction_id": payload.transaction_id,
            "amount": payload.amount,
            "currency": payload.currency,
            "merchant_name": payload.merchant_name,
            "is_on_us": payload.is_on_us,
            "expiry_time": expiry_dt,
            "zone": payload.zone or BANK_ZONE,
            "user_id": user_id,
            "received_at": _now().isoformat(),
        }

    _log_history(
        "WEBHOOK_RECEIVED",
        transaction_id=payload.transaction_id,
        user_id=user_id,
        user_name=_client_name(user_id),
        amount=payload.amount,
        currency=payload.currency,
        merchant_name=payload.merchant_name,
        result="Oczekuje na decyzję klienta",
    )
    return {"received": True, "will_prompt_user": True}


@app.post("/webhook/ping")
async def webhook_ping(payload: Annotated[PingIn, Body()]):
    return {"timestamp": payload.timestamp, "nonce": payload.nonce, "pong": True}


# --------------------------------------------------------------------------
# API dla UI
# --------------------------------------------------------------------------
@app.get("/api/info")
async def api_info():
    _prune_expired()
    return {
        "bank_name": BANK_NAME,
        "zone": BANK_ZONE,
        "currency": ZONE_CURRENCY.get(BANK_ZONE, "PLN"),
        "klik_base_url": KLIK_BASE_URL,
        "klik_api_key_configured": bool(_klik_bank_api_key),
        "klik_api_key_preview": (
            (_klik_bank_api_key[:6] + "…") if _klik_bank_api_key else ""
        ),
        "pending_count": len(pending_authorizations),
        "clients_count": len(clients),
    }


class ApiKeyIn(BaseModel):
    api_key: str


@app.post("/api/config/api-key")
async def api_set_api_key(payload: Annotated[ApiKeyIn, Body()]):
    global _klik_bank_api_key
    key = payload.api_key.strip()
    if not key:
        raise HTTPException(
            status_code=400,
            detail={"code": "EMPTY_KEY", "message": "Klucz nie może być pusty."},
        )
    _klik_bank_api_key = key
    _log_history("API_KEY_UPDATED", result="Klucz KLIK zmieniony w runtime")
    return {"configured": True, "preview": key[:6] + "…"}


@app.delete("/api/config/api-key")
async def api_clear_api_key():
    global _klik_bank_api_key
    _klik_bank_api_key = ""
    _log_history("API_KEY_CLEARED", result="Klucz KLIK wyczyszczony")
    return {"configured": False}


@app.get("/api/clients")
async def api_clients():
    with _lock:
        registered_phones = set(registered_aliases.keys())
    return [
        {
            "id": uid,
            "name": c["name"],
            "balance": round(c["balance"], 2),
            "phone": c.get("phone"),
            "iban": c.get("iban"),
            "alias_registered": c.get("phone") in registered_phones,
        }
        for uid, c in clients.items()
    ]


@app.post("/api/clients/{user_id}/generate-code")
async def api_generate_code(user_id: str):
    if user_id not in clients:
        raise HTTPException(
            status_code=404,
            detail={"code": "CLIENT_NOT_FOUND", "message": f"Brak klienta {user_id}"},
        )
    result = _klik_post("/codes/generate", {"user_id": user_id, "zone": BANK_ZONE})
    code = result.get("code")
    expires_in = int(result.get("expires_in", CODE_TTL_SECONDS))
    with _lock:
        _code_queue.append(
            {
                "user_id": user_id,
                "code": code,
                "created_at": _now(),
                "expires_at": _now() + timedelta(seconds=expires_in),
            }
        )
    _log_history(
        "CODE_GENERATED",
        user_id=user_id,
        user_name=clients[user_id]["name"],
        code=code,
        result=f"Kod ważny {expires_in}s",
    )
    return {
        "code": code,
        "expires_in": expires_in,
        "expires_at": result.get("expires_at"),
    }


def _pending_view(tx_id: str, p: dict) -> dict:
    seconds_left = (
        int((p["expiry_time"] - _now()).total_seconds()) if p["expiry_time"] else None
    )
    user_id = p.get("user_id")
    try:
        amount_f = float(p["amount"])
    except (TypeError, ValueError):
        amount_f = None
    balance = clients[user_id]["balance"] if user_id in clients else None
    sufficient = balance is not None and amount_f is not None and balance >= amount_f
    return {
        "transaction_id": tx_id,
        "user_id": user_id,
        "user_name": _client_name(user_id),
        "amount": p["amount"],
        "currency": p["currency"],
        "merchant_name": p["merchant_name"],
        "is_on_us": p["is_on_us"],
        "zone": p["zone"],
        "received_at": p["received_at"],
        "seconds_left": max(seconds_left, 0) if seconds_left is not None else None,
        "client_balance": round(balance, 2) if balance is not None else None,
        "sufficient_balance": sufficient,
    }


@app.get("/api/pending")
async def api_pending():
    _prune_expired()
    with _lock:
        items = [_pending_view(tx, p) for tx, p in pending_authorizations.items()]
    items.sort(key=lambda x: x["received_at"])
    return items


@app.post("/api/pending/{transaction_id}/accept")
async def api_accept(transaction_id: str, payload: Annotated[AcceptIn, Body()]):
    _prune_expired()
    with _lock:
        p = pending_authorizations.get(transaction_id)
    if not p:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "PENDING_NOT_FOUND",
                "message": "Brak takiej autoryzacji (mogła wygasnąć).",
            },
        )

    user_id = p.get("user_id")
    client = clients.get(user_id) if user_id else None

    # PIN sprawdzany po stronie mocka
    if client is not None and payload.pin != client["pin"]:
        raise HTTPException(
            status_code=400, detail={"code": "PIN_INVALID", "message": "Błędny PIN."}
        )

    try:
        amount_f = float(p["amount"])
    except (TypeError, ValueError):
        amount_f = 0.0

    # Brak środków → automatyczny REJECT zamiast akceptacji
    if client is not None and client["balance"] < amount_f:
        _klik_post(
            "/payments/confirm",
            {
                "transaction_id": transaction_id,
                "status": "REJECTED",
                "reject_reason": "INSUFFICIENT_FUNDS",
            },
        )
        with _lock:
            pending_authorizations.pop(transaction_id, None)
        _log_history(
            "REJECTED",
            transaction_id=transaction_id,
            user_id=user_id,
            user_name=_client_name(user_id),
            amount=p["amount"],
            currency=p["currency"],
            merchant_name=p["merchant_name"],
            reject_reason="INSUFFICIENT_FUNDS",
            result="Odrzucono automatycznie — brak środków",
        )
        return {
            "auto_rejected": True,
            "reject_reason": "INSUFFICIENT_FUNDS",
            "message": "Brak wystarczających środków — autoryzacja odrzucona.",
        }

    result = _klik_post(
        "/payments/confirm", {"transaction_id": transaction_id, "status": "ACCEPTED"}
    )

    with _lock:
        pending_authorizations.pop(transaction_id, None)
        if client is not None:
            client["balance"] = round(client["balance"] - amount_f, 2)

    _log_history(
        "AUTHORIZED",
        transaction_id=transaction_id,
        user_id=user_id,
        user_name=_client_name(user_id),
        amount=p["amount"],
        currency=p["currency"],
        merchant_name=p["merchant_name"],
        merchant_net=result.get("merchant_net"),
        klik_fee=result.get("klik_fee"),
        agent_fee=result.get("agent_fee"),
        result=f"KLIK: {result.get('status', 'COMPLETED')}",
    )
    return {"auto_rejected": False, **result}


@app.post("/api/pending/{transaction_id}/reject")
async def api_reject(transaction_id: str, payload: Annotated[RejectIn, Body()]):
    _prune_expired()
    with _lock:
        p = pending_authorizations.get(transaction_id)
    if not p:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "PENDING_NOT_FOUND",
                "message": "Brak takiej autoryzacji (mogła wygasnąć).",
            },
        )
    reason = (
        payload.reject_reason if payload.reject_reason in REJECT_REASONS else "OTHER"
    )
    result = _klik_post(
        "/payments/confirm",
        {
            "transaction_id": transaction_id,
            "status": "REJECTED",
            "reject_reason": reason,
        },
    )
    with _lock:
        pending_authorizations.pop(transaction_id, None)
    _log_history(
        "REJECTED",
        transaction_id=transaction_id,
        user_id=p.get("user_id"),
        user_name=_client_name(p.get("user_id")),
        amount=p["amount"],
        currency=p["currency"],
        merchant_name=p["merchant_name"],
        reject_reason=reason,
        result=f"KLIK: {result.get('status', 'REJECTED')}",
    )
    return result


# --------------------------------------------------------------------------
# API dla UI — P2P (Telefony)
#
# Pattern analogiczny do /api/clients/{id}/generate-code: operator-mock woła
# w imieniu klienta tego banku, my forwardujemy do KLIK z naszym X-KLIK-Bank-Api-Key.
# --------------------------------------------------------------------------


@app.post("/api/clients/{user_id}/register-alias")
async def api_register_alias(
    user_id: str, payload: Annotated[RegisterAliasIn, Body()] = RegisterAliasIn()
):
    """Operator rejestruje numer telefonu klienta jako alias P2P w KLIK.

    Domyślnie używa `clients[user_id]['phone']`; można nadpisać przez body.
    Po sukcesie cache'ujemy w `registered_aliases` żeby UI mogło listować
    aliasy należące do tego banku (KLIK nie udostępnia list-by-bank).
    """
    if user_id not in clients:
        raise HTTPException(
            status_code=404,
            detail={"code": "CLIENT_NOT_FOUND", "message": f"Brak klienta {user_id}"},
        )
    client = clients[user_id]
    phone = (payload.phone or client.get("phone") or "").strip()
    if not phone:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "PHONE_REQUIRED",
                "message": "Klient nie ma przypisanego telefonu — podaj w body.",
            },
        )
    iban = client.get("iban")
    if not iban:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "IBAN_MISSING",
                "message": f"Klient {user_id} bez przypisanego IBAN — niemożliwa rejestracja.",
            },
        )

    result = _klik_post(
        "/aliases/register", {"phone": phone, "iban": iban, "zone": BANK_ZONE}
    )

    entry = {
        "alias_id": result.get("alias_id"),
        "phone": phone,
        "user_id": user_id,
        "user_name": client["name"],
        "iban": iban,
        "zone": BANK_ZONE,
        "registered_at": result.get("registered_at") or _now().isoformat(),
    }
    with _lock:
        registered_aliases[phone] = entry

    _log_history(
        "ALIAS_REGISTERED",
        phone=phone,
        user_id=user_id,
        user_name=client["name"],
        iban=iban,
        result="Alias zarejestrowany w KLIK",
    )
    return entry


@app.get("/api/aliases")
async def api_aliases():
    """Lista aliasów zarejestrowanych przez ten bank (lokalny cache)."""
    with _lock:
        items = list(registered_aliases.values())
    items.sort(key=lambda x: x.get("registered_at") or "")
    return items


@app.delete("/api/aliases/{phone}")
async def api_alias_delete(phone: str):
    """Usuwa alias z KLIK i z lokalnego cache."""
    _klik_delete(f"/aliases/{phone}")
    with _lock:
        removed = registered_aliases.pop(phone, None)
    _log_history(
        "ALIAS_DELETED",
        phone=phone,
        user_id=(removed or {}).get("user_id"),
        user_name=(removed or {}).get("user_name") or "—",
        result="Alias usunięty z KLIK",
    )
    return {"deleted": True, "phone": phone}


@app.post("/api/lookup")
async def api_lookup(payload: Annotated[LookupIn, Body()]):
    """Operator pyta KLIK o routing dla telefonu (bank odbiorcy + IBAN).

    Zwraca `{"found": False}` na 404 zamiast propagować jako błąd HTTP —
    "nie znaleziono" to normalny wynik UX, nie awaria.
    """
    phone = payload.phone.strip()
    if not phone:
        raise HTTPException(
            status_code=400,
            detail={"code": "PHONE_REQUIRED", "message": "Podaj numer telefonu."},
        )
    try:
        result = _klik_get(f"/aliases/lookup/{phone}")
    except HTTPException as exc:
        if exc.status_code == 404:
            _log_history(
                "LOOKUP_MISS",
                phone=phone,
                result="Nie znaleziono aliasu w KLIK",
            )
            return {"found": False, "phone": phone}
        raise

    _log_history(
        "LOOKUP_HIT",
        phone=phone,
        bank_id=result.get("bank_id"),
        bank_code=result.get("bank_code"),
        result=f"Znaleziono w banku {result.get('bank_code') or result.get('bank_id')}",
    )
    return {"found": True, **result}


@app.get("/api/history")
async def api_history():
    return list(reversed(history))


@app.get("/healthz")
async def healthz():
    return {"status": "ok", "bank": BANK_NAME, "zone": BANK_ZONE}
