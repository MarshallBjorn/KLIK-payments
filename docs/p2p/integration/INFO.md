# KLIK P2P — Dokumentacja integracyjna

Dokument dla zespołów bankowych integrujących się z modułem **Telefony (P2P)**
systemu KLIK. Zawiera słownik domenowy, referencję błędów oraz specyfikację API.

**Wersja:** 1.0
**Data:** 2026-04-26
**Status:** Kompletny — czeka na sprawdzenie

> **Powiązane dokumenty:**
> - [C2B INFO.md](../../c2b/integration/INFO.md) — moduł Kody (płatności kodem)
> - [C2B WORKFLOW.md](../../c2b/diagrams/WORKFLOW.md) — diagramy sekwencji C2B (onboarding A0)
> - [P2P WORKFLOW.md](../diagrams/WORKFLOW.md) — diagramy sekwencji P2P (P0-P4)
> - [BPMN](../bpmn/) — diagramy procesów biznesowych

---

## Spis treści

1. [Słownik domenowy](#słownik-domenowy)
2. [Pricing model](#pricing-model)
3. [Account identifier](#account-identifier)
4. [Error codes reference](#error-codes-reference)
5. [API reference](#api-reference)
6. [Onboarding i autentykacja](#onboarding-i-autentykacja)

---

## Słownik domenowy

Terminologia specyficzna dla modułu P2P. Pojęcia ogólne (Bank, Zone, RTGS,
SettlementSession itp.) w [C2B INFO.md](../../c2b/integration/INFO.md#słownik-domenowy).

### Obiekty domenowe

| Termin | Definicja | Gdzie przechowywany |
|---|---|---|
| **Alias** | Mapowanie numeru telefonu na parę [bank_id, account_identifier]. Klucz lookup-u dla przelewów P2P. | Postgres (trwały, bez TTL) |
| **Lookup counter** | Licznik wywołań `GET /aliases/lookup/{phone}` per bank per dzień. Podstawa do naliczania prowizji P2P. | Redis (`bank_lookups:{bank_id}:{date}`, TTL 7 dni) |
| **P2P LedgerEntry** | Zobowiązanie banku wobec KLIK z tytułu prowizji P2P. Generowane raz dziennie cron jobem (P4). | Postgres (wspólna tabela z C2B fees) |

### Pojęcia procesowe

| Termin | Definicja |
|---|---|
| **Lookup-based fee** | Model rozliczania P2P. KLIK pobiera stałą stawkę (`p2p_lookup_fee`) za **każdy udany lookup**. Lookup zakończony 404 (alias nie istnieje) **nie jest** naliczany. |
| **Daily fee accrual** | Cron job (Celery Beat, 23:55 UTC) agregujący lookup countery w `LedgerEntry`. Po agregacji entries trafiają do najbliższej sesji rozliczeniowej. |
| **P2P-enabled bank** | Bank z flagą `p2p_enabled=True` na rekordzie. Bank może być aktywny w C2B bez P2P (i odwrotnie) — moduły są niezależnie aktywowane. |
| **P2P routing** | Zwrot przez KLIK pary [bank_id, account_identifier] do banku-pytającego. KLIK **nie uczestniczy** w fizycznym transferze pieniędzy — bank przesyła środki przez Elixir Express / Faster Payments / SEPA Instant / FedNow RTP poza KLIK. |

### Stany (skrót — pełne diagramy w [WORKFLOW.md](../diagrams/WORKFLOW.md#stany-alias))

**Alias:** `REGISTERED` → `deleted` (brak stanów pośrednich, brak soft-delete w v1.0)

---

## Pricing model

KLIK pobiera prowizję od banków za udane lookupy. Model **per zapytanie**,
agregowany nocą.

### Stawka

- Stawka **per bank**, ustalana przy onboardingu (pole `Bank.p2p_lookup_fee`)
- Wyrażona w walucie strefy banku (PLN, EUR, GBP, USD)
- Precyzja: 4 miejsca po przecinku (np. `0.0500` PLN za lookup)
- Domyślna wartość: `0.0000` (bank musi mieć stawkę ustawioną explicit przez operatora KLIK)

### Co jest płatne, co nie

| Operacja | Płatne? |
|---|---|
| `POST /aliases/register` | Nie |
| `GET /aliases/lookup/{phone}` zakończone 200 | **Tak** |
| `GET /aliases/lookup/{phone}` zakończone 404 | Nie (bank nie skorzystał z usługi) |
| `GET /aliases/lookup/{phone}` zakończone 401/403/422 | Nie (błąd kliencki) |
| `DELETE /aliases/{phone}` | Nie |

### Rozliczanie

1. Każde udane wywołanie lookup inkrementuje counter w Redisie
2. Cron job o 23:55 UTC agreguje countery i tworzy `LedgerEntry` per bank
3. Te entries trafiają do najbliższej sesji nettingowej (wspólnej z C2B fees)
4. Settlement przez RTGS dispatcher (SORBNET3/TARGET2/CHAPS/FedNow)
5. Bank widzi w raporcie sesji łączną kwotę zobowiązań wobec KLIK (C2B + P2P)

Szczegóły w [WORKFLOW P4](../diagrams/WORKFLOW.md#p4--naliczanie-prowizji-daily-fee-accrual).

---

## Account identifier

Identyfikator konta bankowego — pole `account_identifier` w endpointach P2P.
Format zależy od strefy.

### Schemat per strefa

#### PL / EU / UK — IBAN

```json
{
    "type": "iban",
    "value": "PL61109010140000071219812874"
}
```

**Walidacja:**
- Format: 2 litery kraju + 2 cyfry kontrolne + 11-30 znaków alfanumerycznych
- Spacje są dopuszczalne ale ignorowane (`PL61 1090 1014 0000 0712 1981 2874` = `PL61109010140000071219812874`)
- Dla strefy **PL** musi zaczynać się na `PL`
- Dla strefy **UK** musi zaczynać się na `GB`
- Dla strefy **EU** akceptowane są wszystkie krajowe prefixy europejskie (DE, FR, IT, ES itd.)

#### US — routing number + account number

```json
{
    "type": "us_routing",
    "routing_number": "021000021",
    "account_number": "1234567890"
}
```

**Walidacja:**
- `routing_number`: dokładnie 9 cyfr (ABA routing transit number)
- `account_number`: 4-17 cyfr

### Zgodność strefa ↔ typ

| Strefa | Dozwolony `type` |
|---|---|
| PL | `iban` |
| EU | `iban` |
| UK | `iban` |
| US | `us_routing` |

Niezgodność powoduje błąd `422_ZONE_MISMATCH`.

---

## Error codes reference

Endpointy P2P używają tego samego formatu błędów co C2B:

```json
{
    "error": {
        "code": "404_ALIAS_NOT_FOUND",
        "message": "Numer telefonu nie jest zarejestrowany w KLIK.",
        "timestamp": "2026-04-26T14:00:00Z"
    }
}
```

### Tabela błędów P2P

| Code | HTTP | Kategoria | Kiedy występuje | Działanie banku |
|---|---|---|---|---|
| `400_BAD_REQUEST` | 400 | Walidacja | Malformed JSON, brakujące wymagane pola | Popraw payload |
| `400_INVALID_PHONE_FORMAT` | 400 | Walidacja | Numer telefonu nie w formacie E.164 | Popraw format (`+48...`) |
| `400_INVALID_ACCOUNT_IDENTIFIER` | 400 | Walidacja | account_identifier nie pasuje do schematu strefy | Popraw payload |
| `401_UNAUTHORIZED` | 401 | Auth | Brak lub niepoprawny `X-KLIK-Bank-Api-Key` | Sprawdź credentials |
| `403_BANK_INACTIVE` | 403 | Auth | Bank zablokowany (`active=False`) | Skontaktuj się z operatorem KLIK |
| `403_P2P_NOT_ENABLED` | 403 | Auth | Bank ma aktywny C2B ale nie P2P (`p2p_enabled=False`) | Skontaktuj się z operatorem KLIK aby aktywować P2P |
| `403_INSUFFICIENT_PERMISSIONS` | 403 | Auth | Bank próbuje usunąć alias innego banku | Operacja niedozwolona |
| `404_ALIAS_NOT_FOUND` | 404 | Biznesowy | Numer telefonu nie zarejestrowany w KLIK | Bank może zaproponować klasyczny przelew na IBAN |
| `409_ALIAS_ALREADY_EXISTS` | 409 | Biznesowy | Numer telefonu już przypisany | Wcześniejszy bank musi wyrejestrować alias |
| `422_ZONE_MISMATCH` | 422 | Biznesowy | Strefa banku ≠ strefa aliasu lub account_identifier niezgodny ze strefą | Sprawdź strefę i format account_identifier |
| `500_INTERNAL_ERROR` | 500 | System | Nieoczekiwany błąd KLIK | Retry, zgłoś jeśli się powtarza |
| `503_DB_UNAVAILABLE` | 503 | System | Postgres nie odpowiada | Retry z backoff |
| `503_REDIS_UNAVAILABLE` | 503 | System | Redis nie odpowiada (krytyczne dla counter'ów) | Retry, KLIK przywróci dostępność |

---

## API reference

**Bazowy URL (MVP / development):** `https://api.klik.example.com/api/v1`

Jeden deployment obsługuje wszystkie strefy. Strefa identyfikowana po polu
`zone` w payloadzie i strefie bankowej przypisanej do API key.

### Wspólne nagłówki

```
X-KLIK-Bank-Api-Key: <klucz_wydany_przy_onboardingu>
Content-Type: application/json
Idempotency-Key: <uuid-v4>  (dla żądań mutujących: register, delete)
```

> **Uwaga o autentykacji:** P2P używa **wyłącznie** banku jako uwierzytelnionego klienta.
> Agenci (z C2B) nie mają dostępu do API P2P. Header autentykacji to **`X-KLIK-Bank-Api-Key`**,
> nie `X-KLIK-Agent-Api-Key` (różne moduły, różne typy klientów).

---

### `POST /aliases/register`

**Kto wywołuje:** Bank klienta po włączeniu przez klienta funkcji "Przelew na telefon"

**Request body:**

Dla strefy PL:
```json
{
    "phone": "+48501234567",
    "account_identifier": {
        "type": "iban",
        "value": "PL61109010140000071219812874"
    },
    "zone": "PL"
}
```

Dla strefy US:
```json
{
    "phone": "+15551234567",
    "account_identifier": {
        "type": "us_routing",
        "routing_number": "021000021",
        "account_number": "1234567890"
    },
    "zone": "US"
}
```

**Response 201:**
```json
{
    "alias_id": "550e8400-e29b-41d4-a716-446655440000",
    "phone": "+48501234567",
    "registered_at": "2026-04-26T14:00:00Z"
}
```

**Uwagi:**
- `phone` w formacie E.164 (z prefixem kraju, bez spacji)
- `zone` musi być zgodna ze strefą banku wywołującego oraz prefixem numeru telefonu
- Jeden numer może być zarejestrowany **tylko raz** w danej strefie (cross-zone OK — np. ten sam numer może mieć alias PL i UK, jeśli klient ma konta w obu strefach)
- Bank musi mieć `p2p_enabled=True`

**Możliwe błędy:** `400`, `401`, `403_BANK_INACTIVE`, `403_P2P_NOT_ENABLED`, `409_ALIAS_ALREADY_EXISTS`, `422_ZONE_MISMATCH`, `500`, `503`

---

### `GET /aliases/lookup/{phone}`

**Kto wywołuje:** Bank nadawcy przed wykonaniem przelewu P2P, żeby wiedzieć gdzie wysłać

**URL parameter:** `phone` — numer w formacie E.164 (URL-encoded `+` jako `%2B`, np. `%2B48501234567`)

**Response 200:**
```json
{
    "phone": "+48501234567",
    "bank_id": "bank-uuid-receiving",
    "bank_code": "BANK_A",
    "account_identifier": {
        "type": "iban",
        "value": "PL61109010140000071219812874"
    }
}
```

**Uwagi:**
- **Każdy udany lookup (200) jest płatny** — KLIK inkrementuje counter dla banku-pytającego
- Lookup zakończony 404 nie jest płatny
- Bank nadawcy używa zwróconego `account_identifier` do wysłania przelewu przez RTP (Elixir Express / Faster Payments / SEPA Instant / FedNow RTP) — **poza KLIK**
- KLIK nie uczestniczy w transferze pieniędzy
- Możliwa retencja lookupów dla audytu i fraud detection (szczegóły u operatora KLIK)

**Możliwe błędy:** `401`, `403_BANK_INACTIVE`, `403_P2P_NOT_ENABLED`, `404_ALIAS_NOT_FOUND`, `422_ZONE_MISMATCH`

---

### `DELETE /aliases/{phone}`

**Kto wywołuje:** Bank klienta przy wyłączeniu funkcji lub zamknięciu konta

**URL parameter:** `phone` — numer w formacie E.164 (URL-encoded)

**Response 204:** (No Content)

**Uwagi:**
- Bank może usuwać **tylko aliasy swoich klientów** (alias.bank_id == bank wywołujący)
- Próba usunięcia cudzego aliasu = `403_INSUFFICIENT_PERMISSIONS`
- Alias jest fizycznie usuwany z bazy (no soft-delete w v1.0)
- Po DELETE klient może ponownie zarejestrować ten sam numer (w tym samym lub innym banku)

**Możliwe błędy:** `401`, `403_BANK_INACTIVE`, `403_P2P_NOT_ENABLED`, `403_INSUFFICIENT_PERMISSIONS`, `404_ALIAS_NOT_FOUND`

---

## Onboarding i autentykacja

### Proces onboardingu P2P (skrót — pełny flow w [WORKFLOW P0](../diagrams/WORKFLOW.md#p0--onboarding-banku-w-p2p))

1. Bank kontaktuje operatora KLIK (poza systemem)
2. Operator ustala stawkę `p2p_lookup_fee` przy umowie
3. Operator w Django Admin:
   - Aktywuje moduł: `bank.p2p_enabled = True`
   - Ustawia stawkę: `bank.p2p_lookup_fee = 0.05` (przykład PLN)
4. Bank konfiguruje URL endpointów `/aliases/*` u siebie
5. Bank wykonuje testowe wywołanie `POST /aliases/register` — sukces oznacza gotowość

> **Banki już zintegrowane z C2B** używają tego samego API key — operator po
> prostu odznacza/zaznacza flagę `p2p_enabled` i dorzuca stawkę.

### Autentykacja

- Wszystkie endpointy P2P używają `X-KLIK-Bank-Api-Key`
- Agenci (z C2B) **nie mają dostępu** do API P2P — ich `X-KLIK-Agent-Api-Key` zwróci 401 na `/aliases/*`

### Idempotency

Endpointy mutujące (`POST /aliases/register`, `DELETE /aliases/{phone}`)
przyjmują nagłówek:

```
Idempotency-Key: <uuid-v4>
```

Ten sam klucz + ten sam payload = zwrócenie oryginalnego wyniku.
Ten sam klucz + inny payload = `409_IDEMPOTENCY_CONFLICT`.

KLIK przechowuje idempotency_key przez 24h od pierwszego wywołania.

---

## Wersjonowanie API

P2P API jest wersjonowane wspólnie z C2B przez prefix URL (`/api/v1/`).
Lista zmian w `CHANGELOG.md` w repo KLIK.

---

## Kontakt

- **Dokumentacja techniczna:** `docs/p2p/` w repo KLIK
- **Diagramy:** `docs/p2p/diagrams/` (Mermaid), `docs/p2p/bpmn/` (BPMN)
- **Zgłoszenia integracyjne:** przez operatora KLIK (poza systemem w MVP)
