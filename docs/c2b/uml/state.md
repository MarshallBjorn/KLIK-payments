# Diagramy stanowe (B) i domenowe (C)

---

## B2 — Stany Transakcji (+ B1 stany Kodu jako podsekcja)

Cykl życia obiektów Transaction i Code.

**Code** (w Redis, krótki TTL):

```mermaid
---
config:
  theme: dark
---
stateDiagram-v2
    [*] --> ACTIVE: POST /codes/generate<br/>(A1)
    ACTIVE --> USED: POST /payments/initiate<br/>(A2, atomic SET)
    ACTIVE --> EXPIRED: TTL=120s upłynął<br/>(Redis expire)
    USED --> [*]: Redis TTL wygasa<br/>(pozostaje w Transaction)
    EXPIRED --> [*]: Usunięty z Redis
```

**Transaction** (w Postgres, cache statusu w Redis):

```mermaid
---
config:
  theme: dark
---
stateDiagram-v2
    [*] --> PENDING: /initiate przyjęte,<br/>webhook zlecony do Celery (A2)

    PENDING --> AUTHORIZED: Bank zwrócił 200<br/>{authorized: true} (A3)
    PENDING --> REJECTED: Bank zwrócił 200<br/>{authorized: false}<br/>(A3, np. insufficient funds)
    PENDING --> TIMEOUT: Celery retry wyczerpany<br/>(bank unreachable, A3)

    AUTHORIZED --> COMPLETED: /confirm ACCEPTED<br/>ledger entries zapisane (A4)
    AUTHORIZED --> REJECTED: /confirm REJECTED<br/>(klient jednak odrzucił)

    COMPLETED --> SETTLED: Sesja nettingowa udana,<br/>wszystkie entries transakcji<br/>SETTLED (A5)
    COMPLETED --> SETTLEMENT_FAILED: Transfer RTGS fail,<br/>entries wracają do<br/>następnej sesji (A5)

    SETTLEMENT_FAILED --> SETTLED: Następna sesja udana

    REJECTED --> [*]: Stan terminalny
    TIMEOUT --> [*]: Stan terminalny
    SETTLED --> [*]: Stan terminalny
```

**Uwagi do maszyny stanów Transaction:**

- `PENDING → REJECTED` i `PENDING → TIMEOUT` nie tworzą ledger entries (nic do rozliczenia).
- `AUTHORIZED → REJECTED` jest rzadki, ale możliwy: bank w webhooku zwrócił OK, ale przy `/confirm` już zwrócił REJECTED (np. user zmienił zdanie, bank znalazł AML flag).
- `SETTLEMENT_FAILED` nie jest naprawdę terminalny — entries wracają do puli i próbujemy ponownie w następnej sesji.
- Stany widoczne przez polling (`/payments/status`): PENDING, AUTHORIZED, COMPLETED, REJECTED, TIMEOUT. Stany SETTLED/SETTLEMENT_FAILED są po stronie clearingu i nie interesują agenta.

---

## B3 — Stany LedgerEntry

LedgerEntry to pojedyncze zobowiązanie wygenerowane z transakcji (jedna z kilku
pozycji w splicie: merchant_net, klik_fee, agent_fee). Żyje od `/confirm` do
zamknięcia sesji rozliczeniowej.

```mermaid
---
config:
  theme: dark
---
stateDiagram-v2
    [*] --> PENDING_SETTLEMENT: INSERT po /confirm ACCEPTED<br/>(A4, settled=False, session_id=NULL)

    PENDING_SETTLEMENT --> LOCKED: Rozpoczęcie sesji<br/>nettingowej (A5 krok 2)<br/>SET session_id=X, FOR UPDATE

    LOCKED --> SETTLED: SettlementTransfer<br/>zawierający ten entry<br/>zakończony SUCCESS (A5 krok 7)

    LOCKED --> PENDING_SETTLEMENT: SettlementTransfer<br/>zawierający ten entry<br/>zakończony FAILED<br/>SET session_id=NULL<br/>(wraca do puli)

    SETTLED --> [*]: Stan terminalny

    note right of PENDING_SETTLEMENT: settled=False,<br/>session_id=NULL<br/>(dostępne dla nowej sesji)
    note right of LOCKED: settled=False,<br/>session_id=X<br/>(przetwarzany)
    note right of SETTLED: settled=True,<br/>session_id=X<br/>(zamknięte)
```

