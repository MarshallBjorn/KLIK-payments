# KLIK Cheques — Diagramy sekwencji

Diagramy sekwencji dla modułu Czeki (Cheques). Format spójny z dokumentacją
C2B (`docs/c2b/diagrams/`) i P2P (`docs/p2p/diagrams/`).

W przeciwieństwie do C2B (gdzie autoryzacja klienta dzieje się **podczas**
płatności) i podobnie do P2P (gdzie KLIK nie uczestniczy w autoryzacji),
w module Czeków **autoryzacja klienta dzieje się przy wystawieniu czeku po
stronie banku**, zanim KLIK zarejestruje czek. KLIK ufa bankowi że hold jest
założony.

---

## Spis treści

### A. Cykl życia czeku
- [CH0 — Wystawienie czeku (issue)](#ch0--wystawienie-czeku-issue)
- [CH1 — Realizacja czeku (redeem)](#ch1--realizacja-czeku-redeem)
- [CH2 — Anulacja czeku (cancel)](#ch2--anulacja-czeku-cancel)
- [CH3 — Wygaśnięcie czeku (expire)](#ch3--wygaśnięcie-czeku-expire)

### B. Settlement
- [CH4 — Settlement realizacji (referencja do A5)](#ch4--settlement-realizacji-referencja-do-a5)

## Powiązana dokumentacja
- [../integration/INFO.md](../integration/INFO.md) — API reference, słownik, model rozliczeniowy
- [../diagrams/STATE.md](./STATE.md) — diagramy stanów + ERD update
- [../../c2b/diagrams/WORKFLOW.md](../../c2b/diagrams/WORKFLOW.md) — sekwencje C2B (analogiczny model)
- [../../c2b/diagrams/WORKFLOW.md#a5--netting--settlement-przez-rtgs](../../c2b/diagrams/WORKFLOW.md#a5--netting--settlement-przez-rtgs) — settlement (wspólny mechanizm)

---

## CH0 — Wystawienie czeku (issue)

Klient w aplikacji bankowej generuje czek. Bank lokalnie blokuje środki,
autoryzuje PIN-em, następnie rejestruje czek w KLIK i pokazuje 9-cyfrowy kod.

```mermaid
---
config:
  theme: dark
---
sequenceDiagram
    autonumber
    actor Klient
    participant BankN as Bank Wystawcy (Bank N)
    box System (KLIK)
        participant API as KLIK (Django API)
        participant DB as KLIK (PostgreSQL)
    end

    %% ETAP 1: Klient inicjuje czek w aplikacji bankowej
    Note over Klient, BankN: ETAP 1: Inicjacja po stronie klienta
    Klient->>BankN: "Wystaw czek 100 PLN, ważny 24h"
    BankN->>BankN: Walidacja: saldo >= 100 PLN, limit czeków klienta OK
    BankN-->>Klient: Wyświetla podsumowanie + prośbę o PIN
    Klient->>BankN: PIN
    BankN->>BankN: Weryfikacja PIN

    %% ETAP 2: Lokalny hold po stronie banku
    Note over BankN: ETAP 2: Hold po stronie banku<br/>(saldo dostępne -= 100, środki zablokowane)
    BankN->>BankN: Lokalny zapis: hold(client_id, 100 PLN, expires=now+24h)

    %% ETAP 3: Rejestracja czeku w KLIK
    Note over BankN, DB: ETAP 3: Bank rejestruje czek w KLIK
    BankN->>API: POST /cheques/issue<br/>{user_id, amount=100, currency=PLN, zone=PL, ttl_seconds=86400}<br/>X-KLIK-Bank-Api-Key, Idempotency-Key
    API->>API: Auth banku, weryfikacja cheques_enabled=True
    API->>API: Walidacja: zone == bank.zone, currency == bank.currency,<br/>ttl_seconds in [MIN, MAX]

    loop Generacja unikalnego kodu (max 10 prób)
        API->>API: code = secrets.randbelow(1_000_000_000), 9-digit
        API->>DB: INSERT Cheque (code, status=ACTIVE, ...) ON CONFLICT(code, status=ACTIVE) DO NOTHING
        alt Insert OK
            DB-->>API: cheque_id
        else Konflikt (kod zajęty)
            DB-->>API: empty
            Note right of API: Retry z nowym kodem
        end
    end

    alt Wygenerowano unikalny kod
        API-->>BankN: HTTP 201 {cheque_id, code, amount, expires_at, issued_at}
        BankN-->>Klient: Wyświetla kod 123456789 + info "Ważny do 2026-05-04 14:00"
    else 10 prób bez sukcesu (skrajnie rzadkie)
        API-->>BankN: HTTP 500_INTERNAL_ERROR
        BankN->>BankN: Rollback holda (zwolnij środki klienta)
        BankN-->>Klient: "Spróbuj za chwilę"
    end

    %% Scenariusze błędne (na poziomie KLIK API)
    Note over API: Błędy:<br/>- 400_INVALID_TTL (poza widełkami)<br/>- 400_INVALID_AMOUNT (≤0)<br/>- 401/403 (auth)<br/>- 422_CURRENCY_MISMATCH<br/>W każdym z tych przypadków bank MUSI zwolnić hold u siebie.
```

**Uwagi:**

- KLIK ufa że bank zablokował środki **przed** wywołaniem `/issue`. Brak tej blokady to bug po stronie banku — KLIK nie ma jak tego wyegzekwować.
- W razie błędu KLIK po stronie banku (np. `400_INVALID_TTL`) bank musi rollbacknąć swój hold. To samo gdy network failure i bank dostanie timeout — bank powinien retryować z tym samym `Idempotency-Key`; jeśli czek został zarejestrowany pomimo timeoutu, retry zwróci tę samą odpowiedź (KLIK nie utworzy duplikatu).

---

## CH1 — Realizacja czeku (redeem)

Klient pokazuje agentowi (sklep/bankomat) 9-cyfrowy kod. Agent wpisuje go
do swojego terminala. KLIK atomowo: zmienia stan czeku, tworzy `Transaction`
ze statusem COMPLETED, generuje ledger entries, kolejkuje webhook do banku
wystawcy.

```mermaid
---
config:
  theme: dark
---
sequenceDiagram
    autonumber
    actor Klient
    actor Kasjer
    participant Agent as Agent (Vue Terminal)
    box System (KLIK)
        participant API as KLIK (Django API)
        participant DB as KLIK (PostgreSQL)
        participant Q as KLIK (Celery Queue)
    end
    participant BankN as Bank Wystawcy

    %% ETAP 1: Klient pokazuje kod
    Note over Klient, Kasjer: ETAP 1: Klient pokazuje kod
    Klient->>Kasjer: "Zapłacę czekiem KLIK: 123456789"
    Kasjer->>Agent: Wpisuje kod, wybiera merchant_id (sklep)
    Agent->>Agent: Walidacja lokalna: format kodu 9 cyfr

    %% ETAP 2: Wywołanie KLIK
    Note over Agent, DB: ETAP 2: Atomowa realizacja czeku
    Agent->>API: POST /cheques/redeem<br/>{cheque_code: "123456789", merchant_id}<br/>X-KLIK-Agent-Api-Key, Idempotency-Key
    API->>API: Auth agenta, walidacja merchant_id

    API->>DB: BEGIN, SELECT Cheque WHERE code AND status=ACTIVE FOR UPDATE
    alt Czek istnieje, ACTIVE, nie wygasł
        DB-->>API: Cheque{id, amount, issuer_bank_id, expires_at, ...}
        API->>API: Walidacja: cheque.zone == agent.zone,<br/>cheque.expires_at > now
        API->>API: Wylicz is_on_us = (cheque.issuer_bank == merchant.settlement_bank)
        API->>API: AgentService.calculate_split(amount) → klik_fee, agent_fee, merchant_net

        API->>DB: INSERT Transaction (status=COMPLETED,<br/>cheque_id=cheque.id, amount_gross=cheque.amount,<br/>klik_fee, agent_fee, merchant_net, is_on_us, ...)
        DB-->>API: transaction_id

        API->>DB: UPDATE Cheque SET status=REDEEMED,<br/>redeemed_at=now, transaction_id=...
        DB-->>API: OK

        API->>DB: INSERT LedgerEntry × 2-3 (BANK_TO_BANK, KLIK_FEE_C2B, AGENT_FEE)<br/>(LedgerService.record_c2b_transaction)
        DB-->>API: OK
        API->>DB: COMMIT

        %% ETAP 3: Asynchroniczna notyfikacja banku
        API->>Q: enqueue notify_cheque_redeemed_task(cheque_id, transaction_id)
        Q-->>API: queued

        API-->>Agent: HTTP 200 {cheque_id, transaction_id, amount_gross,<br/>merchant_net, klik_fee, agent_fee, currency, redeemed_at}
        Agent-->>Kasjer: "Płatność zaakceptowana, 100 PLN"
        Kasjer-->>Klient: Paragon

    else Czek nie istnieje
        DB-->>API: empty
        API->>DB: ROLLBACK
        API-->>Agent: HTTP 404_CHEQUE_NOT_FOUND
        Agent-->>Kasjer: "Niepoprawny kod czeku"
    else Czek wygasł (expires_at <= now ale jeszcze nie przeszedł cron expire)
        DB-->>API: Cheque (ACTIVE)
        API->>API: Walidacja: now > expires_at
        API->>DB: UPDATE Cheque SET status=EXPIRED, ROLLBACK Transaction
        API->>Q: enqueue notify_cheque_released_task(reason=EXPIRED)
        API-->>Agent: HTTP 404_CHEQUE_EXPIRED
    else Czek REDEEMED/CANCELLED
        Note right of DB: SELECT FOR UPDATE bez WHERE status=ACTIVE<br/>znajdzie czek, ale my filtrujemy po ACTIVE.<br/>Drugi SELECT bez filtra dla error message.
        API->>DB: SELECT Cheque WHERE code=...
        DB-->>API: Cheque{status: REDEEMED|CANCELLED}
        API->>DB: ROLLBACK
        API-->>Agent: HTTP 409_CHEQUE_ALREADY_REDEEMED<br/>(lub 409_CHEQUE_ALREADY_CANCELLED)
    end

    %% ETAP 4: Webhook do banku wystawcy (async, niezależnie od response)
    Note over Q, BankN: ETAP 4: Worker notyfikuje bank wystawcy (niezależnie od response do agenta)
    Q->>BankN: POST {bank.cheques_webhook_url}/redeemed<br/>{cheque_id, transaction_id, amount, currency, redeemed_at, user_id}
    alt Bank odpowiada 200
        BankN->>BankN: Zwolnij hold, zaksięguj debet 100 PLN<br/>na koncie klienta, push do klienta
        BankN-->>Q: HTTP 200 {received: true}
    else Bank fail (timeout, 5xx)
        BankN-->>Q: error
        Q->>Q: Retry exponential backoff (5s, 30s, 2min, 3 próby)
        Q->>Q: Po 3 failach: log + alert dla operatora KLIK
    end

    Note over Q, BankN: ETAP 5: Settlement międzybankowy idzie niezależnie<br/>(LedgerEntries trafiły do najbliższej sesji A5)
```

**Uwagi:**

- Cała sekwencja od `BEGIN` do `COMMIT` jest atomowa — jednocześnie zmiana statusu czeku, INSERT Transaction, INSERT LedgerEntries. Brak ryzyka half-state.
- `SELECT FOR UPDATE` blokuje wiersz Cheque przed równoczesną podwójną realizacją (gdyby dwóch agentów wpisało ten sam kod jednocześnie — drugi czeka, dostaje stan REDEEMED, dostaje 409).
- Webhook do banku wystawcy jest **fire-and-forget** z perspektywy agenta — odpowiedź do agenta NIE czeka na bank. Bank dostanie webhook w ciągu sekund (typowo).
- Jeśli bank wystawcy nie odpowie na 3 retry — czek jest dla KLIK zrealizowany (entries idą do nettingu, sesja zamyka się normalnie), ale klient w aplikacji bankowej może nadal widzieć "czek aktywny". Operator KLIK musi mieć panel do redrive notyfikacji (TBD jako osobne zadanie).

---

## CH2 — Anulacja czeku (cancel)

Klient w aplikacji bankowej kasuje czek. Bank wywołuje KLIK, KLIK zmienia
stan i kolejkuje notyfikację o release.

```mermaid
---
config:
  theme: dark
---
sequenceDiagram
    autonumber
    actor Klient
    participant BankN as Bank Wystawcy
    box System (KLIK)
        participant API as KLIK (Django API)
        participant DB as KLIK (PostgreSQL)
        participant Q as KLIK (Celery Queue)
    end

    %% ETAP 1: Klient kasuje czek
    Note over Klient, BankN: ETAP 1: Klient klika "Anuluj czek"
    Klient->>BankN: Anuluj czek 123456789
    BankN->>BankN: Identyfikacja czeku po user_id + cheque_id (bank trzyma mapping lokalnie)

    %% ETAP 2: Bank wywołuje KLIK
    Note over BankN, DB: ETAP 2: Anulacja w KLIK
    BankN->>API: POST /cheques/cancel {cheque_id}<br/>X-KLIK-Bank-Api-Key, Idempotency-Key
    API->>API: Auth banku, weryfikacja cheques_enabled

    API->>DB: INSERT Cheque (code, status=ACTIVE, ...) ON CONFLICT(code, status=ACTIVE) DO NOTHING
    alt Czek nie istnieje
        DB-->>API: empty
        API->>DB: ROLLBACK
        API-->>BankN: HTTP 404_CHEQUE_NOT_FOUND
    else Czek istnieje ale to nie jego bank
        DB-->>API: Cheque (issuer_bank != requesting_bank)
        API->>DB: ROLLBACK
        API-->>BankN: HTTP 403_INSUFFICIENT_PERMISSIONS<br/>(lub 404 dla security)
    else Czek nie ACTIVE
        DB-->>API: Cheque (status=REDEEMED|CANCELLED|EXPIRED)
        API->>DB: ROLLBACK
        alt status==CANCELLED i Idempotency-Key ten sam
            API-->>BankN: HTTP 200 {cheque_id, status: CANCELLED, cancelled_at}<br/>(replay idempotentny)
        else
            API-->>BankN: HTTP 409_CHEQUE_NOT_ACTIVE
        end
    else Czek ACTIVE
        DB-->>API: Cheque (status=ACTIVE)
        API->>DB: UPDATE Cheque SET status=CANCELLED, cancelled_at=now
        DB-->>API: OK
        API->>DB: COMMIT

        API->>Q: enqueue notify_cheque_released_task(cheque_id, reason=CANCELLED)
        Q-->>API: queued

        API-->>BankN: HTTP 200 {cheque_id, status: CANCELLED, cancelled_at}
        BankN-->>Klient: "Czek anulowany"
    end

    %% ETAP 3: Worker notyfikuje bank o release
    Note over Q, BankN: ETAP 3: Webhook /released (zwykle ten sam bank co requesting)
    Q->>BankN: POST {bank.cheques_webhook_url}/released<br/>{cheque_id, amount, currency, reason: "CANCELLED", released_at, user_id}
    BankN->>BankN: Zwolnij hold (saldo dostępne klienta wraca do +100)
    BankN-->>Q: HTTP 200
```

**Uwagi:**

- W praktyce bank wywołujący `/cancel` jest tym samym bankiem co odbiorca webhooka `/released` — ale rozdzielenie tych dwóch ścieżek jest świadome: cancel jest synchroniczny (klient czeka na potwierdzenie w aplikacji), release jest asynchroniczny i może być retryowany.
- Idempotency: drugi `/cancel` z tym samym `Idempotency-Key` na ten sam czek zwraca to samo bez tworzenia drugiej notyfikacji.

---

## CH3 — Wygaśnięcie czeku (expire)

Cron job KLIK skanuje czeki z `expires_at <= now` i `status=ACTIVE`,
oznacza je jako EXPIRED i kolejkuje webhooki release.

```mermaid
---
config:
  theme: dark
---
sequenceDiagram
    autonumber
    participant Beat as KLIK (Celery Beat)
    participant Worker as KLIK (Celery Worker)
    participant DB as KLIK (PostgreSQL)
    participant Q as KLIK (Celery Queue)
    participant BankN as Bank Wystawcy

    %% ETAP 1: Trigger
    Note over Beat, Worker: ETAP 1: Cron co 1 minutę (cheques.expire_due)
    Beat->>Worker: Queue: expire_due_cheques()

    %% ETAP 2: Skanowanie wygasłych czeków
    Note over Worker, DB: ETAP 2: Znajdź czeki do wygaszenia
    Worker->>DB: SELECT Cheque WHERE status=ACTIVE AND expires_at <= now LIMIT 1000

    alt Brak czeków
        DB-->>Worker: empty
        Note right of Worker: No-op, koniec
    else Są czeki
        DB-->>Worker: lista czeków (np. 25)

        loop Per czek (w batchu po 1000, dla każdego osobna mała transakcja)
            Worker->>DB: BEGIN, SELECT Cheque WHERE id=X AND status=ACTIVE, FOR UPDATE
            alt Stan się zmienił między SELECT a UPDATE (race z redeem/cancel)
                DB-->>Worker: status != ACTIVE
                Worker->>DB: ROLLBACK
            else Nadal ACTIVE
                DB-->>Worker: Cheque (ACTIVE)
                Worker->>DB: UPDATE Cheque SET status=EXPIRED, expired_at=now
                Worker->>DB: COMMIT
                Worker->>Q: enqueue notify_cheque_released_task(cheque_id, reason=EXPIRED)
            end
        end
    end

    %% ETAP 3: Worker notyfikuje banki (każdy czek osobno)
    Note over Q, BankN: ETAP 3: Webhook /released per czek
    loop Per zakolejkowany task
        Q->>BankN: POST {bank.cheques_webhook_url}/released<br/>{cheque_id, amount, reason: "EXPIRED", released_at, user_id}
        alt Bank 200
            BankN->>BankN: Zwolnij hold
            BankN-->>Q: HTTP 200
        else Bank fail
            Q->>Q: Retry 5s/30s/2min, alert po 3 failach
        end
    end
```

**Uwagi:**

- Cron co minutę = czek wygasa z dokładnością do ~60s. Akceptowalne — dla 24h+ TTL nieistotne.
- Limit `LIMIT 1000` per run zabezpiecza przed eksplozją gdy operator zostawił system bez expire'a przez dłuższy czas. Jeśli >1000 do wygaszenia, kolejny run za minutę zabierze następne.
- Race z `/redeem` i `/cancel`: oba używają `SELECT FOR UPDATE` na tym samym wierszu — wygrywa pierwszy. Worker expire sprawdza ponownie status po lock-u i pomija jeśli się zmienił.

---

## CH4 — Settlement realizacji (referencja do A5)

Settlement międzybankowy realizacji czeku jest **identyczny** jak dla zwykłego C2B:

- LedgerEntries wygenerowane przy `/redeem` (BANK_TO_BANK, KLIK_FEE_C2B, AGENT_FEE) trafiają do następnej sesji rozliczeniowej dla strefy
- Sesja: multilateral netting → SettlementTransfers → RTGS dispatch
- Bank wystawcy debetuje konto klienta lokalnie (po webhooku `/redeemed`), niezależnie od momentu nettingu

Pełny diagram: [C2B WORKFLOW A5](../../c2b/diagrams/WORKFLOW.md#a5--netting--settlement-przez-rtgs).

**Co jest specyficzne dla czeku w settlemencie:** **nic**. Po redempcji czek jest niewidoczny dla mechanizmu settlement — widzi tylko LedgerEntries.

---

## Podsumowanie różnic Cheques vs C2B vs P2P

| Aspekt | C2B (Kody) | P2P (Telefony) | Cheques (Czeki) |
|---|---|---|---|
| **Czas życia obiektu** | 120s w Redis | trwały w Postgres | 1h–72h w Postgres |
| **Format kodu** | 6 cyfr | n/d (telefon E.164) | 9 cyfr |
| **Autoryzacja klienta** | Push do banku, PIN przy płatności | Wewnątrz banku | PIN przy wystawieniu (przed `/issue`) |
| **Webhook autoryzacyjny** | Tak (`/authorize`) | Nie | Nie (autoryzacja przed issue) |
| **Webhook end-of-life** | Nie (status w `/payments/status`) | Nie | Tak (`/redeemed`, `/released`) |
| **Tworzenie Transaction** | `/payments/initiate` (status PENDING) | n/d | `/cheques/redeem` (status COMPLETED od razu) |
| **Hold środków** | Implicit przy `/confirm ACCEPTED` | n/d (KLIK nie uczestniczy) | Explicit przy `/issue`, release przy końcu cyklu |
| **Sesja rozliczeniowa** | Tak | Tak | Tak (wspólna z C2B) |
| **Prowizja KLIK** | % od kwoty | per lookup | % od kwoty (jak C2B) |
| **Cron tasks** | A5 settlement | P4 accrual + A5 settlement | CH3 expire + A5 settlement |
