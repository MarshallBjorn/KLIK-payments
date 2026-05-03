# Mock RTGS

Symulator 4 systemów bankowości centralnej (SORBNET3 / TARGET2 / CHAPS / FedNow)
dla developmentu i demo systemu KLIK.

## Endpoints

Każdy z 4 systemów wystawia ten sam protokół pod swoim prefiksem:

```
POST /sorbnet3/settle    POST /target2/settle    POST /chaps/settle    POST /fednow/settle
GET  /sorbnet3/healthz   GET  /target2/healthz   GET  /chaps/healthz   GET  /fednow/healthz
```

### Settle

```http
POST /sorbnet3/settle
Content-Type: application/json

{
  "session_id": "uuid",
  "transfer_id": "uuid",
  "system": "SORBNET3",
  "from": "Bank A",
  "to": "Bank B",
  "amount": "265.00",
  "currency": "PLN"
}
```

Odpowiedź:

```json
{
  "transfer_id": "uuid",
  "status": "SUCCESS",
  "rtgs_reference": "SORBNET3-A1B2C3D4E5F6",
  "failure_reason": ""
}
```

## Failure injection

| ENV | Default | Opis |
|---|---|---|
| `RTGS_LATENCY_MIN_MS` | 50 | Min latency per transfer |
| `RTGS_LATENCY_MAX_MS` | 300 | Max latency per transfer |
| `RTGS_FAIL_RATE` | 0.0 | Prawdopodobieństwo FAILED (0.0-1.0) |
| `RTGS_TIMEOUT_RATE` | 0.0 | Prawdopodobieństwo HTTP 504 (0.0-1.0) |
| `RTGS_BLACKLIST` | "" | Comma-separated lista nazw banków zwracających FAILED |

## Admin (live management)

```http
# Pokaż obecną blacklist
GET /admin/blacklist

# Dodaj banki do blacklist
POST /admin/blacklist
{"banks": ["Bad Bank", "Insolvent Bank"]}

# Wyczyść blacklist
DELETE /admin/blacklist

# Wyczyść cache idempotency
POST /admin/reset-cache

# Statystyki + konfiguracja
GET /admin/stats
```

## Lokalne uruchomienie

```bash
# Z poziomu rtgs_mock/
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 9000

# Test
curl -X POST http://localhost:9000/sorbnet3/settle \
  -H 'Content-Type: application/json' \
  -d '{"session_id":"s1","transfer_id":"t1","system":"SORBNET3","from":"A","to":"B","amount":"100.00","currency":"PLN"}'
```

W docker-compose serwis ustawiony jako `rtgs-mock:9000` — host name dla
backendu KLIK.