**Uwagi:**

- `LOCKED` to nie osobny enum, tylko widok logiczny: `settled=False AND session_id IS NOT NULL`.
- Bank zablokowany (`bank.active=False`) nie powoduje zmiany stanu entry — entry nadal `PENDING_SETTLEMENT`, ale query w A5 kroku 2 pomija go przez WHERE clause.
- Brak stanu terminalnego `FAILED` dla pojedynczego entry — fail jest zawsze na poziomie SettlementTransfer, a entry wraca do puli.

---

## C1 — ERD (model bazy danych)

Trzon modelu domenowego. Pomija tabele techniczne (django_migrations, auth_user,
sessions) i audit log.

```mermaid
---
config:
  theme: dark
---
erDiagram
    Bank ||--o{ BankWebhookConfig : "ma"
    Bank ||--o{ Transaction : "jest_nadawca"
    Bank ||--o{ Agent : "prowadzi_konto"
    Bank ||--o{ Merchant : "prowadzi_konto"
    Bank ||--o{ LedgerEntry : "strona_from"
    Bank ||--o{ LedgerEntry : "strona_to"
    Bank }o--|| Zone : "operuje_w"

    Agent ||--|| MSCAgreement : "ma"
    Agent ||--o{ Transaction : "inicjuje"
    Agent }o--|| Zone : "operuje_w"

    Merchant ||--o{ Transaction : "beneficjent"

    Transaction ||--o{ LedgerEntry : "generuje"
    Transaction }o--|| Zone : "odbywa_sie_w"

    SettlementSession ||--o{ SettlementTransfer : "zawiera"
    SettlementSession ||--o{ LedgerEntry : "lockuje"
    SettlementSession }o--|| Zone : "dotyczy"

    SettlementTransfer }o--|| Bank : "from_bank"
    SettlementTransfer }o--|| Bank : "to_bank"

    Bank {
        uuid id PK
        string name
        string api_key_hash
        string webhook_url
        enum zone FK
        string currency
        decimal debt_limit
        boolean active
        datetime created_at
    }

    BankWebhookConfig {
        uuid id PK
        uuid bank_id FK
        string url
        datetime last_ping_ok
        boolean verified
    }

    Agent {
        uuid id PK
        string name
        string api_key_hash
        uuid settlement_bank_id FK
        string iban
        enum zone FK
        boolean active
    }

    MSCAgreement {
        uuid id PK
        uuid agent_id FK
        decimal klik_fee_perc
        decimal agent_fee_perc
        datetime valid_from
        datetime valid_to
    }

    Merchant {
        uuid id PK
        string name
        uuid settlement_bank_id FK
        string iban
        enum zone FK
    }

    Transaction {
        uuid id PK
        string code_snapshot
        uuid sender_bank_id FK
        uuid agent_id FK
        uuid merchant_id FK
        decimal amount_gross
        decimal klik_fee
        decimal agent_fee
        decimal merchant_net
        string currency
        enum zone FK
        boolean is_on_us
        enum status
        string reject_reason
        string idempotency_key
        datetime created_at
        datetime authorized_at
        datetime completed_at
    }

    LedgerEntry {
        uuid id PK
        uuid transaction_id FK
        uuid from_bank_id FK
        uuid to_bank_id FK
        string beneficiary_type
        uuid beneficiary_ref
        decimal amount
        string currency
        enum zone FK
        uuid session_id FK
        boolean settled
        datetime created_at
        datetime settled_at
    }

    SettlementSession {
        uuid id PK
        enum zone FK
        enum status
        datetime started_at
        datetime closed_at
        int total_transfers
        int success_count
        int failed_count
    }

    SettlementTransfer {
        uuid id PK
        uuid session_id FK
        uuid from_bank_id FK
        uuid to_bank_id FK
        decimal amount
        string currency
        enum status
        string rtgs_reference
        string failure_reason
        datetime created_at
        datetime completed_at
    }

    Zone {
        string code PK
        string currency
        string rtgs_system
        int session_interval_minutes
    }
```

**Uwagi do ERD:**

