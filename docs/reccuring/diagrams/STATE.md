# KLIK Recurring — Diagramy stanów i domeny

Diagramy stanów obiektów Recurring + zaktualizowany ERD systemu z nowymi encjami.

## Spis treści

### Diagramy stanowe (B)
- [B-R1 — Stany RecurringTransfer (Mandate)](#b-r1--stany-recurringtransfer-mandate)
- [B-R2 — Stany RecurringExecution (Run)](#b-r2--stany-recurringexecution-run)

### Diagramy domenowe (C)
- [C-R1 — ERD update (Recurring w modelu)](#c-r1--erd-update-recurring-w-modelu)

### Dokumenty referencyjne
- [Uwagi do modelu](#uwagi-do-modelu)
- [Wpływ na istniejące encje](#wpływ-na-istniejące-encje)

## Powiązana dokumentacja
- [WORKFLOW.md](./WORKFLOW.md) — diagramy sekwencji (R0-R6)
- [../integration/INFO.md](../integration/INFO.md) — API reference
- [../../c2b/diagrams/STATE.md](../../c2b/diagrams/STATE.md) — pełny ERD systemu

---

## B-R1 — Stany RecurringTransfer (Mandate)

Mandate żyje od momentu utworzenia do CANCELLED (manualnie) lub COMPLETED
(automatycznie po `end_date`).

```mermaid
---
config:
  theme: dark
---
stateDiagram-v2
    [*] --> ACTIVE: POST /recurring/create<br/>(R1, walidacja + alias lookup OK)

    ACTIVE --> PAUSED: POST /recurring/{id}/pause<br/>(R3, klient w aplikacji bankowej)
    PAUSED --> ACTIVE: POST /recurring/{id}/resume<br/>(R3, reset failed_runs_count=0,<br/>next_run_at = next slot)

    ACTIVE --> PAUSED: failed_runs_count >= 3<br/>(R5, auto-pause<br/>+ webhook /auto-paused)
    ACTIVE --> PAUSED: AML_BLOCK od banku<br/>(R2, single rejection<br/>+ webhook /auto-paused)

    ACTIVE --> CANCELLED: POST /recurring/{id}/cancel<br/>(R4, klient anuluje)
    PAUSED --> CANCELLED: POST /recurring/{id}/cancel<br/>(R4, klient anuluje z pauzy)

    ACTIVE --> CANCELLED: MANDATE_REVOKED_LOCALLY / ACCOUNT_CLOSED<br/>(R2, bank zwraca w execution<br/>+ webhook /cancelled)

    ACTIVE --> COMPLETED: next_run_at > end_date<br/>(R6, naturalne zakończenie<br/>+ webhook /cancelled reason=END_DATE_REACHED)

    CANCELLED --> [*]: Stan terminalny
    COMPLETED --> [*]: Stan terminalny

    note right of ACTIVE: next_run_at trzyma czas najbliższego runu.<br/>failed_runs_count licznik failów z rzędu.
    note right of PAUSED: Cron worker pomija przy dispatch<br/>(SELECT WHERE status=ACTIVE).<br/>Pending Executions SCHEDULED → SKIPPED.
```

**Uwagi do maszyny stanów RecurringTransfer:**

- Brak `PAUSED → COMPLETED` — żeby skończyć naturalnie mandate musi być najpierw resume'owany. Jeśli klient zostawił PAUSED i `end_date` minął, mandate zostaje w PAUSED — operator musi to obsłużyć ręcznie (TBD: ewentualny cron auto-cleanup po N dni w PAUSED, post-MVP).

- `ACTIVE → CANCELLED` jest jedyną drogą do CANCELLED (z PAUSED też, ale przez ten sam endpoint). Brak ścieżki "mandate sam się skasował".

- `failed_runs_count` jest **resetowany** w trzech sytuacjach: SUCCESS execution, resume, lub manualny edit operatora.

---

## B-R2 — Stany RecurringExecution (Run)

Pojedyncza execution ma krótkie życie — od momentu utworzenia (kiedy worker
podejmuje mandate) do terminala (success/failed/skipped). Zwykle <30s całość.

```mermaid
---
config:
  theme: dark
---
stateDiagram-v2
    [*] --> SCHEDULED: Worker tworzy Execution<br/>w momencie dispatch<br/>(R2 ETAP 3)

    SCHEDULED --> EXECUTING: Worker rozpoczyna processing<br/>(zaraz po INSERT,<br/>w ramach jednej tx)

    EXECUTING --> SUCCESS: Bank zwraca {EXECUTED, rtp_reference}<br/>(R2 happy path)

    EXECUTING --> FAILED: Bank zwraca {REJECTED, reject_reason}<br/>lub network timeout po 3 retry<br/>(R2 reject path)

    SCHEDULED --> SKIPPED: Mandate przeszedł do PAUSED/CANCELLED<br/>między utworzeniem Execution a startem<br/>(R3, R4 race)

    SUCCESS --> [*]: Stan terminalny
    FAILED --> [*]: Stan terminalny
    SKIPPED --> [*]: Stan terminalny

    note right of SCHEDULED: Stan przejściowy.<br/>Worker robi INSERT(SCHEDULED) → UPDATE(EXECUTING)<br/>w jednej tx, więc okno SCHEDULED<br/>jest milisekundami.
    note right of EXECUTING: Lookup aliasu + webhook do banku.<br/>Tu trwa execution (typowo 1-5s).
    note right of SUCCESS: rtp_reference od banku zapisany.<br/>Liczy się jako run dla<br/>RecurringTransfer.last_run_at.
    note right of FAILED: failure_reason wypełniony.<br/>Inkrementuje failed_runs_count<br/>w mandate.
    note right of SKIPPED: Brak failure_reason — nie liczy się<br/>jako fail dla auto-pause.
```

**Uwagi:**

- Stan `SCHEDULED` istnieje krótko bo worker tworzy execution i od razu robi UPDATE na EXECUTING. Po awarii workera (kill -9 między INSERT i UPDATE) execution zostaje w SCHEDULED — operator musi to zauważyć (TBD: cron sprzątający stale SCHEDULED po 5 min na status=FAILED z reason=ORPHANED).

- `SCHEDULED → SKIPPED` to scenariusz race: worker A tworzy execution, w międzyczasie request `/pause` zmienia mandate na PAUSED, worker B (lub ten sam) widzi PAUSED i zamiast EXECUTING ustawia SKIPPED. To rzadkie ale możliwe.

- Brak stanu RETRYING — retry jest implementacyjnie wewnątrz EXECUTING (3 próby network), nie jako osobny stan w DB.

---

## C-R1 — ERD update (Recurring w modelu)

```mermaid
---
config:
  theme: dark
---
erDiagram
    Bank ||--o{ RecurringTransfer : "wystawia"
    Bank ||--o{ RecurringTransfer : "odbiorca_via_alias"
    RecurringTransfer ||--o{ RecurringExecution : "ma_runy"
    RecurringTransfer }o--|| Zone : "operuje_w"
    RecurringTransfer }o--|| Alias : "wskazuje_na (snapshot phone)"

    Bank {
        uuid id PK
        boolean recurring_enabled "NEW: czy bank może tworzyć mandate"
        string recurring_webhook_url "NEW: URL dla /execute, /auto-paused, /cancelled"
    }

    RecurringTransfer {
        uuid id PK
        uuid payer_bank_id FK "Bank nadawcy (właściciel mandate)"
        string payer_user_id "internal client id po stronie banku nadawcy"
        string recipient_phone "E.164 — alias odbiorcy"
        decimal amount "stała kwota"
        string currency
        enum zone FK
        enum cycle "DAILY / WEEKLY / MONTHLY"
        date start_date
        date end_date "nullable — open-ended jeśli NULL"
        datetime next_run_at "kalkulowane z cycle + last_run_at"
        datetime last_run_at "nullable"
        uuid last_execution_id FK "nullable"
        enum status "ACTIVE / PAUSED / CANCELLED / COMPLETED"
        int failed_runs_count "licznik failów z rzędu (reset przy SUCCESS lub resume)"
        datetime mandate_signed_at "audit, z payloadu /create"
        string idempotency_key "z POST /recurring/create"
        datetime created_at
        datetime updated_at
        datetime paused_at "nullable"
        datetime cancelled_at "nullable"
    }

    RecurringExecution {
        uuid id PK
        uuid recurring_transfer_id FK
        datetime scheduled_for "kiedy miało być wykonane (= mandate.next_run_at w momencie dispatch)"
        datetime executed_at "nullable, kiedy faktycznie się wydarzyło"
        enum status "SCHEDULED / EXECUTING / SUCCESS / FAILED / SKIPPED"
        string rtp_reference "nullable, ID przelewu RTP od banku"
        string failure_reason "nullable: INSUFFICIENT_FUNDS / MANDATE_REVOKED_LOCALLY / ACCOUNT_CLOSED / AML_BLOCK / RECIPIENT_ALIAS_GONE / NETWORK_TIMEOUT / OTHER"
        json lookup_response_snapshot "nullable: dane aliasu z lookupu w momencie execution (audit)"
        datetime created_at
        datetime updated_at
    }
```

---

## Uwagi do modelu

### 1. Indeksy

Krytyczne dla wydajności:

| Indeks | Pole(a) | Powód |
|---|---|---|
| `recurring_transfer_dispatch_idx` | `(status, next_run_at)` partial WHERE status='ACTIVE' | Cron query co 5 min — najczęstszy access pattern |
| `recurring_transfer_payer_idx` | `(payer_bank_id, payer_user_id, status)` | Listing mandate-ów klienta (`GET /recurring?payer_user_id=`) |
| `recurring_transfer_idempotency_idx` | `(idempotency_key, payer_bank_id)` UNIQUE | Idempotency lookup dla `/create` |
| `recurring_execution_mandate_idx` | `(recurring_transfer_id, scheduled_for DESC)` | Listing executions (`GET /recurring/{id}/executions`) |
| `recurring_execution_status_idx` | `(status)` partial WHERE status='SCHEDULED' | Cleanup orphaned executions |

### 2. Relacja do Alias

Świadomie **nie ma FK** z `RecurringTransfer.recipient_phone` do `Alias.phone`:

- Alias może zostać DELETE'owany niezależnie od mandate-ów (klient odbiorcy zmienia bank, wyłącza P2P)
- FK constraint blokowałby delete aliasu który ma aktywne mandate-y kierowane na niego — to nie nasza sprawa egzekwować przy delete (bank odbiorcy nie wie o mandate w innych bankach)
- Zamiast tego: `RecurringExecution.lookup_response_snapshot` zachowuje co znaleźliśmy w czasie execution, plus `failure_reason='RECIPIENT_ALIAS_GONE'` jeśli zniknął

### 3. cycle jako enum vs cron expression

W MVP enum (`DAILY/WEEKLY/MONTHLY`) bo:
- Łatwiej walidować
- Łatwiej wyświetlać w aplikacji bankowej ("co miesiąc" vs "0 8 1 * *")
- Pokrywa 95% use case'ów

Post-MVP można dodać `cycle_expression` (cron string) jako alternatywne pole z osobną logiką kalkulacji `next_run_at`.

### 4. Czas w UTC

Wszystkie pola datetime trzymane w UTC (Postgres `TIMESTAMPTZ`). Konwersja na czas
lokalny strefy klienta robiona w aplikacji bankowej, nie w KLIK.

### 5. Delete vs soft-delete

Brak soft-delete dla mandate-ów. Stany terminalne (CANCELLED/COMPLETED)
zostają w bazie permanentnie dla audytu. Polityka retencji TBD.

---

## Wpływ na istniejące encje

### Bank (modyfikacja)

Dodajemy 2 pola:

| Pole | Typ | Default | Opis |
|---|---|---|---|
| `recurring_enabled` | bool | `False` | Bank uczestniczy w module Recurring. Wymaga też `p2p_enabled=True`. |
| `recurring_webhook_url` | URL | `''` | URL dla webhooków `/execute`, `/auto-paused`, `/cancelled`. Pusty = fallback do `Bank.webhook_url`. |

Migracja: dodanie pól bez wpływu na istniejące rekordy.

### Brak zmian w innych encjach

`Agent`, `MSCAgreement`, `Merchant`, `Transaction`, `LedgerEntry`,
`SettlementSession`, `SettlementTransfer`, `Alias`, `Cheque` — bez zmian.

Recurring **nie tworzy** żadnych C2B LedgerEntries (transfer pieniędzy odbywa
się przez RTP poza KLIK). Naliczanie prowizji P2P przy execution korzysta
z istniejącego mechanizmu (counter w Redis + P4 daily accrual).

---

## Tabela porównawcza encji per moduł (zaktualizowana)

| Encja | C2B | P2P | Cheques | Recurring |
|---|---|---|---|---|
| **Centralny obiekt** | Code (Redis) | Alias (Postgres) | Cheque (Postgres) | RecurringTransfer (Postgres) |
| **Sub-encja** | Transaction | brak (counter w Redis) | brak (Transaction reused) | RecurringExecution |
| **TTL/lifetime** | 120s | trwały | 1h–72h | do `end_date` (mogą być lata) |
| **Tworzy Transaction** | tak | nie | tak (z `cheque_id`) | nie (przelew RTP poza KLIK) |
| **Tworzy LedgerEntries** | przy /confirm | przy P4 accrual | przy /redeem | przy P4 accrual (lookup fees) |
| **Webhooki banku (KLIK→Bank)** | `/authorize` | brak | `/redeemed`, `/released` | `/execute`, `/auto-paused`, `/cancelled` |
| **Cron tasks** | A5 settlement | P4 accrual | CH3 expire | R-dispatch (co 5min), A5/P4 (wspólne) |
| **Kto trigger-uje?** | Agent (płatność) / Bank (issue) | Bank (lookup) | Bank (issue), Agent (redeem) | **KLIK** (cron) |
