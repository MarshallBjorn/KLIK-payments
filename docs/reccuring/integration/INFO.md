# KLIK Recurring — Dokumentacja integracyjna

Dokument dla zespołów bankowych integrujących się z modułem **Regularne transfery
(Recurring)** systemu KLIK. Zawiera słownik domenowy, model rozliczeniowy,
referencję błędów oraz specyfikację API.

**Wersja:** 1.0 (draft)
**Data:** 2026-05-03
**Status:** W trakcie projektowania — specyfikacja może ulec zmianom

> **Powiązane dokumenty:**
> - [P2P INFO.md](../../p2p/integration/INFO.md) — moduł Telefony, model bazowy (Recurring opiera się na aliasach)
> - [Recurring WORKFLOW.md](../diagrams/WORKFLOW.md) — diagramy sekwencji (R0-R6)
> - [Recurring STATE.md](../diagrams/STATE.md) — diagramy stanów + ERD update
> - [P2P WORKFLOW.md](../../p2p/diagrams/WORKFLOW.md) — sekwencje P2P (Recurring używa lookupu)

---

## Spis treści

1. [Słownik domenowy](#słownik-domenowy)
2. [Model rozliczeniowy](#model-rozliczeniowy)
3. [Schedule i kalendarz wykonania](#schedule-i-kalendarz-wykonania)
4. [Failure handling i auto-pause](#failure-handling-i-auto-pause)
5. [Error codes reference](#error-codes-reference)
6. [API reference](#api-reference)
7. [Webhooki wymagane od banków](#webhooki-wymagane-od-banków)
8. [Pricing](#pricing)
9. [Onboarding i autentykacja](#onboarding-i-autentykacja)

---

## Słownik domenowy

Pojęcia ogólne (Bank, Zone, Alias, RTP, ...) w [P2P INFO.md](../../p2p/integration/INFO.md#słownik-domenowy).
Tu tylko pojęcia specyficzne dla Recurring.

### Obiekty domenowe

| Termin | Definicja | Gdzie przechowywany |
|---|---|---|
| **RecurringTransfer (Mandate)** | Definicja zlecenia stałego: kto płaci, komu (przez alias P2P), ile, jak często, od kiedy do kiedy. Tworzony raz, podpisywany PIN-em u banku wystawcy. | Postgres (długi cykl życia) |
| **RecurringExecution (Run)** | Pojedyncze wykonanie zlecenia w określonym dniu. Generowane przez cron KLIK w momencie nadejścia `next_run_at`. Stanowi audit trail "co kiedy poszło". | Postgres |
| **Mandate (po stronie banku)** | Lokalna reprezentacja zlecenia w banku — referencja do `recurring_transfer_id` + `mandate_signed_at`. KLIK ufa że bank ma to lokalnie i autoryzował klienta. | System banku (poza KLIK) |

### Pojęcia procesowe

| Termin | Definicja |
|---|---|
| **Mandate signing** | Klient w aplikacji bankowej akceptuje PIN-em treść zlecenia ("co miesiąc 50 PLN do +48..."). Bank rejestruje mandate u siebie i wywołuje `POST /recurring/create` w KLIK. **Autoryzacja jednorazowa** — kolejne wykonania nie wymagają potwierdzenia klienta. |
| **Execution (Run)** | Pojedyncze wykonanie zlecenia. KLIK trigger-uje przez webhook `/recurring/execute` do banku nadawcy, bank wykonuje przelew RTP poza KLIK, odpowiada synchronicznie z wynikiem. |
| **Recipient lookup** | Przy każdej execution KLIK robi lookup aliasu odbiorcy (świeży, nie cachowany) i dołącza dane routingu do webhooka. **Bank nadawcy jest naliczany za lookup** (p2p_lookup_fee per run). |
| **Auto-pause** | Mechanizm bezpieczeństwa: po 3 nieudanych executions z rzędu mandate jest automatycznie pauzowany. Klient musi wykonać `resume` żeby kolejne runy weszły. |
| **Recurring-enabled bank** | Bank z flagą `recurring_enabled=True`. Niezależna od C2B/P2P/Cheques. Wymaga też `p2p_enabled=True` (lookup aliasu jest mechanizmem P2P). |

### Stany (skrót — pełne diagramy w [STATE.md](../diagrams/STATE.md))

**RecurringTransfer:** `ACTIVE ↔ PAUSED → CANCELLED | COMPLETED`
**RecurringExecution:** `SCHEDULED → EXECUTING → SUCCESS | FAILED | SKIPPED`

---

## Model rozliczeniowy

### Kto przesyła pieniądze

Tak jak w P2P — KLIK **nie uczestniczy w transferze środków**. Bank nadawcy
wykonuje przelew bezpośrednio przez RTP (Elixir Express / Faster Payments /
SEPA Instant / FedNow RTP) na konto odbiorcy uzyskane z lookupu aliasu.

KLIK jest "trigger-em + książką telefoniczną":
1. Decyduje **kiedy** (cron Beat sprawdza `next_run_at`)
2. Wskazuje **komu** (lookup aliasu)
3. Przekazuje to bankowi nadawcy webhookiem

Bank nadawcy:
1. Sprawdza lokalny mandate (czy klient nie odwołał, czy są środki)
2. Wykonuje przelew RTP
3. Odpowiada KLIK z wynikiem

### Co księguje KLIK

| Etap | Action w ledgerze KLIK |
|---|---|
| `POST /recurring/create` | **Brak** entries (sam mandate to nie transfer) |
| Execution success | **1 entry** typu `P2P_LOOKUP_FEE` (bank nadawcy → KLIK) — taki sam jak każdy P2P lookup, agregowany w P4 daily accrual |
| Execution failure | **Brak** entries (bank nie zapłacił za lookup zakończony 4xx — tu też nie naliczamy, bo execution się nie powiódł) |
| Execution skipped (mandate paused/cancelled mid-batch) | **Brak** entries |

Czyli ledger widzi recurring jako serię standardowych P2P lookupów. Mandate sam
nie kosztuje. Realne pieniądze klient → odbiorca lecą poza KLIK.

> **Uwaga o failure billing:** decyzja "fail = nie naliczamy" zachowuje spójność
> z P2P (`GET /aliases/lookup` 4xx też nie naliczamy). Lookup wewnętrzny robiony
> w execution to ten sam mechanizm — jeśli execution failuje *po* lookupie (np.
> bank zwraca INSUFFICIENT_FUNDS), counter już został inkrementowany. To
> akceptowalna nieidealność — bank skorzystał z lookupu, że się płatność
> nie udała to nie sprawa KLIK.

---

## Schedule i kalendarz wykonania

### Cykle (MVP)

| Cycle | Znaczenie | next_run_at logic |
|---|---|---|
| `DAILY` | Codziennie | `last_run_at + 1 day` (ten sam czas) |
| `WEEKLY` | Co 7 dni | `last_run_at + 7 days` |
| `MONTHLY` | Co miesiąc, ten sam dzień miesiąca | `last_run_at + 1 month` (relativedelta — patrz "edge cases") |

Plan post-MVP: `BIWEEKLY`, `QUARTERLY`, `YEARLY`, custom cron.

### Czas wykonania

Globalnie z `.env`:

| Zmienna | Default | Znaczenie |
|---|---|---|
| `RECURRING_EXECUTION_HOUR_UTC` | `8` | Godzina (UTC) o której odpalają się daily executions. Reszta cykli też startuje o tej godzinie. |
| `RECURRING_DISPATCH_INTERVAL_SECONDS` | `300` (5 min) | Interwał z jakim KLIK Beat sprawdza `next_run_at <= now` |

Rzeczywista godzina execution może się różnić od `RECURRING_EXECUTION_HOUR_UTC`
o kilka minut (granularność dispatch interval). To akceptowalne — recurring
ma SLA "tego dnia", nie "tej minuty".

### Edge cases dla MONTHLY

- **31. dzień miesiąca → luty/kwiecień/...**: KLIK używa `relativedelta(months=+1)` z biblioteki `dateutil`. Reguła: jeśli kolejny miesiąc nie ma takiego dnia, użyj **ostatniego dnia miesiąca**. Przykład: mandate utworzony 31 stycznia → kolejny run 28 lutego (lub 29 w roku przestępnym).
- **29 luty (rok przestępny)**: w roku nieprzestępnym → 28 luty.
- **start_date sztywny**: data rozpoczęcia jest punktem odniesienia. Brak "drift" — np. mandate startujący 15-go zawsze stara się trafić w 15-ty.

### Walidacja dat przy create

| Reguła | Reason |
|---|---|
| `start_date >= today` | Nie można backdate'ować mandate |
| `end_date > start_date` (jeśli podane) | Logika |
| `end_date - start_date <= 10 lat` | Sanity check |
| `start_date - today <= 1 rok` | Nie planujemy bardzo daleko w przyszłość (TBD: do uzgodnienia) |

---

## Failure handling i auto-pause

### Klasyfikacja failure

| Reject reason (od banku) | Akcja KLIK |
|---|---|
| `INSUFFICIENT_FUNDS` | Execution → FAILED, `failed_runs_count++`, mandate kontynuuje (lub auto-pause) |
| `MANDATE_REVOKED_LOCALLY` | Execution → FAILED, mandate → **CANCELLED** od razu (klient odwołał w banku, nie poinformował KLIK) |
| `ACCOUNT_CLOSED` | Execution → FAILED, mandate → **CANCELLED** (konto klienta zamknięte) |
| `AML_BLOCK` | Execution → FAILED, mandate → **PAUSED**, alert dla operatora KLIK i banku |
| `RECIPIENT_ALIAS_GONE` | (KLIK-internal) Execution → FAILED, `failed_runs_count++`, mandate kontynuuje. Alias odbiorcy został usunięty z KLIK między runami. |
| `OTHER` | Execution → FAILED, `failed_runs_count++` |

### Auto-pause threshold

`RECURRING_AUTO_PAUSE_FAILURE_THRESHOLD=3` (env). Po 3 FAILED executions **z rzędu**
(bez SUCCESS pomiędzy) mandate przechodzi `ACTIVE → PAUSED`. KLIK kolejkuje
notyfikację `/recurring/auto-paused` do banku.

Counter `failed_runs_count` resetuje się przy każdym SUCCESS.

### Resume po pauzie

Klient w aplikacji bankowej klika "Wznów". Bank wywołuje `POST /recurring/{id}/resume`.
KLIK weryfikuje status PAUSED, przeliczy `next_run_at` (zwykle = nadchodzący czas
zgodnie z cyklem, NIE catch-up missed runs) i zmienia `PAUSED → ACTIVE`.

> **Brak catch-up:** missed runs podczas PAUSED są **stracone** (nie wykonujemy
> ich retroaktywnie). Klient świadomie zatrzymał — nie jest naszym zadaniem
> zgadywać czy chce nadrobić. Można to zmienić w post-MVP jeśli będzie potrzeba.

### Network failure (KLIK → bank)

Jeśli KLIK nie dosięgnął banku w `/recurring/execute`:
- Retry exponential backoff: 5s, 30s, 2min (3 próby)
- Po 3 failach: execution → FAILED z `failure_reason="NETWORK_TIMEOUT"`, jak każdy inny fail
- Mandate kontynuuje (lub auto-pause przy threshold)

---

## Error codes reference

Format spójny z C2B/P2P/Cheques:

```json
{
    "error": {
        "code": "404_RECURRING_NOT_FOUND",
        "message": "Zlecenie stałe nie istnieje.",
        "recurring_transfer_id": "uuid-if-applicable",
        "timestamp": "2026-05-03T14:00:00Z"
    }
}
```

### Tabela błędów

| Code | HTTP | Kategoria | Kiedy występuje |
|---|---|---|---|
| `400_BAD_REQUEST` | 400 | Walidacja | Malformed JSON |
| `400_INVALID_AMOUNT` | 400 | Walidacja | Kwota ≤ 0 lub > limit banku |
| `400_INVALID_CYCLE` | 400 | Walidacja | `cycle` nie w `[DAILY, WEEKLY, MONTHLY]` |
| `400_INVALID_DATE_RANGE` | 400 | Walidacja | `start_date < today`, `end_date <= start_date`, lub przekroczone limity |
| `400_INVALID_PHONE_FORMAT` | 400 | Walidacja | `recipient_phone` nie w E.164 |
| `401_UNAUTHORIZED` | 401 | Auth | Brak/zły API key |
| `403_BANK_INACTIVE` | 403 | Auth | Bank zablokowany |
| `403_RECURRING_NOT_ENABLED` | 403 | Auth | `bank.recurring_enabled=False` |
| `403_P2P_NOT_ENABLED` | 403 | Auth | `bank.p2p_enabled=False` (recurring używa lookupu P2P) |
| `403_INSUFFICIENT_PERMISSIONS` | 403 | Auth | Bank próbuje operować na cudzym mandate |
| `404_RECURRING_NOT_FOUND` | 404 | Biznesowy | Brak mandate o podanym `id` |
| `404_RECIPIENT_ALIAS_NOT_FOUND` | 404 | Biznesowy | `recipient_phone` nie zarejestrowany w KLIK przy create |
| `409_RECURRING_NOT_ACTIVE` | 409 | Biznesowy | Operacja (pause/cancel) na mandate w stanie nie-ACTIVE |
| `409_RECURRING_NOT_PAUSED` | 409 | Biznesowy | Operacja `resume` na mandate nie w stanie PAUSED |
| `409_RECURRING_TERMINATED` | 409 | Biznesowy | Operacja na mandate w stanie CANCELLED/COMPLETED |
| `409_IDEMPOTENCY_CONFLICT` | 409 | Walidacja | Ten sam `Idempotency-Key` z innym payloadem |
| `422_ZONE_MISMATCH` | 422 | Biznesowy | Strefa banku ≠ strefa aliasu odbiorcy (cross-zone) |
| `422_CURRENCY_MISMATCH` | 422 | Walidacja | Waluta w request ≠ waluta strefy |
| `500_INTERNAL_ERROR` | 500 | System | Nieoczekiwany błąd |
| `503_DB_UNAVAILABLE` | 503 | System | Postgres nie odpowiada |

### Konwencje retry

Zgodne z resztą systemu. `Idempotency-Key` wymagany dla `/create`, `/cancel`, `/pause`, `/resume`.

---

## API reference

**Bazowy URL:** `https://api.klik.example.com/api/v1`

### Wspólne nagłówki

```
X-KLIK-Bank-Api-Key: <klucz_banku>
Content-Type: application/json
Idempotency-Key: <uuid-v4>           (dla operacji mutujących)
```

> **Tylko bank ma dostęp do API recurring.** Agenci nie operują na zleceniach
> stałych — to kontrakt klient ↔ bank. Próba użycia `X-KLIK-Agent-Api-Key`
> zwraca 401.

---

### `POST /recurring/create`

**Kto wywołuje:** Bank nadawcy **PO** uzyskaniu PIN-a od klienta i lokalnym
zarejestrowaniu mandate.

**Request body:**
```json
{
    "payer_user_id": "bank-internal-client-id-12345",
    "recipient_phone": "+48501234567",
    "amount": "50.00",
    "currency": "PLN",
    "zone": "PL",
    "cycle": "MONTHLY",
    "start_date": "2026-06-01",
    "end_date": "2027-06-01",
    "mandate_signed_at": "2026-05-03T14:00:00Z"
}
```

`end_date` opcjonalne (open-ended mandate). `mandate_signed_at` to pole audytowe —
KLIK loguje, ale ufa banku.

**Response 201:**
```json
{
    "recurring_transfer_id": "550e8400-e29b-41d4-a716-446655440000",
    "status": "ACTIVE",
    "next_run_at": "2026-06-01T08:00:00Z",
    "created_at": "2026-05-03T14:00:00Z"
}
```

**Uwagi:**
- KLIK robi **walidacyjny lookup** aliasu odbiorcy synchronicznie przy create. Jeśli alias nie istnieje → `404_RECIPIENT_ALIAS_NOT_FOUND`. Ten lookup **nie jest naliczany** (jest częścią mandate setup, nie execution).
- Strefa banku, strefa aliasu odbiorcy, `zone` w request — wszystko musi być **identyczne**. Cross-zone niedozwolone.
- `next_run_at` jest kalkulowane z `start_date + RECURRING_EXECUTION_HOUR_UTC`.
- Bank musi mieć `recurring_enabled=True` ORAZ `p2p_enabled=True`.

**Możliwe błędy:** `400_*`, `401`, `403_BANK_INACTIVE`, `403_RECURRING_NOT_ENABLED`, `403_P2P_NOT_ENABLED`, `404_RECIPIENT_ALIAS_NOT_FOUND`, `422_ZONE_MISMATCH`, `422_CURRENCY_MISMATCH`

---

### `GET /recurring/{recurring_transfer_id}`

**Kto wywołuje:** Bank nadawcy mandate (tylko swoich).

**Response 200:**
```json
{
    "recurring_transfer_id": "550e8400-...",
    "status": "ACTIVE",
    "payer_user_id": "...",
    "recipient_phone": "+48501234567",
    "amount": "50.00",
    "currency": "PLN",
    "zone": "PL",
    "cycle": "MONTHLY",
    "start_date": "2026-06-01",
    "end_date": "2027-06-01",
    "next_run_at": "2026-06-01T08:00:00Z",
    "last_run_at": null,
    "failed_runs_count": 0,
    "executions_summary": {
        "scheduled": 12,
        "succeeded": 0,
        "failed": 0
    },
    "created_at": "2026-05-03T14:00:00Z"
}
```

`executions_summary.scheduled` to **planowana** liczba runów do `end_date`
(orientacyjna — może być 0 dla open-ended).

**Możliwe błędy:** `401`, `404_RECURRING_NOT_FOUND`

---

### `GET /recurring?payer_user_id={id}`

**Kto wywołuje:** Bank nadawcy — listing mandatów klienta.

**Query params:**
- `payer_user_id` (required)
- `status` (optional: `ACTIVE`, `PAUSED`, `CANCELLED`, `COMPLETED`, `ALL` — default `ACTIVE`)

**Response 200:**
```json
{
    "items": [
        {
            "recurring_transfer_id": "550e8400-...",
            "status": "ACTIVE",
            "recipient_phone": "+48501234567",
            "amount": "50.00",
            "currency": "PLN",
            "cycle": "MONTHLY",
            "next_run_at": "2026-06-01T08:00:00Z"
        }
    ],
    "count": 1
}
```

Bank widzi tylko swoich klientów (filtrowanie po `payer_bank_id == request.user.id` z auth).

**Możliwe błędy:** `401`, `403_BANK_INACTIVE`

---

### `POST /recurring/{recurring_transfer_id}/pause`

**Kto wywołuje:** Bank nadawcy gdy klient klika "Wstrzymaj".

**Request body:** `{}` (pusty obiekt — przyszłościowo ewentualnie `paused_until`).

**Response 200:**
```json
{
    "recurring_transfer_id": "550e8400-...",
    "status": "PAUSED",
    "paused_at": "2026-05-15T10:00:00Z"
}
```

**Uwagi:**
- Tylko mandate ACTIVE może być pauzowany. PAUSED → `409_RECURRING_NOT_ACTIVE`.
- Pending executions w stanie SCHEDULED dla tego mandate przechodzą `SCHEDULED → SKIPPED` przy najbliższym dispatch (worker sprawdza status mandate przed execution).
- **Brak catch-up po resume** — runs przegapione w czasie pauzy nie są wykonane retroaktywnie.

**Możliwe błędy:** `401`, `403_INSUFFICIENT_PERMISSIONS`, `404_RECURRING_NOT_FOUND`, `409_RECURRING_NOT_ACTIVE`

---

### `POST /recurring/{recurring_transfer_id}/resume`

**Kto wywołuje:** Bank nadawcy gdy klient wznawia.

**Response 200:**
```json
{
    "recurring_transfer_id": "550e8400-...",
    "status": "ACTIVE",
    "next_run_at": "2026-07-01T08:00:00Z",
    "resumed_at": "2026-06-15T10:00:00Z"
}
```

**Uwagi:**
- `next_run_at` jest przeliczany — wskazuje na pierwszy nadchodzący slot zgodnie z cyklem (nie na pierwszy missed slot).
- Resume resetuje `failed_runs_count` do 0 (świeży start).

**Możliwe błędy:** `401`, `404_RECURRING_NOT_FOUND`, `409_RECURRING_NOT_PAUSED`

---

### `POST /recurring/{recurring_transfer_id}/cancel`

**Kto wywołuje:** Bank nadawcy gdy klient kasuje na stałe.

**Response 200:**
```json
{
    "recurring_transfer_id": "550e8400-...",
    "status": "CANCELLED",
    "cancelled_at": "2026-06-15T10:00:00Z"
}
```

**Uwagi:**
- Działa na ACTIVE i PAUSED. CANCELLED/COMPLETED → `409_RECURRING_TERMINATED`.
- Stan terminalny — brak resume z CANCELLED (klient musi utworzyć nowy mandate).
- Pending executions SCHEDULED dla tego mandate przechodzą SKIPPED.

**Możliwe błędy:** `401`, `403_INSUFFICIENT_PERMISSIONS`, `404_RECURRING_NOT_FOUND`, `409_RECURRING_TERMINATED`

---

### `GET /recurring/{recurring_transfer_id}/executions`

**Kto wywołuje:** Bank nadawcy — historia runów.

**Query params:**
- `limit` (default 20, max 100)
- `before` (datetime, paginacja)

**Response 200:**
```json
{
    "items": [
        {
            "execution_id": "770e8400-...",
            "scheduled_for": "2026-06-01T08:00:00Z",
            "executed_at": "2026-06-01T08:02:13Z",
            "status": "SUCCESS",
            "rtp_reference": "ELIXIR-EXP-12345"
        },
        {
            "execution_id": "880e8400-...",
            "scheduled_for": "2026-05-01T08:00:00Z",
            "executed_at": "2026-05-01T08:01:55Z",
            "status": "FAILED",
            "failure_reason": "INSUFFICIENT_FUNDS"
        }
    ],
    "count": 2
}
```

**Możliwe błędy:** `401`, `404_RECURRING_NOT_FOUND`

---

## Webhooki wymagane od banków

Bank wystawcy mandate wystawia **trzy endpointy**. URL z `Bank.recurring_webhook_url`
(fallback: `Bank.webhook_url` z suffixem `/recurring`).

### `POST {bank_recurring_webhook_url}/execute`

**Kto wywołuje:** KLIK (Celery worker) gdy `next_run_at` mandate-a został osiągnięty.

**Payload od KLIK:**
```json
{
    "recurring_transfer_id": "550e8400-...",
    "execution_id": "770e8400-...",
    "payer_user_id": "bank-internal-client-id-12345",
    "amount": "50.00",
    "currency": "PLN",
    "scheduled_for": "2026-06-01T08:00:00Z",
    "mandate_signed_at": "2026-05-03T14:00:00Z",
    "recipient": {
        "phone": "+48501234567",
        "bank_id": "bank-uuid-receiving",
        "bank_code": "BANK_A",
        "account_identifier": {
            "type": "iban",
            "value": "PL61109010140000071219812874"
        }
    }
}
```

**Oczekiwana odpowiedź — sukces:**
```json
HTTP 200 OK
{
    "status": "EXECUTED",
    "rtp_reference": "ELIXIR-EXP-12345",
    "executed_at": "2026-06-01T08:02:13Z"
}
```

**Oczekiwana odpowiedź — odrzucenie:**
```json
HTTP 200 OK
{
    "status": "REJECTED",
    "reject_reason": "INSUFFICIENT_FUNDS"
}
```

**Dozwolone `reject_reason`:** `INSUFFICIENT_FUNDS`, `MANDATE_REVOKED_LOCALLY`, `ACCOUNT_CLOSED`, `AML_BLOCK`, `OTHER`

**Uwagi:**
- Bank MUSI wykonać przelew RTP (Elixir Express / Faster Payments / SEPA Instant / FedNow RTP) **przed** odpowiedzeniem `EXECUTED`. KLIK ufa.
- Timeout KLIK: 30s. Po timeoucie retry exponential backoff (5s, 30s, 2min — łącznie 3 próby), potem execution → FAILED z `failure_reason="NETWORK_TIMEOUT"`.
- Idempotency: KLIK nie wyśle dwa razy webhook-a dla tego samego `execution_id` (chyba że pierwszy to network timeout — wtedy retry z tym samym `execution_id`). Bank powinien sam wykryć duplikat (np. po `execution_id`) i zwrócić ten sam wynik.

### `POST {bank_recurring_webhook_url}/auto-paused`

**Kto wywołuje:** KLIK gdy mandate jest auto-pauzowany po 3 failach z rzędu.

**Payload od KLIK:**
```json
{
    "recurring_transfer_id": "550e8400-...",
    "payer_user_id": "...",
    "paused_at": "2026-08-01T08:05:00Z",
    "failed_runs_count": 3,
    "last_failure_reason": "INSUFFICIENT_FUNDS"
}
```

**Oczekiwana odpowiedź:**
```json
HTTP 200 OK
{"received": true}
```

**Uwagi:**
- Bank powinien powiadomić klienta (push: "Twoje zlecenie 50 PLN do +48... zostało wstrzymane po 3 nieudanych próbach. Wznów w aplikacji.").
- Retry policy: 5s/30s/2min, po 3 failach alert dla operatora KLIK.

### `POST {bank_recurring_webhook_url}/cancelled`

**Kto wywołuje:** KLIK gdy mandate jest auto-cancelowany (np. `MANDATE_REVOKED_LOCALLY`,
`ACCOUNT_CLOSED`).

**Payload od KLIK:**
```json
{
    "recurring_transfer_id": "550e8400-...",
    "payer_user_id": "...",
    "cancelled_at": "2026-08-01T08:05:00Z",
    "reason": "MANDATE_REVOKED_LOCALLY"
}
```

**Możliwe wartości `reason`:** `MANDATE_REVOKED_LOCALLY`, `ACCOUNT_CLOSED`, `END_DATE_REACHED`

> Reason `END_DATE_REACHED` używamy gdy mandate przechodzi `ACTIVE → COMPLETED`
> po `end_date` — to nie jest "cancel" w sensie biznesowym, ale używamy
> tego samego endpointu dla uproszczenia. Bank rozpoznaje po `reason` że to
> naturalne zakończenie.

**Oczekiwana odpowiedź:**
```json
HTTP 200 OK
{"received": true}
```

---

## Pricing

KLIK pobiera **standardowy `p2p_lookup_fee`** za każdą udaną execution (lookup
aliasu jest częścią execution). Bank nadawcy płaci.

| Operacja | Płatne? |
|---|---|
| `POST /recurring/create` | Nie (lookup walidacyjny też nie naliczany) |
| Execution z lookupem (mandate run) | **Tak** — 1× `p2p_lookup_fee` od bank nadawcy |
| Execution failed (np. INSUFFICIENT_FUNDS od banku) | **Tak** (lookup wykonany, counter inkrementowany) |
| Execution skipped (mandate paused mid-batch) | Nie (lookup nie wykonany) |
| `pause` / `resume` / `cancel` | Nie |
| `GET /recurring/...` | Nie |

Counter w Redisie pod tym samym kluczem co P2P (`aliases:lookups:{bank_id}:YYYYMMDD`).
Daily P4 accrual agreguje recurring + zwykłe P2P lookupy razem.

> **Plan post-MVP:** osobna stawka `recurring_run_fee` (per-execution surcharge,
> niezależnie od lookupu) jeśli operator KLIK chce zarabiać na samym fakcie
> orkiestracji. Nie w MVP.

---

## Onboarding i autentykacja

### Proces onboardingu (skrót)

1. Bank kontaktuje operatora KLIK
2. Operator w Django Admin:
   - Włącza moduł: `bank.recurring_enabled = True` (wymaga też `bank.p2p_enabled = True`)
   - Ustawia `bank.recurring_webhook_url` (lub fallback do `webhook_url + /recurring`)
3. Bank konfiguruje endpointy `/execute`, `/auto-paused`, `/cancelled` u siebie
4. Bank wykonuje testowy `POST /recurring/create` (np. mandate na siebie samego, dla testu)
5. Czekanie na pierwsze execution → weryfikacja webhooka

### Autentykacja

Wszystkie endpointy używają `X-KLIK-Bank-Api-Key`. Agenci/inne typy klientów odrzucane.

### Idempotency

`/create`, `/cancel`, `/pause`, `/resume` przyjmują `Idempotency-Key`. Reguły jak w C2B/P2P.

---

## Wersjonowanie API

Wspólne z C2B/P2P/Cheques przez `/api/v1/`.

---

## Kontakt

- **Dokumentacja techniczna:** `docs/recurring/`
- **Diagramy:** `docs/recurring/diagrams/` (Mermaid)
- **Zgłoszenia integracyjne:** przez operatora KLIK
