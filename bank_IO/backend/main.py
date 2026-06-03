"""
Mock Banku — symulator banku nadawcy w ekosystemie KLIK (C2B + P2P + Cheques).

Domyka happy-path end-to-end bez ręcznego curl-a:

C2B (kody):
  operator → "Wygeneruj kod"      (mock → KLIK POST /codes/generate)
  agent (osobna apka, :5175)      (agent → KLIK POST /payments/initiate)
  KLIK → POST /webhook/authorize  (mock zapisuje do pending, odpowiada od razu)
  operator → "Autoryzuj PIN-em"   (mock → KLIK POST /payments/confirm status=ACCEPTED)

P2P (telefony):
  operator rejestruje alias klienta → mock → KLIK POST /aliases/register
  operator wykonuje lookup → mock → KLIK GET /aliases/lookup/{phone}

Cheques (czeki):
  operator wystawia czek dla klienta → mock → KLIK POST /cheques/issue (hold środków)
  agent/sklep realizuje → KLIK POST /cheques/redeem → KLIK → mock POST /webhook/cheques/redeemed
  operator anuluje czek → mock → KLIK POST /cheques/cancel → KLIK → mock POST /webhook/cheques/released

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
KLIK_BASE_URL = os.environ.get("KLIK_BASE_URL", "http://localhost:8000/api/v1").rstrip("/")
_klik_bank_api_key: str = os.environ.get("KLIK_BANK_API_KEY", "")
KLIK_HTTP_TIMEOUT = float(os.environ.get("KLIK_HTTP_TIMEOUT", "10"))
CODE_TTL_SECONDS = int(os.environ.get("KLIK_CODE_TTL_SECONDS", "120"))

ZONE_CURRENCY = {"PL": "PLN", "EU": "EUR", "UK": "GBP", "US": "USD"}
REJECT_REASONS = {"INSUFFICIENT_FUNDS", "USER_DECLINED", "PIN_FAILED", "AML_BLOCK", "OTHER"}

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

pending_authorizations: dict[str, dict] = {}
_code_queue: list[dict] = []
registered_aliases: dict[str, dict] = {}
issued_cheques: dict[str, dict] = {}
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
    entry = {"id": str(uuid.uuid4()), "timestamp": _now().isoformat(), "type": event_type, **fields}
    history.append(entry)
    logger.info("history: %s %s", event_type, {k: v for k, v in fields.items() if k != "result"})


def _prune_expired() -> None:
    now = _now()
    with _lock:
        expired_codes = [c for c in _code_queue if c["expires_at"] <= now]
        for c in expired_codes:
            _code_queue.remove(c)
        expired_tx = [
            tid for tid, p in pending_authorizations.items()
            if p.get("expiry_time") and p["expiry_time"] <= now
        ]
    for tid in expired_tx:
        with _lock:
            p = pending_authorizations.pop(tid, None)
        if p:
            _log_history(
                "EXPIRED",
                transaction_id=tid,
                user_id=p.get("user_id"),
                user_name=_client_name(p.get("user_id")),
                amount=p["amount"],
                currency=p["currency"],
                result="Autoryzacja wygasła",
            )


def _klik_headers(idempotency_key: Optional[str] = None) -> dict:
    h = {
        "X-KLIK-Bank-Api-Key": _klik_bank_api_key,
        "Content-Type": "application/json",
        "Idempotency-Key": idempotency_key or str(uuid.uuid4()),
    }
    return h


def _klik_post(path: str, body: dict, idempotency_key: Optional[str] = None) -> dict:
    if not _klik_bank_api_key:
        raise HTTPException(
            status_code=503,
            detail={"code": "API_KEY_NOT_CONFIGURED", "message": "Klucz KLIK API nie jest skonfigurowany."},
        )
    url = f"{KLIK_BASE_URL}{path}"
    try:
        resp = httpx.post(url, json=body, headers=_klik_headers(idempotency_key), timeout=KLIK_HTTP_TIMEOUT)
        if resp.status_code >= 400:
            try:
                detail = resp.json()
            except Exception:
                detail = {"message": resp.text}
            raise HTTPException(status_code=resp.status_code, detail=detail)
        return resp.json()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=503, detail={"code": "KLIK_UNREACHABLE", "message": str(e)})


# --------------------------------------------------------------------------
# Lifespan
# --------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Bank mock startuje: name=%s zone=%s KLIK=%s api_key=%s",
                BANK_NAME, BANK_ZONE, KLIK_BASE_URL,
                "ustawiony" if _klik_bank_api_key else "BRAK")
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


app = FastAPI(title=f"Mock Banku KLIK — {BANK_NAME}", version="1.1.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


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
    user_id: Optional[str] = None
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
    phone: Optional[str] = None
    model_config = {"extra": "allow"}


class LookupIn(BaseModel):
    phone: str


class ApiKeyIn(BaseModel):
    api_key: str


# --- Cheques ---
class ChequeRedeemedWebhookIn(BaseModel):
    cheque_id: str
    transaction_id: Optional[str] = None
    amount: str
    currency: str
    redeemed_at: Optional[str] = None
    user_id: Optional[str] = None
    model_config = {"extra": "allow"}


class ChequeReleasedWebhookIn(BaseModel):
    cheque_id: str
    amount: str
    currency: str
    reason: str
    released_at: Optional[str] = None
    user_id: Optional[str] = None
    model_config = {"extra": "allow"}


class IssueChequeIn(BaseModel):
    user_id: str
    amount: float
    ttl_seconds: int = 86400


# --------------------------------------------------------------------------
# Webhooki od KLIK — C2B
# --------------------------------------------------------------------------
@app.post("/webhook/authorize")
async def webhook_authorize(payload: Annotated[AuthorizeWebhookIn, Body()]):
    _prune_expired()
    expiry_dt = _parse_iso(payload.expiry_time) or (_now() + timedelta(seconds=CODE_TTL_SECONDS))
    with _lock:
        user_id = payload.user_id
        if not user_id:
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
    _log_history("WEBHOOK_RECEIVED", transaction_id=payload.transaction_id,
                 user_id=user_id, user_name=_client_name(user_id),
                 amount=payload.amount, currency=payload.currency,
                 merchant_name=payload.merchant_name, result="Oczekuje na decyzję klienta")
    return {"received": True, "will_prompt_user": True}


@app.post("/webhook/ping")
async def webhook_ping(payload: Annotated[PingIn, Body()]):
    return {"timestamp": payload.timestamp, "nonce": payload.nonce, "pong": True}


# --------------------------------------------------------------------------
# Webhooki od KLIK — Cheques
# --------------------------------------------------------------------------
@app.post("/webhook/cheques/redeemed")
async def webhook_cheque_redeemed(payload: Annotated[ChequeRedeemedWebhookIn, Body()]):
    """KLIK informuje że czek został zrealizowany → zwalniamy hold, księgujemy debet."""
    with _lock:
        cheque = issued_cheques.get(payload.cheque_id)
        if cheque:
            cheque["status"] = "REDEEMED"
            cheque["redeemed_at"] = payload.redeemed_at or _now().isoformat()
            cheque["transaction_id"] = payload.transaction_id
            user_id = cheque.get("user_id") or payload.user_id
            # Debet już był pobrany przy issue (hold) — nie zmieniamy salda ponownie
        else:
            user_id = payload.user_id
    _log_history("CHEQUE_REDEEMED", cheque_id=payload.cheque_id,
                 transaction_id=payload.transaction_id, amount=payload.amount,
                 currency=payload.currency, user_id=user_id,
                 user_name=_client_name(user_id), result="Debet zaksięgowany, hold zwolniony")
    return {"received": True}


@app.post("/webhook/cheques/released")
async def webhook_cheque_released(payload: Annotated[ChequeReleasedWebhookIn, Body()]):
    """KLIK informuje że czek anulowany/wygasł → zwracamy hold do salda klienta."""
    with _lock:
        cheque = issued_cheques.get(payload.cheque_id)
        if cheque and cheque["status"] == "ACTIVE":
            cheque["status"] = payload.reason  # CANCELLED lub EXPIRED
            cheque["cancelled_at"] = payload.released_at or _now().isoformat()
            user_id = cheque.get("user_id") or payload.user_id
            # Zwrot holda — środki wracają do salda
            if user_id and user_id in clients:
                clients[user_id]["balance"] = round(
                    clients[user_id]["balance"] + cheque["amount"], 2
                )
        else:
            user_id = payload.user_id
    _log_history("CHEQUE_RELEASED", cheque_id=payload.cheque_id, amount=payload.amount,
                 currency=payload.currency, reason=payload.reason, user_id=user_id,
                 user_name=_client_name(user_id), result=f"Hold zwolniony ({payload.reason}), saldo przywrócone")
    return {"received": True}


# --------------------------------------------------------------------------
# API dla UI — info i konfiguracja
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
        "klik_api_key_preview": (_klik_bank_api_key[:6] + "…") if _klik_bank_api_key else "",
        "pending_count": len(pending_authorizations),
        "clients_count": len(clients),
        "cheques_count": len(issued_cheques),
    }


@app.post("/api/config/api-key")
async def api_set_api_key(payload: Annotated[ApiKeyIn, Body()]):
    global _klik_bank_api_key
    key = payload.api_key.strip()
    if not key:
        raise HTTPException(status_code=400, detail={"code": "EMPTY_KEY", "message": "Klucz nie może być pusty."})
    _klik_bank_api_key = key
    _log_history("API_KEY_UPDATED", result="Klucz KLIK zmieniony w runtime")
    return {"configured": True, "preview": key[:6] + "…"}


@app.delete("/api/config/api-key")
async def api_clear_api_key():
    global _klik_bank_api_key
    _klik_bank_api_key = ""
    _log_history("API_KEY_CLEARED", result="Klucz KLIK wyczyszczony")
    return {"configured": False}


# --------------------------------------------------------------------------
# API dla UI — klienci i C2B
# --------------------------------------------------------------------------
@app.get("/api/clients")
async def api_clients():
    with _lock:
        registered_phones = set(registered_aliases.keys())
    return [
        {
            "id": uid, "name": c["name"], "balance": round(c["balance"], 2),
            "phone": c.get("phone"), "iban": c.get("iban"),
            "alias_registered": c.get("phone") in registered_phones,
        }
        for uid, c in clients.items()
    ]


@app.post("/api/clients/{user_id}/generate-code")
async def api_generate_code(user_id: str):
    if user_id not in clients:
        raise HTTPException(status_code=404, detail={"code": "CLIENT_NOT_FOUND", "message": f"Brak klienta {user_id}"})
    result = _klik_post("/codes/generate", {"user_id": user_id, "zone": BANK_ZONE})
    code = result.get("code")
    expires_in = int(result.get("expires_in", CODE_TTL_SECONDS))
    with _lock:
        _code_queue.append({"user_id": user_id, "code": code, "created_at": _now(),
                            "expires_at": _now() + timedelta(seconds=expires_in)})
    _log_history("CODE_GENERATED", user_id=user_id, user_name=clients[user_id]["name"],
                 code=code, result=f"Kod ważny {expires_in}s")
    return {"code": code, "expires_in": expires_in, "expires_at": result.get("expires_at")}


def _pending_view(tx_id: str, p: dict) -> dict:
    seconds_left = int((p["expiry_time"] - _now()).total_seconds()) if p.get("expiry_time") else None
    user_id = p.get("user_id")
    try:
        amount_f = float(p["amount"])
    except (TypeError, ValueError):
        amount_f = None
    balance = clients[user_id]["balance"] if user_id in clients else None
    sufficient = balance is not None and amount_f is not None and balance >= amount_f
    return {
        "transaction_id": tx_id, "amount": p["amount"], "currency": p["currency"],
        "merchant_name": p.get("merchant_name", ""), "is_on_us": p.get("is_on_us", False),
        "seconds_left": max(0, seconds_left) if seconds_left is not None else None,
        "zone": p.get("zone", BANK_ZONE), "user_id": user_id,
        "user_name": _client_name(user_id), "client_balance": balance,
        "sufficient_balance": sufficient,
    }


@app.get("/api/pending")
async def api_pending():
    _prune_expired()
    with _lock:
        return [_pending_view(tid, p) for tid, p in pending_authorizations.items()]


@app.post("/api/pending/{transaction_id}/accept")
async def api_accept(transaction_id: str, payload: Annotated[AcceptIn, Body()]):
    _prune_expired()
    with _lock:
        p = pending_authorizations.get(transaction_id)
    if not p:
        raise HTTPException(status_code=404, detail={"code": "PENDING_NOT_FOUND", "message": "Brak takiej autoryzacji."})
    user_id = p.get("user_id")
    client = clients.get(user_id) if user_id else None
    if client and payload.pin != client.get("pin", "1234"):
        raise HTTPException(status_code=422, detail={"code": "WRONG_PIN", "message": "Błędny PIN."})
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
    _log_history("AUTHORIZED", transaction_id=transaction_id, user_id=user_id,
                 user_name=_client_name(user_id), amount=p["amount"], currency=p["currency"],
                 merchant_name=p.get("merchant_name"), merchant_net=result.get("merchant_net"),
                 result=f"KLIK: {result.get('status', 'COMPLETED')}")
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
    _log_history("REJECTED", transaction_id=transaction_id, user_id=p.get("user_id"),
                 user_name=_client_name(p.get("user_id")), amount=p["amount"], reject_reason=reason)
    return result


# --------------------------------------------------------------------------
# API dla UI — P2P
# --------------------------------------------------------------------------
@app.post("/api/clients/{user_id}/register-alias")
async def api_register_alias(user_id: str, payload: Annotated[RegisterAliasIn, Body()] = RegisterAliasIn()):
    if user_id not in clients:
        raise HTTPException(status_code=404, detail={"code": "CLIENT_NOT_FOUND", "message": f"Brak klienta {user_id}"})
    client_data = clients[user_id]
    phone = payload.phone or client_data.get("phone")
    if not phone:
        raise HTTPException(status_code=422, detail={"code": "NO_PHONE", "message": "Brak numeru telefonu."})
    result = _klik_post("/aliases/register", {"phone": phone, "zone": BANK_ZONE,
                                               "account_identifier": {"type": "iban", "value": client_data["iban"]}})
    with _lock:
        registered_aliases[phone] = {"phone": phone, "user_id": user_id,
                                     "user_name": client_data["name"], "iban": client_data["iban"],
                                     "registered_at": _now().isoformat()}
    _log_history("ALIAS_REGISTERED", user_id=user_id, user_name=client_data["name"],
                 phone=phone, result="Alias zarejestrowany w KLIK")
    return result


@app.get("/api/aliases")
async def api_aliases():
    with _lock:
        return list(registered_aliases.values())


@app.delete("/api/aliases/{phone}")
async def api_delete_alias(phone: str):
    decoded_phone = phone.replace("%2B", "+").replace("%2b", "+")
    with _lock:
        alias = registered_aliases.get(decoded_phone)
    if not alias:
        raise HTTPException(status_code=404, detail={"code": "ALIAS_NOT_FOUND", "message": "Alias nie istnieje lokalnie."})
    _klik_headers_manual = {"X-KLIK-Bank-Api-Key": _klik_bank_api_key, "Idempotency-Key": str(uuid.uuid4())}
    try:
        resp = httpx.delete(f"{KLIK_BASE_URL}/aliases/{decoded_phone}", headers=_klik_headers_manual, timeout=KLIK_HTTP_TIMEOUT)
        if resp.status_code >= 400:
            raise HTTPException(status_code=resp.status_code, detail=resp.json())
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=503, detail={"code": "KLIK_UNREACHABLE", "message": str(e)})
    with _lock:
        registered_aliases.pop(decoded_phone, None)
    _log_history("ALIAS_DELETED", phone=decoded_phone, user_id=alias.get("user_id"),
                 user_name=alias.get("user_name"), result="Alias usunięty z KLIK")
    return {"deleted": True, "phone": decoded_phone}


@app.post("/api/lookup")
async def api_lookup(payload: Annotated[LookupIn, Body()]):
    try:
        resp = httpx.get(f"{KLIK_BASE_URL}/aliases/lookup/{payload.phone}",
                         headers={"X-KLIK-Bank-Api-Key": _klik_bank_api_key}, timeout=KLIK_HTTP_TIMEOUT)
        if resp.status_code == 404:
            _log_history("LOOKUP_MISS", phone=payload.phone, result="Alias nie znaleziony")
            return {"found": False, "phone": payload.phone}
        if resp.status_code >= 400:
            raise HTTPException(status_code=resp.status_code, detail=resp.json())
        data = resp.json()
        _log_history("LOOKUP_HIT", phone=payload.phone, result=f"Znaleziono: {data.get('bank_code', '?')}")
        return {"found": True, **data}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=503, detail={"code": "KLIK_UNREACHABLE", "message": str(e)})


# --------------------------------------------------------------------------
# API dla UI — Cheques (Czeki)
# --------------------------------------------------------------------------
@app.post("/api/clients/{user_id}/issue-cheque")
async def api_issue_cheque(user_id: str, payload: Annotated[IssueChequeIn, Body()]):
    """Bank wystawia czek dla klienta: blokuje środki (hold) i rejestruje w KLIK."""
    if user_id not in clients:
        raise HTTPException(status_code=404, detail={"code": "CLIENT_NOT_FOUND", "message": f"Brak klienta {user_id}"})
    client_data = clients[user_id]
    if client_data["balance"] < payload.amount:
        raise HTTPException(status_code=422, detail={"code": "INSUFFICIENT_FUNDS", "message": "Niewystarczające saldo."})

    currency = ZONE_CURRENCY.get(BANK_ZONE, "PLN")
    idempotency_key = str(uuid.uuid4())
    result = _klik_post(
        "/cheques/issue",
        {"user_id": user_id, "amount": f"{payload.amount:.2f}", "currency": currency,
         "zone": BANK_ZONE, "ttl_seconds": payload.ttl_seconds},
        idempotency_key=idempotency_key,
    )

    cheque_id = result["cheque_id"]
    with _lock:
        issued_cheques[cheque_id] = {
            "cheque_id": cheque_id, "code": result["code"], "user_id": user_id,
            "user_name": client_data["name"], "amount": payload.amount, "currency": currency,
            "status": "ACTIVE", "issued_at": result.get("issued_at"),
            "expires_at": result.get("expires_at"), "cancelled_at": None,
            "redeemed_at": None, "transaction_id": None,
        }
        # Symulacja holda — blokujemy środki klienta (zmniejszamy dostępne saldo)
        client_data["balance"] = round(client_data["balance"] - payload.amount, 2)

    _log_history("CHEQUE_ISSUED", cheque_id=cheque_id, code=result["code"], user_id=user_id,
                 user_name=client_data["name"], amount=payload.amount, currency=currency,
                 result=f"Czek wystawiony, kod: {result['code']}")
    return result


@app.get("/api/cheques")
async def api_list_cheques():
    with _lock:
        return list(issued_cheques.values())


@app.post("/api/cheques/{cheque_id}/cancel")
async def api_cancel_cheque(cheque_id: str):
    """Bank anuluje czek — hold zostanie zwolniony przez webhook /cheques/released."""
    with _lock:
        cheque = issued_cheques.get(cheque_id)
    if not cheque:
        raise HTTPException(status_code=404, detail={"code": "CHEQUE_NOT_FOUND", "message": "Czek nie istnieje."})
    if cheque["status"] != "ACTIVE":
        raise HTTPException(status_code=409,
                            detail={"code": "CHEQUE_NOT_ACTIVE",
                                    "message": f"Czek jest w stanie {cheque['status']}."})

    result = _klik_post("/cheques/cancel", {"cheque_id": cheque_id})

    with _lock:
        cheque["status"] = "CANCELLED"
        cheque["cancelled_at"] = _now().isoformat()

    _log_history("CHEQUE_CANCELLED", cheque_id=cheque_id, code=cheque["code"],
                 user_id=cheque["user_id"], user_name=cheque["user_name"],
                 amount=cheque["amount"], result="Czek anulowany w KLIK")
    return result


@app.get("/api/history")
async def api_history():
    with _lock:
        return list(reversed(history[-200:]))


@app.get("/healthz")
async def healthz():
    return {"status": "ok", "bank": BANK_NAME, "zone": BANK_ZONE}