1. **`beneficiary_type` + `beneficiary_ref` w LedgerEntry** — beneficjentem może być bank (merchant_net, inter-bank), KLIK (klik_fee), lub Agent (agent_fee). Zamiast trzech osobnych FK używamy polimorficznej referencji: `beneficiary_type` enum (BANK / KLIK / AGENT), `beneficiary_ref` UUID. Samo pole `to_bank_id` wskazuje bank fizycznie otrzymujący środki przez RTGS (czyli bank w którym beneficjent ma konto — `settlement_bank_id` agenta/merchanta lub bank reprezentujący KLIK).

2. **KLIK jako "uczestnik"** — KLIK nie ma osobnej tabeli. Jest reprezentowany przez konstantę (np. singleton `KLIK_ACCOUNT` w kodzie) z polem `settlement_bank_id` w konfiguracji (`.env` per strefa). Analogicznie: w strefie PL KLIK ma konto w Banku X, w strefie UK w Banku Y itd.

3. **`Code` nie jest w ERD** — bo żyje tylko w Redisie i nie ma reprezentacji w Postgres. `Transaction.code_snapshot` przechowuje wartość kodu dla audytu.

4. **`Zone` jako tabela** — formalnie moglibyśmy to trzymać w `.env` jako enum, ale tabela daje operator'owi możliwość zmiany `session_interval_minutes` bez restartu (Django admin). Useful dla demo.

5. **Brakuje tu** — audit log (kto kiedy co zmienił w banku/agencie), tabela alertów dla operatora, tabela historii transakcji idempotency. Do dopisania jeśli będzie potrzebne, ale nie jest krytyczne dla rdzenia.

---

## C2 — Dispatcher RTGS (diagram klas / strategy pattern)

Architektura dispatcher'a: jedna abstrakcja, cztery implementacje, wybór po strefie.

```mermaid
---
config:
  theme: dark
---
classDiagram
    class RTGSGateway {
        <<interface>>
        +settle(session_id, transfers) List~TransferResult~
        +healthcheck() bool
    }

    class SORBNET3Gateway {
        -base_url: str
        -api_key: str
        +settle(session_id, transfers) List~TransferResult~
        +healthcheck() bool
        -build_payload(transfers) dict
        -parse_response(resp) List~TransferResult~
    }

    class TARGET2Gateway {
        -base_url: str
        -api_key: str
        +settle(session_id, transfers) List~TransferResult~
        +healthcheck() bool
        -build_payload(transfers) dict
        -parse_response(resp) List~TransferResult~
    }

    class CHAPSGateway {
        -base_url: str
        -api_key: str
        +settle(session_id, transfers) List~TransferResult~
        +healthcheck() bool
    }

    class FedNowGateway {
        -base_url: str
        -api_key: str
        +settle(session_id, transfers) List~TransferResult~
        +healthcheck() bool
    }

    class RTGSDispatcher {
        -gateways: Dict~Zone, RTGSGateway~
        +dispatch(zone, session_id, transfers) List~TransferResult~
        +healthcheck_all() Dict~Zone, bool~
    }

    class Zone {
        <<enumeration>>
        PL
        EU
        UK
        US
    }

    class TransferResult {
        +transfer_id: UUID
        +status: TransferStatus
        +rtgs_reference: str
        +failure_reason: str
    }

    class TransferStatus {
        <<enumeration>>
        SUCCESS
        FAILED
        TIMEOUT
    }

    class SettlementWorker {
        +run_settlement_session(zone)
    }

    RTGSGateway <|.. SORBNET3Gateway : implements
    RTGSGateway <|.. TARGET2Gateway : implements
    RTGSGateway <|.. CHAPSGateway : implements
    RTGSGateway <|.. FedNowGateway : implements

    RTGSDispatcher o--> RTGSGateway : holds 4
    RTGSDispatcher ..> Zone : uses
    RTGSDispatcher ..> TransferResult : returns

    SettlementWorker --> RTGSDispatcher : uses
    TransferResult --> TransferStatus : has
```

**Mapowanie Zone → Gateway (w RTGSDispatcher):**

| Zone | Gateway | Waluta | URL (z `.env`) |
|---|---|---|---|
| PL | SORBNET3Gateway | PLN | `SORBNET3_URL` |
| EU | TARGET2Gateway | EUR | `TARGET2_URL` |
| UK | CHAPSGateway | GBP | `CHAPS_URL` |
| US | FedNowGateway | USD | `FEDNOW_URL` |
