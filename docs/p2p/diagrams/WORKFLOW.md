# KLIK — Diagramy P2P (workflow + stany)

Diagramy sekwencji oraz stanów dla modułu Telefony (P2P) systemu KLIK.
Format spójny z dokumentacją C2B (`docs/c2b/diagrams/`).

W przeciwieństwie do C2B, KLIK w P2P pełni rolę **wyłącznie routera aliasów**
— właściwy przepływ pieniędzy dzieje się poza KLIK, przez systemy płatności
natychmiastowych (Elixir Express / Faster Payments / SEPA Instant / FedNow RTP)
realizowane bezpośrednio między bankami.

KLIK pobiera prowizję od banków za **każde wywołanie lookup**
(model lookup-based fee). Stawka per-bank jest negocjowana przy onboardingu
i przechowywana w polu `Bank.p2p_lookup_fee`. Naliczone prowizje agregowane
są nocą i rozliczane przez ten sam mechanizm settlement co C2B (RTGS).

---

## Spis treści

### A. Onboarding
- [P0 — Onboarding banku w P2P](#p0--onboarding-banku-w-p2p)

### B. Cykl życia aliasu
- [P1 — Rejestracja aliasu](#p1--rejestracja-aliasu)
- [P2 — Lookup aliasu (przelew P2P)](#p2--lookup-aliasu-przelew-p2p)
- [P3 — Wyrejestrowanie aliasu](#p3--wyrejestrowanie-aliasu)

### C. Rozliczenia
- [P4 — Naliczanie prowizji (daily fee accrual)](#p4--naliczanie-prowizji-daily-fee-accrual)

### D. Stany
- [Stany Alias](#stany-alias)

## Powiązana dokumentacja
- [../integration/INFO.md](../integration/INFO.md) — API reference, słownik, pricing
- [../bpmn/](../bpmn/) — diagramy BPMN
- [../../c2b/diagrams/WORKFLOW.md](../../c2b/diagrams/WORKFLOW.md) — analogiczne diagramy dla C2B
- [../../c2b/diagrams/STATE.md](../../c2b/diagrams/STATE.md) — pełny ERD systemu
- [../../c2b/diagrams/WORKFLOW.md#a5--netting--settlement-przez-rtgs](../../c2b/diagrams/WORKFLOW.md#a5--netting--settlement-przez-rtgs) — settlement (wspólny dla C2B i P2P)

---

## P0 — Onboarding banku w P2P

Proces aktywacji modułu P2P dla banku. Analogiczny do onboardingu C2B (A0)
z drobnymi różnicami: bank deklaruje udział w module Telefony, dostaje
ustaloną stawkę `p2p_lookup_fee`.

```mermaid
---
config:
  theme: dark
---
sequenceDiagram
    autonumber
    actor BankOps as Przedstawiciel Banku
    actor KlikOps as Operator KLIK
    participant Admin as KLIK (Django Admin)
    participant DB as KLIK (PostgreSQL)
    participant BankAPI as Bank (API)
    participant API as KLIK (Django API)

    %% ETAP 0: Off-system
    Note over BankOps, KlikOps: ETAP 0: Uzgodnienia poza systemem (umowa, strefy P2P, stawka)
    BankOps->>KlikOps: Zgłoszenie chęci integracji modułu P2P
    KlikOps->>BankOps: Due diligence, ustalenie stref P2P i stawki p2p_lookup_fee

    %% ETAP 1: Aktywacja P2P i konfiguracja stawki
    Note over KlikOps, DB: ETAP 1: Aktywacja P2P dla banku
    KlikOps->>Admin: Edycja Bank: zaznaczenie p2p_enabled=True,<br/>ustawienie p2p_lookup_fee (np. 0.05 PLN)
    Admin->>DB: UPDATE Bank SET p2p_enabled=True, p2p_lookup_fee=...
    DB-->>Admin: OK

    %% Jeśli nowy bank — generujemy api_key (patrz C2B/A0 dla pełnego flow)
    Note over Admin, KlikOps: Dla nowego banku: generowanie api_key<br/>(patrz C2B/A0 ETAP 1-4)

    %% ETAP 2: Bank konfiguruje klienta P2P
    Note over BankOps, BankAPI: ETAP 2: Konfiguracja klienta P2P po stronie banku
    BankOps->>BankAPI: Konfiguracja URL endpointów /aliases/* w KLIK

    %% ETAP 3: Test pierwszego wywołania
    Note over BankAPI, API: ETAP 3: Bank wywołuje produkcyjny endpoint
    BankAPI->>API: POST /aliases/register (test alias)
    API->>API: Weryfikacja: bank.active=True, p2p_enabled=True
    API-->>BankAPI: HTTP 201 (alias zarejestrowany)

    Note over API: Bank gotowy do P2P
```

---

## P1 — Rejestracja aliasu

Klient włącza w aplikacji bankowej "Przelew na telefon". Bank rejestruje
mapowanie numer telefonu → konto w KLIK.

**Uwaga:** rejestracja jest **darmowa** — KLIK nie pobiera fee za REGISTER
ani za DELETE, tylko za LOOKUP (P2). Logika: rejestrowanie i wyrejestrowanie
to operacje administracyjne klienta, lookup to operacja przy każdym przelewie.

```mermaid
---
config:
  theme: dark
---
sequenceDiagram
    autonumber
    actor Klient
    participant BankA as Bank Klienta (Bank A)
    box System (KLIK)
        participant API as KLIK (Django API)
        participant DB as KLIK (PostgreSQL)
    end

    %% ETAP 1: Klient włącza usługę
    Note over Klient, BankA: ETAP 1: Klient aktywuje "Przelew na telefon"
    Klient->>BankA: Klika "Włącz przelewy na telefon" w aplikacji
    BankA->>BankA: Weryfikacja sesji klienta, akceptacja regulaminu

    %% ETAP 2: Wywołanie KLIK
    Note over BankA, DB: ETAP 2: Bank rejestruje alias w KLIK
    BankA->>API: POST /aliases/register<br/>(X-KLIK-Bank-Api-Key, phone, account_identifier, zone)
    API->>API: Uwierzytelnienie banku (Bank A musi mieć p2p_enabled=True)
    API->>API: Walidacja: format E.164, account_identifier zgodny ze strefą,<br/>strefa banku == strefa aliasu

    %% ETAP 3: Sprawdzenie unikalności
    Note over API, DB: ETAP 3: Sprawdzenie czy numer już zarejestrowany
    API->>DB: SELECT Alias WHERE phone=... AND zone=...
    alt Alias istnieje
        DB-->>API: Alias (innym bankiem lub kontem)
        API-->>BankA: HTTP 409_ALIAS_ALREADY_EXISTS
    else Alias nie istnieje
        DB-->>API: empty
        API->>DB: INSERT Alias (phone, bank_id=BankA.id, account_identifier, zone)
        DB-->>API: alias_id
        API-->>BankA: HTTP 201 {alias_id, phone, registered_at}
        BankA-->>Klient: "Twój numer +48... jest aktywny w KLIK"
    end

    %% Scenariusze błędne
    Note over API: Błędy:<br/>- 401 (brak/zły api_key)<br/>- 403 (bank bez p2p_enabled)<br/>- 422_ZONE_MISMATCH (strefa banku ≠ strefa aliasu)<br/>- 400 (zły format telefonu lub account_identifier)
```

---

## P2 — Lookup aliasu (przelew P2P)

Bank nadawcy chce wykonać przelew P2P. KLIK pełni rolę "książki telefonicznej"
— zwraca dane routingu i **inkrementuje licznik prowizji** dla banku-pytającego.
**Właściwy przelew odbywa się poza KLIK** przez systemy RTP.

**Prowizja:** każde **udane** wywołanie lookup (zwracające dane routingu)
inkrementuje counter `bank_lookups:{bank_id}:{date}` w Redisie. Lookup
zakończony 404 (alias nie istnieje) **nie nalicza** prowizji — bank nie skorzystał z usługi.

```mermaid
---
config:
  theme: dark
---
sequenceDiagram
    autonumber
    actor Nadawca as Klient Nadawcy
    actor Odbiorca as Klient Odbiorcy
    participant BankN as Bank Nadawcy (Bank B)
    participant BankO as Bank Odbiorcy (Bank A)
    box System (KLIK)
        participant API as KLIK (Django API)
        participant DB as KLIK (PostgreSQL)
        participant Redis as KLIK (Redis)
    end
    participant RTP as System RTP<br/>(Elixir Express / Faster Payments / SEPA Instant / FedNow)

    %% ETAP 1: Inicjacja przelewu
    Note over Nadawca, BankN: ETAP 1: Nadawca inicjuje przelew P2P
    Nadawca->>BankN: "Wyślij 100 PLN na +48501234567"
    BankN->>BankN: Weryfikacja sesji nadawcy

    %% ETAP 2: Lookup aliasu w KLIK
    Note over BankN, Redis: ETAP 2: Bank pyta KLIK gdzie wysłać
    BankN->>API: GET /aliases/lookup/+48501234567<br/>(X-KLIK-Bank-Api-Key)
    API->>API: Uwierzytelnienie banku, weryfikacja strefy
    API->>DB: SELECT Alias WHERE phone=... AND zone=...

    alt Alias istnieje
        DB-->>API: Alias (bank_id=Bank A, account_identifier)
        API->>Redis: INCR bank_lookups:{BankN.id}:{today}<br/>(licznik prowizji)
        Redis-->>API: OK
        API-->>BankN: HTTP 200 {phone, bank_id, bank_code, account_identifier}
    else Alias nie istnieje
        DB-->>API: empty
        Note right of API: Brak inkrementu countera —<br/>bank nie skorzystał z usługi
        API-->>BankN: HTTP 404_ALIAS_NOT_FOUND
        BankN-->>Nadawca: "Numer nie obsługuje przelewu na telefon"
    end

    %% ETAP 3: Autoryzacja klienta
    Note over Nadawca, BankN: ETAP 3: Autoryzacja przelewu (po stronie banku)
    BankN-->>Nadawca: Wyświetla dane odbiorcy do potwierdzenia
    Nadawca->>BankN: Akceptuje PIN-em
    BankN->>BankN: Blokada środków, kompozycja transferu

    %% ETAP 4: Właściwy przelew (POZA KLIK)
    Note over BankN, BankO: ETAP 4: Fizyczny przelew przez RTP (poza KLIK)
    BankN->>RTP: Inicjacja przelewu natychmiastowego<br/>(account_identifier z lookup)
    RTP->>BankO: Routing transferu
    BankO->>BankO: Księgowanie środków na koncie odbiorcy
    BankO->>Odbiorca: Push: "Otrzymałeś 100 PLN od +48..."
    BankO-->>RTP: HTTP 200 (zaksięgowano)
    RTP-->>BankN: HTTP 200 (transfer zakończony)
    BankN-->>Nadawca: Ekran "Przelew wykonany"

    %% Scenariusze błędne
    Note over API, RTP: Błędy KLIK:<br/>- 404_ALIAS_NOT_FOUND (brak inkrementu)<br/>- 401/403 (auth, brak inkrementu)<br/>- 422_ZONE_MISMATCH (brak inkrementu)<br/><br/>Błędy poza KLIK (problem banku/RTP):<br/>- niepowodzenie przelewu RTP<br/>- niewystarczające środki nadawcy<br/>- limit przelewu przekroczony
```

---

## P3 — Wyrejestrowanie aliasu

Klient wyłącza usługę. Bank usuwa alias z KLIK.

```mermaid
---
config:
  theme: dark
---
sequenceDiagram
    autonumber
    actor Klient
    participant BankA as Bank Klienta (Bank A)
    box System (KLIK)
        participant API as KLIK (Django API)
        participant DB as KLIK (PostgreSQL)
    end

    %% ETAP 1: Klient wyłącza usługę
    Note over Klient, BankA: ETAP 1: Klient wyłącza "Przelew na telefon"
    Klient->>BankA: Klika "Wyłącz przelewy na telefon"
    BankA->>BankA: Weryfikacja sesji klienta

    %% ETAP 2: Wywołanie KLIK
    Note over BankA, DB: ETAP 2: Bank usuwa alias w KLIK
    BankA->>API: DELETE /aliases/+48501234567<br/>(X-KLIK-Bank-Api-Key)
    API->>API: Uwierzytelnienie banku
    API->>DB: SELECT Alias WHERE phone=... AND zone=...

    alt Alias istnieje
        DB-->>API: Alias
        API->>API: Weryfikacja: alias.bank_id == requesting_bank.id<br/>(bank może usuwać tylko swoje aliasy)

        alt Bank jest właścicielem
            API->>DB: DELETE Alias
            DB-->>API: OK
            API-->>BankA: HTTP 204 No Content
            BankA-->>Klient: "Usługa wyłączona"
        else Bank nie jest właścicielem
            API-->>BankA: HTTP 403_INSUFFICIENT_PERMISSIONS
            Note right of API: Bank A nie może usuwać<br/>aliasów Banku B
        end
    else Alias nie istnieje
        DB-->>API: empty
        API-->>BankA: HTTP 404_ALIAS_NOT_FOUND
    end

    %% Scenariusze błędne
    Note over API: Błędy:<br/>- 401 (auth)<br/>- 403 (próba usunięcia cudzego aliasu)<br/>- 404 (alias nie istnieje)
```

---

## P4 — Naliczanie prowizji (daily fee accrual)

Celery Beat task uruchamiany raz dziennie (np. o 23:55) agreguje countery
lookupów per bank, oblicza naliczone prowizje i tworzy `LedgerEntry` dla
każdego banku który wykonywał lookupy. Te entries lecą przez ten sam
mechanizm settlement co C2B fees (patrz [C2B WORKFLOW A5](../../c2b/diagrams/WORKFLOW.md#a5--netting--settlement-przez-rtgs)).

```mermaid
---
config:
  theme: dark
---
sequenceDiagram
    autonumber
    participant Beat as KLIK (Celery Beat)
    box System (KLIK)
        participant Worker as KLIK (Celery Worker)
        participant Redis as KLIK (Redis)
        participant DB as KLIK (PostgreSQL)
    end

    %% ETAP 1: Trigger
    Note over Beat, Worker: ETAP 1: Trigger naliczania prowizji (cron daily)
    Beat->>Beat: Harmonogram: codziennie o 23:55 UTC
    Beat->>Worker: Queue: accrue_p2p_lookup_fees(date=today)

    %% ETAP 2: Pobranie counter'ów z Redis
    Note over Worker, Redis: ETAP 2: Agregacja counter'ów lookup per bank
    Worker->>Redis: KEYS bank_lookups:*:{date}
    Redis-->>Worker: Lista kluczy<br/>[bank_lookups:bank-uuid-1:2026-04-26, ...]

    loop Dla każdego klucza
        Worker->>Redis: GET bank_lookups:{bank_id}:{date}
        Redis-->>Worker: count (np. 1234)
    end

    %% ETAP 3: Kalkulacja i zapis
    Note over Worker, DB: ETAP 3: Kalkulacja prowizji i zapis ledger entries
    loop Dla każdego banku z lookupami > 0
        Worker->>DB: SELECT Bank WHERE id=bank_id
        DB-->>Worker: Bank (z polem p2p_lookup_fee)
        Worker->>Worker: total_fee = count × p2p_lookup_fee
        Worker->>DB: INSERT LedgerEntry<br/>(from_bank=Bank, to_bank=KLIK,<br/>amount=total_fee, type=P2P_LOOKUP_FEE,<br/>settled=False, zone=Bank.zone)
        DB-->>Worker: ledger_entry_id
    end

    %% ETAP 4: Czyszczenie counter'ów
    Note over Worker, Redis: ETAP 4: Usunięcie przetworzonych counter'ów
    loop Dla każdego klucza
        Worker->>Redis: DEL bank_lookups:{bank_id}:{date}
        Redis-->>Worker: OK
    end

    Note over Worker, DB: ETAP 5: Entries trafiają do najbliższej sesji nettingowej<br/>(patrz C2B/WORKFLOW A5 — wspólny mechanizm settlement)

    %% Scenariusze błędne
    Note over Worker, Redis: Błędy:<br/>- Redis unreachable → task fail, retry przez Celery (3 próby)<br/>- Bank z lookupami nie istnieje (mało prawdopodobne) → log + pomiń<br/>- LedgerEntry insert fail → rollback całego batcha, retry<br/>- Counter == 0 dla banku → pomiń (nic do naliczenia)
```

---

## Stany Alias

Cykl życia aliasu w P2P jest minimalny — alias istnieje (jest aktywny)
albo nie istnieje. Brak stanów pośrednich, brak workflow.

```mermaid
---
config:
  theme: dark
---
stateDiagram-v2
    [*] --> REGISTERED: POST /aliases/register<br/>(P1)
    REGISTERED --> [*]: DELETE /aliases/{phone}<br/>(P3)

    note right of REGISTERED
        Alias aktywny w bazie.
        Może być znaleziony przez
        GET /aliases/lookup/{phone} (P2).
    end note
```

**Uwagi:**

1. **Brak soft-delete** w wersji 1.0 — DELETE faktycznie usuwa rekord. Powód:
   GDPR-friendly (nie trzymamy danych dłużej niż trzeba), prostota.
2. **Re-rejestracja** — po DELETE klient może ponownie zarejestrować ten sam
   numer w tym samym lub innym banku. To samo co świeża rejestracja (P1).
3. **Migracja banku** — jeśli klient zmienia bank, najpierw musi DELETE alias
   w starym banku, potem REGISTER w nowym. Brak atomic "transfer aliasu" w v1.0.
4. **Audit log** — fakt rejestracji/wyrejestrowania zostaje w `created_at` /
   logach systemu. Po DELETE rekord znika z głównej tabeli, ale operacja
   jest logowana w audit logu (TBD jako osobne zadanie).

---

## Tabela: różnice P2P vs C2B

Dla porównania ze szczegółową dokumentacją C2B:

| Aspekt | C2B (Kody) | P2P (Telefony) |
|---|---|---|
| **Czas życia obiektu** | Code: 120s w Redisie | Alias: trwały w Postgresie |
| **Stan obiektu** | Code: ACTIVE → USED → expired | Alias: REGISTERED → deleted |
| **Autoryzacja klienta** | Push do banku, PIN | Wewnątrz banku (poza KLIK) |
| **Webhook do banku** | Tak (`/authorize`) | Nie |
| **Celery task (online)** | Tak (autoryzacja async) | Nie (lookupy sync) |
| **Celery task (cron)** | Tak (sesje rozliczeniowe) | Tak (P4 — accrual prowizji) |
| **`/confirm`** | Tak | Nie |
| **Ledger entries (online)** | Tak (przy `/confirm`) | Nie (counter w Redisie) |
| **Ledger entries (cron)** | Nie (online wystarczy) | Tak (P4 — z agregacji counter'ów) |
| **Sesja rozliczeniowa** | Tak (wspólna z P2P) | Tak (wspólna z C2B) |
| **Prowizja KLIK** | % od kwoty (klik_fee) | Per lookup (p2p_lookup_fee × count) |
| **Złożoność modelu** | Transaction + Code + LedgerEntry + ... | Alias + counter w Redis |
