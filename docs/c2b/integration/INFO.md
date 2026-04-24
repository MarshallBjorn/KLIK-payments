# KLIK — Dokumentacja integracyjna

Dokument dla zespołów bankowych integrujących się z systemem KLIK.

**Wersja:** 1.0 (draft)
**Data:** 2026-04-23
**Status:** W trakcie implementacji — specyfikacja może ulec niewielkim zmianom

---

## Spis treści

1. [Słownik domenowy](#słownik-domenowy)
2. [Error codes reference](#error-codes-reference)
3. [API reference](#api-reference)
4. [Webhooki wymagane od banków](#webhooki-wymagane-od-banków)
5. [Onboarding i autentykacja](#onboarding-i-autentykacja)

---

## Słownik domenowy

Spójna terminologia używana w dokumentacji, kodzie i komunikacji między zespołami. Jeśli używasz innego słowa niż poniższe — używasz złego.

### Uczestnicy systemu

| Termin | Definicja |
|---|---|
| **KLIK** | Operator i router płatności mobilnych. Nie przechowuje środków — zarządza logiką autoryzacji (Kody) i routingiem danych (Telefony). |
| **Bank Nadawcy** | Bank klienta inicjującego płatność. Wystawia kod, autoryzuje klienta PINem, blokuje środki. Dalej w dokumencie: **Bank N**. |
| **Bank Merchanta** | Bank prowadzący konto sklepu/merchanta. W przypadku `is_on_us=true` jest to ten sam bank co Bank Nadawcy. Dalej: **Bank M**. |
| **Bank Agenta** | Bank prowadzący konto agenta rozliczeniowego (dla ang. *settlement bank*). Odbiera prowizje agenta przez RTGS. |
| **Agent (rozliczeniowy)** | Podmiot pośredniczący między sklepem/bramką a KLIK. Pobiera prowizję od transakcji. Ma umowę MSC z KLIK określającą stawki. |
| **Merchant** | Sklep lub inny punkt sprzedaży będący beneficjentem płatności. |
| **Klient** | Osoba fizyczna płacąca kodem KLIK. Ma konto w Banku Nadawcy. |
| **Operator KLIK** | Pracownik KLIK zarządzający systemem przez Django Admin. |

### Obiekty domenowe

| Termin | Definicja | Gdzie przechowywany |
|---|---|---|
| **Code (Kod)** | 6-cyfrowy numer wygenerowany przez KLIK na żądanie banku, ważny 120s. Jednorazowy. | Redis (TTL=120s) |
| **Transaction (Transakcja)** | Rekord powstający w momencie użycia kodu przez agenta. Ma cykl życia PENDING → AUTHORIZED → COMPLETED/REJECTED. | Postgres (source of truth) + Redis (cache statusu) |
| **LedgerEntry** | Pojedyncze zobowiązanie finansowe wynikające z transakcji (np. "Bank N winien Bank M 148.05 PLN"). Jedna transakcja generuje 2-3 entries. | Postgres |
| **SettlementSession** | Cykl rozliczeniowy zamykający zbiór ledger entries w ramach jednej strefy. Wyzwalany przez Celery Beat. | Postgres |
| **SettlementTransfer** | Pojedynczy przelew RTGS wygenerowany z nettingu w ramach sesji. Np. "Bank A → Bank B, 50000 PLN, netto sesji 2026-04-23". | Postgres |
| **MSCAgreement** | Umowa Merchant Service Charge między agentem a KLIK. Określa stawki prowizji (klik_fee_perc, agent_fee_perc). | Postgres |
| **Alias (P2P)** | Mapowanie numeru telefonu na parę [bank_id, iban]. Używane w module Telefony. | Postgres |

### Pojęcia procesowe

| Termin | Definicja |
|---|---|
| **Zone (Strefa)** | Obszar walutowo-krajowy. Cztery strefy: **PL** (PLN), **EU** (EUR), **UK** (GBP), **US** (USD). Transakcje odbywają się wyłącznie w obrębie jednej strefy. |
| **Zone isolation** | Zasada że KLIK odrzuca transakcje cross-zone (422_ZONE_MISMATCH). Eliminuje problem przewalutowania. |
| **is_on_us** | Flaga na Transaction wskazująca czy nadawca i merchant mają konta w tym samym banku. Upraszcza bankowi wyłapanie transakcji wewnętrznej vs międzybankowej. |
| **Netting (multilateral)** | Proces agregacji wszystkich zobowiązań sesji do pojedynczej pozycji netto na uczestnika. Redukuje liczbę przelewów RTGS. |
| **Settlement (Clearing)** | Fizyczne wykonanie przelewów RTGS po nettingu. Zmienia ledger entries z `settled=False` na `settled=True`. |
| **RTGS** | Real-Time Gross Settlement. Systemy bankowości centralnej: **SORBNET3** (PL), **TARGET2** (EU), **CHAPS** (UK), **FedNow** (US). |
| **Dispatcher RTGS** | Komponent KLIK wybierający odpowiedni RTGS gateway po strefie transakcji (strategy pattern). |
| **Webhook autoryzacyjny** | Endpoint wystawiany przez bank. KLIK uderza tam żeby bank pokazał klientowi push "Autoryzuj PINem". |
| **Confirm** | Asynchroniczne wywołanie banku do KLIK po tym jak klient zaakceptował (lub odrzucił) autoryzację. |
| **Split prowizji** | Podział kwoty brutto na merchant_net, klik_fee, agent_fee. Liczony od brutto. |

### Stany obiektów (skrót — pełne diagramy w B2/B3)

**Code:** `ACTIVE → USED | EXPIRED`
**Transaction:** `PENDING → AUTHORIZED → COMPLETED | REJECTED | TIMEOUT` → `SETTLED`
**LedgerEntry:** `PENDING_SETTLEMENT → LOCKED → SETTLED` (lub powrót do PENDING przy fail)
**SettlementSession:** `PROCESSING → COMPLETED`

---

## Error codes reference

Wszystkie błędy zwracane przez KLIK API mają jednolity format JSON:

```json
{
    "error": {
        "code": "404_CODE_EXPIRED",
        "message": "Kod utracił ważność lub nie istnieje",
        "transaction_id": "uuid-if-applicable",
        "timestamp": "2026-04-23T14:00:00Z"
    }
}
```

### Tabela błędów

| Code | HTTP | Kategoria | Kiedy występuje | Działanie banku |
|---|---|---|---|---|
| `400_BAD_REQUEST` | 400 | Walidacja | Malformed JSON, brakujące wymagane pola | Popraw payload |
| `400_INVALID_AMOUNT` | 400 | Walidacja | Kwota ≤ 0 lub przekracza limit strefy | Popraw kwotę |
| `401_UNAUTHORIZED` | 401 | Auth | Brak lub niepoprawny `X-KLIK-Api-Key` | Sprawdź credentials |
| `403_BANK_INACTIVE` | 403 | Auth | Bank zablokowany (`active=False`) | Skontaktuj się z operatorem KLIK |
| `403_INSUFFICIENT_PERMISSIONS` | 403 | Auth | Podmiot nie ma uprawnień do tego endpointu (np. agent próbuje wywołać `/codes/generate`) | Użyj właściwego endpointu |
| `403_INSUFFICIENT_FUNDS` | 403 | Biznesowy | Bank odrzucił autoryzację z powodu braku środków (przekazywany przez `/confirm`) | Informacja dla klienta |
| `404_CODE_EXPIRED` | 404 | Biznesowy | Kod nie istnieje lub minął TTL 120s | Klient generuje nowy kod |
| `404_TRANSACTION_NOT_FOUND` | 404 | Biznesowy | `transaction_id` nie istnieje | Sprawdź ID |
| `404_ALIAS_NOT_FOUND` | 404 | Biznesowy (P2P) | Numer telefonu nie zarejestrowany w KLIK | Nadawca przelewa tradycyjnie |
| `409_CODE_ALREADY_USED` | 409 | Biznesowy | Kod został już użyty w innej transakcji | Klient generuje nowy kod |
| `409_ALIAS_ALREADY_EXISTS` | 409 | Biznesowy (P2P) | Numer telefonu już przypisany do innego banku/konta | Wyrejestruj wcześniejsze przypisanie |
| `409_PREMATURE_CONFIRM` | 409 | Biznesowy | `/confirm` wywołany przed `AUTHORIZED` | Poczekaj na webhook autoryzacyjny |
| `409_IDEMPOTENCY_CONFLICT` | 409 | Walidacja | Ten sam `idempotency_key` z różnym payloadem | Użyj nowego klucza lub wyślij identyczny payload |
| `422_ZONE_MISMATCH` | 422 | Biznesowy | Strefa kodu ≠ strefa agenta, lub transakcja cross-zone | Operacja niedozwolona |
| `422_CURRENCY_MISMATCH` | 422 | Walidacja | Waluta w request ≠ waluta strefy | Popraw walutę |
| `500_INTERNAL_ERROR` | 500 | System | Nieoczekiwany błąd KLIK | Retry po pewnym czasie, zgłoś jeśli się powtarza |
| `503_REDIS_UNAVAILABLE` | 503 | System | Redis nie odpowiada (krytyczne) | Retry, KLIK przywróci dostępność |
| `503_DB_UNAVAILABLE` | 503 | System | Postgres nie odpowiada | Retry |
| `504_BANK_TIMEOUT` | 504 | System | Bank nie odpowiedział na webhook autoryzacyjny (po retry) | Transakcja oznaczona TIMEOUT |

### Konwencje dotyczące retry

- Błędy **4xx** (klient-side): **nie retryuj** bez zmiany payloadu. Wyjątek: `401` po odświeżeniu tokenu (n/d przy API keys).
- Błędy **5xx** (server-side): **retryuj z exponential backoff** (np. 1s, 5s, 30s, 2min, stop po 5 próbach).
- **Idempotency**: wszystkie endpointy mutujące (`/codes/generate`, `/payments/initiate`, `/payments/confirm`, `/aliases/register`) wymagają nagłówka `Idempotency-Key` (UUID v4). KLIK gwarantuje że retry z tym samym kluczem i tym samym payloadem zwróci ten sam wynik bez duplikowania akcji.

---

## API reference

**Bazowy URL (MVP / development):** `https://api.klik.example.com/api/v1`

Jeden deployment obsługuje wszystkie strefy. Strefa transakcji jest identyfikowana
po polu `zone` w payloadzie oraz po strefie bankowej przypisanej do `X-KLIK-Api-Key`.

**Plan produkcyjny (post-MVP):** możliwe wprowadzenie odseparowanych deploymentów per strefa
(`api.klik.{pl,eu,uk,us}.example.com`). Do uzgodnienia po MVP.

### Wspólne nagłówki

Wszystkie żądania wymagają:

```
X-KLIK-Api-Key: <klucz_wydany_przy_onboardingu>
Content-Type: application/json
Idempotency-Key: <uuid-v4> (dla żądań mutujących)
```

---

### Moduł Kody (C2B)

#### `POST /codes/generate`

**Kto wywołuje:** Bank Nadawcy (w imieniu klienta klikającego "Generuj KLIK")

**Request body:**
```json
{
    "user_id": "bank-internal-client-id-12345",
    "zone": "PL"
}
```

**Response 200:**
```json
{
    "code": "123456",
    "expires_in": 120,
    "expires_at": "2026-04-23T14:02:00Z"
}
```

**Uwagi:**
- `user_id` to wewnętrzny identyfikator klienta w banku. KLIK nie weryfikuje jego semantyki, używa tylko do korelacji przy webhooku autoryzacyjnym.
- `zone` musi się zgadzać ze strefą w której bank jest zarejestrowany.
- Kod jest krótkotrwały (120s). Bank powinien pokazać klientowi licznik.

**Możliwe błędy:** `401`, `403_BANK_INACTIVE`, `422_CURRENCY_MISMATCH`, `500`, `503_REDIS_UNAVAILABLE`

---

#### `POST /payments/initiate`

**Kto wywołuje:** Agent (sklep/bramka) gdy klient wpisuje kod i klika "Zapłać"

**Request body:**
```json
{
    "code": "123456",
    "amount": "150.00",
    "currency": "PLN",
    "merchant_name": "Sklep Żabka",
    "merchant_iban": "PL61109010140000071219812874"
}
```

**Response 202:**
```json
{
    "transaction_id": "550e8400-e29b-41d4-a716-446655440000",
    "status": "PENDING",
    "status_url": "/api/v1/payments/status/550e8400-e29b-41d4-a716-446655440000",
    "expires_at": "2026-04-23T14:02:00Z"
}
```

**Uwagi:**
- Status 202 oznacza że KLIK przyjął żądanie i zleca autoryzację bankowi nadawcy asynchronicznie. Agent powinien pollować `status_url`.
- Stawki prowizji (klik_fee, agent_fee) NIE są przesyłane — KLIK odczyta je z `MSCAgreement` agenta na podstawie `X-KLIK-Api-Key`.
- Flaga `is_on_us` jest wyliczana automatycznie przez KLIK (porównanie bank nadawcy vs bank merchanta — ten drugi to `settlement_bank` merchanta zarejestrowanego w KLIK).

**Możliwe błędy:** `400_BAD_REQUEST`, `400_INVALID_AMOUNT`, `401`, `403_BANK_INACTIVE`, `404_CODE_EXPIRED`, `409_CODE_ALREADY_USED`, `422_ZONE_MISMATCH`, `422_CURRENCY_MISMATCH`

---

#### `GET /payments/status/{transaction_id}`

**Kto wywołuje:** Agent (polling) lub Bank Nadawcy (dla weryfikacji)

**Response 200:**
```json
{
    "transaction_id": "550e8400-...",
    "status": "COMPLETED",
    "amount_gross": "150.00",
    "merchant_net": "148.05",
    "currency": "PLN",
    "created_at": "2026-04-23T14:00:00Z",
    "completed_at": "2026-04-23T14:00:08Z"
}
```

**Możliwe wartości `status`:** `PENDING`, `AUTHORIZED`, `COMPLETED`, `REJECTED`, `TIMEOUT`

**Uwagi:**
- Polling interval rekomendowany: 1-2s.
- Maksymalny czas odpytywania: do `expires_at` + 60s.

**Możliwe błędy:** `401`, `404_TRANSACTION_NOT_FOUND`

---

#### `POST /payments/confirm`

**Kto wywołuje:** Bank Nadawcy po otrzymaniu odpowiedzi od klienta (PIN zaakceptowany/odrzucony)

**Request body:**
```json
{
    "transaction_id": "550e8400-...",
    "status": "ACCEPTED",
    "authorization_timestamp": "2026-04-23T14:00:07Z"
}
```

**Alternatywny request (odrzucenie):**
```json
{
    "transaction_id": "550e8400-...",
    "status": "REJECTED",
    "reject_reason": "INSUFFICIENT_FUNDS"
}
```

**Dozwolone `reject_reason`:** `INSUFFICIENT_FUNDS`, `USER_DECLINED`, `PIN_FAILED`, `AML_BLOCK`, `OTHER`

**Response 200:**
```json
{
    "transaction_id": "550e8400-...",
    "status": "COMPLETED",
    "ledger_entries_count": 3
}
```

**Uwagi:**
- Endpoint jest idempotentny. Powtórny call z tym samym statusem zwraca 200 bez efektu.
- Bank MUSI wywołać `/confirm` w ciągu 60s od odpowiedzi na webhook autoryzacyjny. Brak confirmu w tym oknie = transakcja timeout.
- Po `ACCEPTED`: KLIK zapisuje ledger entries, transakcja → COMPLETED, status widoczny dla agenta.
- Po `REJECTED`: brak ledger entries, transakcja → REJECTED, agent dostaje błąd.

**Możliwe błędy:** `401`, `403` (bank inny niż bank nadawcy kodu), `404_TRANSACTION_NOT_FOUND`, `409_PREMATURE_CONFIRM`, `422` (invalid status)

---

### Moduł Telefony (P2P)

#### `POST /aliases/register`

**Kto wywołuje:** Bank klienta po włączeniu przez klienta funkcji "Przelew na telefon"

**Request body:**
```json
{
    "phone": "+48501234567",
    "iban": "PL61109010140000071219812874",
    "zone": "PL"
}
```

**Response 201:**
```json
{
    "alias_id": "...",
    "phone": "+48501234567",
    "registered_at": "2026-04-23T14:00:00Z"
}
```

**Uwagi:**
- `phone` w formacie E.164 (z prefiksem kraju).
- `zone` musi być zgodna z prefiksem numeru telefonu (np. `+48` → PL).
- Jeden numer może być zarejestrowany tylko raz w danej strefie.

**Możliwe błędy:** `400_BAD_REQUEST`, `401`, `409_ALIAS_ALREADY_EXISTS`, `422_ZONE_MISMATCH`

---

#### `GET /aliases/lookup/{phone}`

**Kto wywołuje:** Bank Nadawcy przelewu P2P, żeby wiedzieć dokąd routować środki

**Response 200:**
```json
{
    "phone": "+48501234567",
    "bank_id": "BANK_A_UUID",
    "bank_code": "BANK_A",
    "iban": "PL61109010140000071219812874"
}
```

**Uwagi:**
- Bank nadawcy używa zwróconego IBAN-u do wysłania przelewu przez Elixir Express / Faster Payments / SEPA Instant / FedNow RTP (nie przez KLIK).
- KLIK nie uczestniczy dalej w transferze środków P2P (wersja 1.0 — bez clearingu P2P).
- Możliwy retention lookupów dla celów audytu i fraud detection (szczegóły u operatora KLIK).

**Możliwe błędy:** `401`, `404_ALIAS_NOT_FOUND`

---

#### `DELETE /aliases/{phone}`

**Kto wywołuje:** Bank klienta przy wyłączeniu funkcji lub zamknięciu konta

**Response 204:** (no content)

**Uwagi:**
- Bank może usuwać tylko aliasy swoich klientów. Próba usunięcia cudzego aliasu = `403`.

**Możliwe błędy:** `401`, `403_INSUFFICIENT_PERMISSIONS`, `404_ALIAS_NOT_FOUND`

---

## Webhooki wymagane od banków

Bank musi wystawić **jeden endpoint** dostępny dla KLIK. URL rejestrowany przy onboardingu.

### `POST {bank_webhook_url}/authorize`

**Kto wywołuje:** KLIK (Celery Worker) asynchronicznie po przyjęciu `/payments/initiate`

**Payload od KLIK:**
```json
{
    "transaction_id": "550e8400-...",
    "user_id": "bank-internal-client-id-12345",
    "amount": "150.00",
    "currency": "PLN",
    "merchant_name": "Sklep Żabka",
    "is_on_us": false,
    "expiry_time": "2026-04-23T14:01:00Z",
    "zone": "PL"
}
```

**Oczekiwana odpowiedź (synchroniczna):**

Happy path (bank przyjął żądanie do procesowania):
```json
HTTP 200 OK
{
    "received": true,
    "will_prompt_user": true
}
```

**Uwagi:**
- Ta odpowiedź oznacza tylko że bank **przyjął żądanie do pokazania klientowi**, nie że klient zaakceptował. Decyzja klienta idzie **osobnym kanałem** — bank wywołuje `POST /payments/confirm` do KLIK.
- KLIK czeka na tę odpowiedź max 30s. Timeout = retry (3 próby z backoff).
- Po 3 failach transakcja → TIMEOUT.
- `is_on_us` pomaga bankowi zdecydować czy potrzebne jest zaangażowanie rozliczeń międzybankowych, czy wystarczy ruch wewnętrzny.

### (Opcjonalnie) `POST {bank_webhook_url}/ping`

**Kto wywołuje:** KLIK podczas onboardingu i periodycznie dla healthcheck

**Payload:**
```json
{
    "timestamp": "2026-04-23T14:00:00Z",
    "nonce": "random-string"
}
```

**Oczekiwana odpowiedź:**
```json
HTTP 200 OK
{
    "timestamp": "2026-04-23T14:00:00Z",
    "nonce": "random-string",
    "pong": true
}
```

**Uwagi:**
- Bank musi zwrócić ten sam `timestamp` i `nonce` — to proof-of-liveness.
- Używane przy pierwszej rejestracji webhook URL (ścieżka A0 w diagramach).

---

## Onboarding i autentykacja

### Proces onboardingu (skrót — pełny flow w BPMN)

1. Bank kontaktuje operatora KLIK (email, umowa) — poza systemem.
2. Operator tworzy rekord banku w Django Admin, generuje `api_key`.
3. Operator przekazuje `api_key` bankowi bezpiecznym kanałem.
4. Bank konfiguruje `KLIK_API_KEY` i URL API u siebie.
5. Bank wywołuje `POST /banks/webhook-config` rejestrując swój endpoint webhooka.
6. KLIK wykonuje ping do zarejestrowanego URL-a. Jeśli OK — ustawia `active=True`.
7. Bank może wywoływać produkcyjne endpointy.

### Autentykacja

**W fazie obecnej (MVP):** statyczny `X-KLIK-Api-Key` w nagłówku każdego żądania.

**Plan produkcyjny (post-MVP):** HMAC-SHA256 podpis payloadu z timestampem, weryfikacja sygnatury KLIK przy webhookach (nagłówek `X-KLIK-Signature`). Szczegóły w osobnym dokumencie Security.

### Idempotency

Wszystkie endpointy mutujące wymagają nagłówka:
```
Idempotency-Key: <uuid-v4>
```

- Ten sam klucz + ten sam payload = zwrócenie oryginalnego wyniku (bez duplikacji).
- Ten sam klucz + inny payload = `409_IDEMPOTENCY_CONFLICT`.
- KLIK przechowuje idempotency_key przez 24h od utworzenia transakcji.

### Rate limiting (planowane)

Limity per bank (planowane, nie obowiązujące w MVP):
- `/codes/generate`: 100 req/s
- `/payments/*`: 500 req/s
- `/aliases/*`: 50 req/s

Przekroczenie = `429 Too Many Requests` z nagłówkiem `Retry-After`.

---

## Wersjonowanie API

- API jest wersjonowane przez prefix URL (`/api/v1/...`).
- Breaking changes = nowa wersja (`/api/v2/...`). Stara wersja wspierana min. 6 miesięcy po release nowej.
- Zmiany niebędące breaking (nowe optional pola, nowe endpointy) publikowane w ramach tej samej wersji.
- Lista zmian: `CHANGELOG.md` w repo KLIK (TBD).

---

## Kontakt

- **Dokumentacja techniczna:** `docs/` w repo KLIK
- **Diagramy:** `docs/diagrams/` (UML), `docs/bpmn/` (BPMN)
- **Zgłoszenia integracyjne:** przez operatora KLIK (poza systemem w MVP)
  