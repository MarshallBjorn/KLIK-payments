# KLIK Cheques — Dokumentacja integracyjna

Dokument dla zespołów bankowych integrujących się z modułem **Czeki (Cheques)**
systemu KLIK. Zawiera słownik domenowy, model rozliczeniowy, referencję
błędów oraz specyfikację API.

**Wersja:** 1.0 (draft)
**Data:** 2026-05-03
**Status:** W trakcie projektowania — specyfikacja może ulec zmianom

> **Powiązane dokumenty:**
> - [C2B INFO.md](../../c2b/integration/INFO.md) — moduł Kody, model bazowy
> - [Cheques WORKFLOW.md](../diagrams/WORKFLOW.md) — diagramy sekwencji (CH0-CH4)
> - [Cheques STATE.md](../diagrams/STATE.md) — diagramy stanów + ERD update
> - [C2B WORKFLOW A5](../../c2b/diagrams/WORKFLOW.md#a5--netting--settlement-przez-rtgs) — settlement (wspólny dla C2B, P2P, Cheques)

---

## Spis treści

1. [Słownik domenowy](#słownik-domenowy)
2. [Model rozliczeniowy](#model-rozliczeniowy)
3. [Format kodu i TTL](#format-kodu-i-ttl)
4. [Error codes reference](#error-codes-reference)
5. [API reference](#api-reference)
6. [Webhooki wymagane od banków](#webhooki-wymagane-od-banków)
7. [Pricing](#pricing)
8. [Onboarding i autentykacja](#onboarding-i-autentykacja)

---

## Słownik domenowy

Pojęcia ogólne (Bank, Zone, Agent, Merchant, RTGS, SettlementSession itp.)
w [C2B INFO.md](../../c2b/integration/INFO.md#słownik-domenowy). Tu tylko pojęcia
specyficzne dla modułu Czeków.

### Obiekty domenowe

| Termin | Definicja | Gdzie przechowywany |
|---|---|---|
| **Cheque** | 9-cyfrowy kod o ustalonej kwocie i długim TTL (1h–72h), wystawiony przez bank klienta. Realizowany **jednorazowo** przez agenta w punkcie sprzedaży. Środki blokowane u nadawcy w momencie wystawienia. | Postgres (długi cykl życia, audyt) |
| **Cheque Transaction** | `Transaction` (model C2B) wygenerowana w momencie realizacji czeku. Posiada FK do `Cheque` (`cheque_id`) i nie wymaga webhooka autoryzacyjnego (autoryzacja była przy wystawieniu). | Postgres |
| **Hold (po stronie banku)** | Blokada środków na koncie klienta po stronie banku wystawcy. KLIK **nie reprezentuje** holda we własnym ledgerze — bank zarządza nim wewnętrznie. KLIK tylko notyfikuje o końcu cyklu (redeemed/released). | System banku (poza KLIK) |

### Pojęcia procesowe

| Termin | Definicja |
|---|---|
| **Issue (Wystawienie)** | Klient w aplikacji bankowej generuje czek o określonej kwocie. Bank lokalnie blokuje środki, następnie rejestruje czek w KLIK i pokazuje klientowi 9-cyfrowy kod. |
| **Redeem (Realizacja)** | Agent (sklep/bramka/bankomat) wczytuje kod od klienta, KLIK atomowo zmienia stan czeku na REDEEMED, tworzy `Transaction` w stanie `COMPLETED` od razu (split prowizji jak w C2B), notyfikuje bank wystawcy. |
| **Cancel (Anulacja)** | Klient w aplikacji bankowej kasuje wystawiony czek. Bank wywołuje `POST /cheques/cancel`, KLIK zmienia stan na CANCELLED, notyfikuje bank o release. |
| **Expire (Wygaśnięcie)** | Cron job KLIK skanuje czeki z `expires_at <= now` i `status=ACTIVE`, zmienia stan na EXPIRED, notyfikuje bank o release. |
| **Release (Zwolnienie holda)** | Webhook KLIK → bank wystawcy informujący że klient odzyskuje dostęp do zablokowanych środków (po CANCELLED lub EXPIRED). |
| **Cheque-enabled bank** | Bank z flagą `cheques_enabled=True`. Bank może być aktywny w C2B/P2P bez Cheques (i odwrotnie) — moduły aktywowane niezależnie. |

### Stany (skrót — pełne diagramy w [STATE.md](../diagrams/STATE.md))

**Cheque:** `ACTIVE → REDEEMED | CANCELLED | EXPIRED`
**Transaction (cheque-redemption):** `COMPLETED → SETTLED` (pomija PENDING/AUTHORIZED — autoryzacja była przy issue)

---

## Model rozliczeniowy

### Kto trzyma środki

KLIK **nie przechowuje depozytu**. Środki są blokowane na koncie klienta przez
**bank wystawcy** zanim ten zarejestruje czek w KLIK. Spójne z resztą architektury
(KLIK = orkiestrator, banki = custodian).

### Kiedy księgujemy

| Etap | Akcja po stronie banku wystawcy | Akcja w ledgerze KLIK |
|---|---|---|
| **Issue** | Blokada środków klienta (hold w banku, saldo dostępne maleje) | **Brak** entries — KLIK tylko rejestruje fakt istnienia czeku |
| **Redeem** | Bank dostanie webhook `/cheques/redeemed` → debet konta klienta, zwolnienie holda | **3 entries** (lub 2 dla on-us) jak w C2B: `BANK_TO_BANK` (merchant_net), `KLIK_FEE_C2B`, `AGENT_FEE`. Trafiają do najbliższej sesji nettingowej. |
| **Cancel** | Bank dostanie webhook `/cheques/released` → zwolnienie holda bez debetu | **Brak** entries |
| **Expire** | Bank dostanie webhook `/cheques/released` → zwolnienie holda bez debetu | **Brak** entries |

Czyli ledger KLIK widzi czek **dopiero przy redempcji** i wtedy zachowuje się
identycznie jak C2B. Cykl życia holda jest po stronie banku.

### Powiązanie z C2B Transaction

Realizacja czeku tworzy zwykły rekord `Transaction` z dodatkowym polem `cheque_id`
(FK do Cheque, nullable). Pozwala to:
- Reużyć cały kod ledgera, nettingu, dispatcher RTGS
- Polować status przez `GET /payments/status/{transaction_id}` jak w zwykłym C2B
- Trzymać audit trail "z czego wzięła się ta transakcja"

Różnica względem regularnego C2B Transaction:
- `Transaction.status` startuje od razu jako `COMPLETED` (nie ma PENDING/AUTHORIZED, bo autoryzacja była już przy `Cheque.issue`)
- `Transaction.code_snapshot` zawiera kod czeku (9 cyfr), nie zwykły 6-cyfrowy

---

## Format kodu i TTL

### Format kodu

- **9 cyfr** (`000000000`–`999999999`)
- 1 mld kombinacji daje akceptowalny margines bezpieczeństwa dla TTL do 72h
- Generacja: `secrets.randbelow(1_000_000_000)`, sformatowane na 9 znaków z leading zeros
- Kolizje rozwiązywane jak w C2B: insert atomic z constraint unique na `(code, status=ACTIVE)`, retry do 10 prób

> Świadomie NIE używamy 6 cyfr: przy potencjalnie 100k aktywnych czekach naraz
> (cała Polska, 72h okno) prawdopodobieństwo kolizji to ~10%. 9 cyfr daje
> efektywnie zero.

### TTL

Konfiguracja przez `.env` (globalnie, nie per strefa w MVP):

| Zmienna | Default | Znaczenie |
|---|---|---|
| `CHEQUE_TTL_MIN_SECONDS` | `3600` (1h) | Dolne ograniczenie — bank nie może wystawić czeku z TTL < tej wartości |
| `CHEQUE_TTL_MAX_SECONDS` | `259200` (72h) | Górne ograniczenie — bank nie może wystawić czeku z TTL > tej wartości |
| `CHEQUE_TTL_DEFAULT_SECONDS` | `86400` (24h) | TTL użyty gdy bank nie poda `ttl_seconds` w request |

Bank podaje `ttl_seconds` w `POST /cheques/issue` lub pomija — wtedy default.
Wartości spoza widełek → `400_INVALID_TTL`.

---

## Error codes reference

Format błędów spójny z C2B/P2P:

```json
{
    "error": {
        "code": "404_CHEQUE_EXPIRED",
        "message": "Czek wygasł lub nie istnieje.",
        "cheque_id": "uuid-if-applicable",
        "timestamp": "2026-05-03T14:00:00Z"
    }
}
```

### Tabela błędów

| Code | HTTP | Kategoria | Kiedy występuje | Działanie banku/agenta |
|---|---|---|---|---|
| `400_BAD_REQUEST` | 400 | Walidacja | Malformed JSON, brakujące wymagane pola | Popraw payload |
| `400_INVALID_AMOUNT` | 400 | Walidacja | Kwota ≤ 0 lub przekracza limit strefy | Popraw kwotę |
| `400_INVALID_TTL` | 400 | Walidacja | `ttl_seconds` poza widełkami `[CHEQUE_TTL_MIN_SECONDS, CHEQUE_TTL_MAX_SECONDS]` | Popraw TTL |
| `401_UNAUTHORIZED` | 401 | Auth | Brak/zły API key | Sprawdź credentials |
| `403_BANK_INACTIVE` | 403 | Auth | Bank zablokowany | Skontaktuj się z operatorem |
| `403_CHEQUES_NOT_ENABLED` | 403 | Auth | `bank.cheques_enabled=False` | Skontaktuj się z operatorem KLIK aby aktywować moduł |
| `403_INSUFFICIENT_PERMISSIONS` | 403 | Auth | Bank próbuje anulować czek innego banku, agent próbuje zrealizować w innej strefie itp. | Operacja niedozwolona |
| `404_CHEQUE_EXPIRED` | 404 | Biznesowy | Kod nie istnieje lub czek wygasł (status EXPIRED) | Klient generuje nowy czek |
| `404_CHEQUE_NOT_FOUND` | 404 | Biznesowy | Brak czeku o podanym `cheque_id` | Sprawdź ID |
| `409_CHEQUE_ALREADY_REDEEMED` | 409 | Biznesowy | Czek został już zrealizowany | Brak akcji — czek jednorazowy |
| `409_CHEQUE_ALREADY_CANCELLED` | 409 | Biznesowy | Czek został już anulowany | Brak akcji |
| `409_CHEQUE_NOT_ACTIVE` | 409 | Biznesowy | Próba operacji (cancel/redeem) na czeku w stanie nie-ACTIVE | Sprawdź status |
| `409_IDEMPOTENCY_CONFLICT` | 409 | Walidacja | Ten sam `Idempotency-Key` z innym payloadem | Użyj nowego klucza |
| `422_ZONE_MISMATCH` | 422 | Biznesowy | Strefa czeku ≠ strefa agenta realizującego | Operacja niedozwolona |
| `422_CURRENCY_MISMATCH` | 422 | Walidacja | Waluta w request ≠ waluta strefy | Popraw walutę |
| `500_INTERNAL_ERROR` | 500 | System | Nieoczekiwany błąd | Retry, zgłoś jeśli się powtarza |
| `503_DB_UNAVAILABLE` | 503 | System | Postgres nie odpowiada | Retry z backoff |

### Konwencje retry

Identyczne z C2B/P2P:
- 4xx: nie retryuj bez zmiany payloadu
- 5xx: exponential backoff (1s, 5s, 30s, 2min, stop po 5 próbach)
- Wszystkie endpointy mutujące (`/cheques/issue`, `/cheques/redeem`, `/cheques/cancel`) wymagają `Idempotency-Key`

---

## API reference

**Bazowy URL:** `https://api.klik.example.com/api/v1`

### Wspólne nagłówki

```
X-KLIK-Bank-Api-Key: <klucz_banku>     (dla /issue, /cancel, /status)
X-KLIK-Agent-Api-Key: <klucz_agenta>   (dla /redeem, /status)
Content-Type: application/json
Idempotency-Key: <uuid-v4>             (dla operacji mutujących)
```

---

### `POST /cheques/issue`

**Kto wywołuje:** Bank wystawcy **PO** zablokowaniu środków klienta po swojej stronie i autoryzacji PIN-em.

**Request body:**
```json
{
    "user_id": "bank-internal-client-id-12345",
    "amount": "100.00",
    "currency": "PLN",
    "zone": "PL",
    "ttl_seconds": 86400
}
```

`ttl_seconds` jest opcjonalne. Pominięcie = `CHEQUE_TTL_DEFAULT_SECONDS`.

**Response 201:**
```json
{
    "cheque_id": "550e8400-e29b-41d4-a716-446655440000",
    "code": "123456789",
    "amount": "100.00",
    "currency": "PLN",
    "expires_at": "2026-05-04T14:00:00Z",
    "issued_at": "2026-05-03T14:00:00Z"
}
```

**Uwagi:**
- Bank MUSI mieć już zablokowane środki **przed** wywołaniem tego endpointu. KLIK ufa bankowi.
- Pole `code` jest pokazywane klientowi tylko w tej odpowiedzi — KLIK nie wystawia endpointu do "pobierz mój kod ponownie" z bezpieczeństwa.
- `expires_at` = `issued_at + ttl_seconds`.
- Strefa `zone` musi się zgadzać ze strefą banku.

**Możliwe błędy:** `400`, `400_INVALID_TTL`, `400_INVALID_AMOUNT`, `401`, `403_BANK_INACTIVE`, `403_CHEQUES_NOT_ENABLED`, `422_CURRENCY_MISMATCH`, `500`, `503`

---

### `POST /cheques/redeem`

**Kto wywołuje:** Agent (sklep/bramka/bankomat) gdy klient wpisuje kod czeku.

**Request body:**
```json
{
    "cheque_code": "123456789",
    "merchant_id": "merchant-uuid"
}
```

**Response 200:**
```json
{
    "cheque_id": "550e8400-...",
    "transaction_id": "660e8400-...",
    "amount_gross": "100.00",
    "merchant_net": "98.70",
    "klik_fee": "0.50",
    "agent_fee": "0.80",
    "currency": "PLN",
    "redeemed_at": "2026-05-03T14:30:00Z"
}
```

**Uwagi:**
- Operacja **synchroniczna** i **atomowa** — jednym requestem czek przechodzi `ACTIVE → REDEEMED`, powstaje `Transaction(status=COMPLETED)`, ledger entries idą do bazy.
- W przeciwieństwie do `/payments/initiate` nie ma pollingu — wynik znany od razu (autoryzacja była przy issue).
- Agent NIE podaje kwoty — jest zaszyta w czeku.
- Notyfikacja banku wystawcy (`/cheques/redeemed`) idzie **asynchronicznie** przez Celery — odpowiedź dla agenta nie czeka na bank.

**Możliwe błędy:** `400`, `401`, `404_CHEQUE_EXPIRED`, `404_CHEQUE_NOT_FOUND`, `409_CHEQUE_ALREADY_REDEEMED`, `409_CHEQUE_ALREADY_CANCELLED`, `409_CHEQUE_NOT_ACTIVE`, `422_ZONE_MISMATCH`

---

### `POST /cheques/cancel`

**Kto wywołuje:** Bank wystawcy gdy klient kasuje czek w aplikacji bankowej.

**Request body:**
```json
{
    "cheque_id": "550e8400-..."
}
```

**Response 200:**
```json
{
    "cheque_id": "550e8400-...",
    "status": "CANCELLED",
    "cancelled_at": "2026-05-03T15:00:00Z"
}
```

**Uwagi:**
- Tylko bank wystawcy może anulować czek. Próba przez inny bank = `403_INSUFFICIENT_PERMISSIONS`.
- Działa tylko na czekach w stanie ACTIVE. REDEEMED/CANCELLED/EXPIRED → `409_CHEQUE_NOT_ACTIVE`.
- Po cancel KLIK kolejkuje webhook `/cheques/released` do banku (asynchronicznie) — bank zwalnia hold.
- Idempotency: drugi cancel z tym samym `Idempotency-Key` zwraca to samo bez duplikacji.

**Możliwe błędy:** `401`, `403_BANK_INACTIVE`, `403_CHEQUES_NOT_ENABLED`, `403_INSUFFICIENT_PERMISSIONS`, `404_CHEQUE_NOT_FOUND`, `409_CHEQUE_NOT_ACTIVE`

---

### `GET /cheques/status/{cheque_id}`

**Kto wywołuje:** Bank wystawcy (audit) lub agent który zrealizował czek.

**Response 200:**
```json
{
    "cheque_id": "550e8400-...",
    "status": "REDEEMED",
    "amount": "100.00",
    "currency": "PLN",
    "zone": "PL",
    "issued_at": "2026-05-03T14:00:00Z",
    "expires_at": "2026-05-04T14:00:00Z",
    "redeemed_at": "2026-05-03T14:30:00Z",
    "transaction_id": "660e8400-..."
}
```

**Możliwe wartości `status`:** `ACTIVE`, `REDEEMED`, `CANCELLED`, `EXPIRED`

**Uwagi:**
- Bank widzi tylko swoje czeki. Agent widzi tylko czeki które sam zrealizował.
- Inne podmioty → `404_CHEQUE_NOT_FOUND` (security — nie ujawniamy istnienia).

**Możliwe błędy:** `401`, `404_CHEQUE_NOT_FOUND`

---

## Webhooki wymagane od banków

Bank wystawcy musi wystawić **dwa endpointy** dla notyfikacji o końcu cyklu czeku.
URL rejestrowane przy onboardingu (`Bank.cheques_webhook_url`, lub `Bank.webhook_url` jako fallback z suffixem `/cheques`).

### `POST {bank_cheques_webhook_url}/redeemed`

**Kto wywołuje:** KLIK (Celery worker) po udanej realizacji czeku.

**Payload od KLIK:**
```json
{
    "cheque_id": "550e8400-...",
    "transaction_id": "660e8400-...",
    "amount": "100.00",
    "currency": "PLN",
    "redeemed_at": "2026-05-03T14:30:00Z",
    "user_id": "bank-internal-client-id-12345"
}
```

**Oczekiwana odpowiedź:**
```json
HTTP 200 OK
{"received": true}
```

**Uwagi:**
- Bank powinien:
  1. Zwolnić hold na koncie klienta
  2. Zaksięgować debet w wysokości `amount`
  3. Powiadomić klienta (push)
- Settlement międzybankowy (przelew RTGS od bank wystawcy do bank merchanta) odbywa się **niezależnie**, w cyklu sesji nettingowej KLIK.
- KLIK retryuje 3x z exponential backoff (5s, 30s, 2min). Po 3 failach: alert dla operatora (manualny redrive).

### `POST {bank_cheques_webhook_url}/released`

**Kto wywołuje:** KLIK po anulacji lub wygaśnięciu czeku.

**Payload od KLIK:**
```json
{
    "cheque_id": "550e8400-...",
    "amount": "100.00",
    "currency": "PLN",
    "reason": "EXPIRED",
    "released_at": "2026-05-04T14:00:00Z",
    "user_id": "bank-internal-client-id-12345"
}
```

**Możliwe wartości `reason`:** `CANCELLED`, `EXPIRED`

**Oczekiwana odpowiedź:**
```json
HTTP 200 OK
{"received": true}
```

**Uwagi:**
- Bank zwalnia hold bez debetu — środki wracają do dostępnego salda klienta.
- Retry policy: jak `/redeemed`.

---

## Pricing

W MVP czek dziedziczy **stawki MSC agenta** który go realizuje (te same `klik_fee_perc` i `agent_fee_perc` co dla zwykłego C2B). Logika splitu w `AgentService.calculate_split` — bez zmian.

**Plan post-MVP:** osobne `MSCAgreement.cheque_klik_fee_perc` jeśli operator KLIK chce pobierać surcharge za "koszt utrzymania holda" (czek 72h zajmuje miejsce w ledgerze longer than typical C2B). Do uzgodnienia.

Wystawienie i anulacja czeku są **darmowe** dla banku — KLIK nie pobiera prowizji za sam fakt issue/cancel/expire (analogicznie do P2P register/delete).

---

## Onboarding i autentykacja

### Proces onboardingu (skrót)

1. Bank kontaktuje operatora KLIK (poza systemem)
2. Operator w Django Admin:
   - Aktywuje moduł: `bank.cheques_enabled = True`
   - Ustawia `bank.cheques_webhook_url` (lub fallback do `webhook_url`)
3. Bank konfiguruje endpointy `/cheques/redeemed` i `/cheques/released` u siebie
4. Bank wykonuje testowy `POST /cheques/issue` — sukces oznacza gotowość

> **Banki już zintegrowane z C2B/P2P** używają tego samego API key — operator
> tylko zaznacza flagę `cheques_enabled` i ustawia URL.

### Autentykacja

- `/cheques/issue`, `/cheques/cancel`, `/cheques/status` — `X-KLIK-Bank-Api-Key`
- `/cheques/redeem`, `/cheques/status` — `X-KLIK-Agent-Api-Key`

### Idempotency

Endpointy mutujące (`/issue`, `/redeem`, `/cancel`) przyjmują `Idempotency-Key` (UUID v4).
Reguły identyczne z C2B/P2P.

---

## Wersjonowanie API

Wspólne z C2B/P2P przez prefix `/api/v1/`. Lista zmian: `CHANGELOG.md`.

---

## Kontakt

- **Dokumentacja techniczna:** `docs/cheques/`
- **Diagramy:** `docs/cheques/diagrams/` (Mermaid)
- **Zgłoszenia integracyjne:** przez operatora KLIK
