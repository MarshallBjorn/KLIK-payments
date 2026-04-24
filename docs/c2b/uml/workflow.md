# Diagramy C2B — Etapy A0–A3

Diagramy sekwencji dla modułu płatności kodem (C2B) w systemie KLIK.

---

## A0 — Onboarding banku

Proces rejestracji nowego banku w ekosystemie KLIK. Zawiera kroki wykonywane
poza systemem (umowa, email) oraz techniczną weryfikację połączenia.

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
    participant API as KLIK (Django API)
    participant DB as KLIK (PostgreSQL)
    participant BankAPI as Bank (API + Webhook Endpoint)

    %% ETAP 0: Off-system
    Note over BankOps, KlikOps: ETAP 0: Uzgodnienia poza systemem (umowa, strefy, waluta)
    BankOps->>KlikOps: Zgłoszenie chęci integracji (email/umowa)
    KlikOps->>BankOps: Due diligence, ustalenie stref i limitów

    %% ETAP 1: Rejestracja rekordu banku
    Note over KlikOps, DB: ETAP 1: Utworzenie rekordu banku w systemie
    KlikOps->>Admin: Utworzenie rekordu Bank (name, zones, currency, limits)
    Admin->>DB: INSERT Bank (active=False)
    Admin->>DB: Wygeneruj i zahashuj api_key
    DB-->>Admin: OK (bank_id + plaintext api_key jednorazowo)
    Admin-->>KlikOps: Wyświetl api_key (do przekazania bankowi)

    %% ETAP 2: Przekazanie credentials
    Note over KlikOps, BankOps: ETAP 2: Przekazanie credentials bezpiecznym kanałem
    KlikOps->>BankOps: api_key + adres KLIK API (szyfrowany kanał)
    BankOps->>BankAPI: Konfiguracja KLIK_API_KEY + adres

    %% ETAP 3: Rejestracja webhooka
    Note over BankAPI, DB: ETAP 3: Rejestracja adresu webhooka
    BankAPI->>API: POST /banks/webhook-config (url, X-KLIK-Api-Key)
    API->>DB: Zapisz webhook_url dla banku
    DB-->>API: OK
    API-->>BankAPI: HTTP 200 (webhook zarejestrowany)

    %% ETAP 4: Weryfikacja połączenia
    Note over API, BankAPI: ETAP 4: Ping weryfikacyjny webhooka
    API->>BankAPI: POST {webhook_url}/ping (signed payload)
    BankAPI-->>API: HTTP 200 OK (pong)
    API->>DB: Ustaw active=True dla banku
    DB-->>API: OK

    %% ETAP 5: Bank produkcyjny
    Note over BankAPI, API: ETAP 5: Bank aktywny — może wywoływać endpointy
    BankAPI->>API: POST /codes/generate (produkcyjne wywołanie)
    API-->>BankAPI: HTTP 200 (bank zautoryzowany i aktywny)
```

---

## A1 — Generowanie kodu

Klient prosi swój bank o wygenerowanie kodu KLIK. Bank prosi KLIK,
KLIK zapisuje kod w Redis z TTL=120s.

```mermaid
---
config:
  theme: dark
---
sequenceDiagram
    autonumber
    actor Klient
    participant BankN as Bank Nadawcy
    box System (KLIK)
        participant API as KLIK (Django API)
        participant Redis as KLIK (Redis)
    end

    %% ETAP 1: Żądanie kodu
    Note over Klient, BankN: ETAP 1: Klient inicjuje generowanie kodu
    Klient->>BankN: Klika "Generuj KLIK" w aplikacji
    BankN->>BankN: Weryfikacja sesji klienta, aktywności konta

    %% ETAP 2: Wywołanie KLIK
    Note over BankN, Redis: ETAP 2: Bank prosi KLIK o wygenerowanie kodu
    BankN->>API: POST /codes/generate (X-KLIK-Api-Key, user_id, zone)
    API->>API: Uwierzytelnienie, wybór strefy
    API->>API: Wylosuj 6-cyfrowy kod (z retry na kolizję)
    API->>Redis: SET code:{kod} {bank_id, user_id, zone, status=ACTIVE} EX 120
    Redis-->>API: OK

    %% ETAP 3: Zwrot do klienta
    Note over API, Klient: ETAP 3: Wyświetlenie kodu klientowi
    API-->>BankN: HTTP 200 {code: "123456", expires_in: 120}
    BankN-->>Klient: Wyświetla kod 123456 z odliczaniem czasu

    %% Scenariusze błędne (opis w dokumencie)
    Note over API, Redis: Błędy: bank nieaktywny (403), <br/>Redis unreachable (503), <br/>kolizja kodu po N próbach (500)
```

---

## A2 — Inicjacja transakcji przez agenta

Klient wpisuje kod w terminalu/sklepie. Agent wysyła kod do KLIK,
KLIK tworzy transakcję i zleca asynchroniczne wywołanie webhooka
do banku nadawcy.

```mermaid
---
config:
  theme: dark
---
sequenceDiagram
    autonumber
    actor Klient
    participant Agent as Agent (Sklep/Bramka)
    box System (KLIK)
        participant API as KLIK (Django API)
        participant Redis as KLIK (Redis)
        participant DB as KLIK (PostgreSQL)
        participant Worker as KLIK (Celery Worker)
    end

    %% ETAP 1: Wpisanie kodu
    Note over Klient, Agent: ETAP 1: Klient wpisuje kod w punkcie sprzedaży
    Klient->>Agent: Wpisuje kod 123456
    Agent->>Agent: Klient akceptuje kwotę

    %% ETAP 2: Wywołanie /initiate
    Note over Agent, API: ETAP 2: Agent wysyła żądanie do KLIK
    Agent->>API: POST /payments/initiate<br/>(code, amount, currency, agent_bank_id,<br/>merchant_name, merchant_iban, idempotency_key)
    API->>API: Uwierzytelnienie agenta (X-KLIK-Api-Key)
    API->>API: Weryfikacja idempotency_key

    %% ETAP 3: Walidacja kodu
    Note over API, Redis: ETAP 3: Walidacja kodu w Redis
    API->>Redis: GET code:123456
    Redis-->>API: {bank_id, user_id, zone, status=ACTIVE}
    API->>API: Weryfikacja strefy (zone kodu == zone agenta)
    API->>API: Pobierz MSCAgreement dla agenta z DB (cached)
    API->>Redis: UPDATE status=USED (atomic, żeby uniknąć race)
    Redis-->>API: OK

    %% ETAP 4: Utworzenie transakcji
    Note over API, DB: ETAP 4: Utworzenie rekordu transakcji
    API->>DB: INSERT Transaction (status=PENDING, is_on_us=?, kwoty, fees)
    DB-->>API: transaction_id (UUID)
    API->>Redis: SET tx:{id} {status: PENDING} EX 900 (hot cache statusu)

    %% ETAP 5: Zlecenie webhooka
    Note over API, Worker: ETAP 5: Asynchroniczne zlecenie autoryzacji
    API->>Worker: Queue: authorize_webhook(transaction_id)
    API-->>Agent: HTTP 202 Accepted {transaction_id}
    Agent->>Agent: Start polling GET /payments/status/{tx_id}
    Agent-->>Klient: "Autoryzuj transakcję w aplikacji banku"

    %% Scenariusze błędne
    Note over API, Redis: Błędy: kod nie istnieje/wygasł (404_CODE_EXPIRED),<br/>kod już użyty (409), cross-zone (422_ZONE_MISMATCH),<br/>agent nieaktywny (403), duplikat idempotency_key (zwróć istniejący tx)
```

---

## A3 — Autoryzacja przez bank nadawcy

Celery Worker uderza do webhooka banku nadawcy. Bank pokazuje klientowi
push z prośbą o autoryzację. Klient akceptuje PINem.

```mermaid
---
config:
  theme: dark
---
sequenceDiagram
    autonumber
    actor Klient
    participant BankN as Bank Nadawcy
    box System (KLIK)
        participant Worker as KLIK (Celery Worker)
        participant DB as KLIK (PostgreSQL)
        participant Redis as KLIK (Redis)
    end

    %% ETAP 1: Wysłanie webhooka
    Note over Worker, BankN: ETAP 1: KLIK wysyła webhook autoryzacyjny
    Worker->>DB: Pobierz Transaction by id
    DB-->>Worker: Dane transakcji
    Worker->>BankN: POST {bank.webhook_url}/authorize<br/>(transaction_id, user_id, amount, currency,<br/>merchant_name, expiry_time, is_on_us, signature)
    BankN->>BankN: Weryfikacja sygnatury KLIK

    %% ETAP 2: Interakcja z klientem
    Note over BankN, Klient: ETAP 2: Autoryzacja w aplikacji banku
    BankN->>Klient: Push notification: "Płacisz 150 PLN w Sklep Żabka. Akceptujesz?"
    Klient->>BankN: Wpisuje PIN i klika "Akceptuj"
    BankN->>BankN: Weryfikacja PIN, sprawdzenie salda
    BankN->>BankN: Blokada środków na koncie klienta

    %% ETAP 3: Odpowiedź na webhook
    Note over BankN, Worker: ETAP 3: Bank zwraca wynik autoryzacji
    BankN-->>Worker: HTTP 200 OK {authorized: true}
    Worker->>DB: UPDATE Transaction SET status=AUTHORIZED
    Worker->>Redis: UPDATE tx:{id} status=AUTHORIZED

    %% ETAP 4: Osobne wywołanie /confirm przez bank (asynchroniczne)
    Note over BankN, Worker: ETAP 4: Bank potwierdza transakcję przez /confirm<br/>(flow opisany w A4 — tu tylko dla kontekstu)
    BankN->>Worker: (async) POST /payments/confirm (patrz A4)

    %% Scenariusze błędne
    Note over Worker, BankN: Błędy:<br/>- Bank timeout → Celery retry (3 próby, exp. backoff max 30s)<br/>- Klient nie autoryzował do expiry_time → bank zwraca TIMEOUT<br/>- Niewystarczające środki → HTTP 200 {authorized: false, reason: INSUFFICIENT_FUNDS}<br/>- PIN błędny → bank sam retryuje u klienta, dopiero po failu zwraca REJECTED<br/>- Kod wygasł w Redisie zanim klient zaakceptował → bank zwraca 200, ale /confirm odrzucony w A4
```

---

## Tabela kontekstowa: pola `is_on_us` i `zone`

| Pole | Typ | Kiedy ustawiane | Źródło prawdy |
|------|-----|-----------------|---------------|
| `is_on_us` | bool | Przy tworzeniu Transaction (A2 krok 4) | `bank_id` kodu == `agent_bank_id` |
| `zone` | enum (PL/UK/US/EU) | Przy generowaniu kodu (A1 krok 2) | Strefa banku nadawcy |
| Walidacja strefy | — | A2 krok 3 | `code.zone == agent.zone` lub 422_ZONE_MISMATCH |

---

## A4 — Confirm, kalkulacja splitu i zapis do ledgera

Bank nadawcy (po autoryzacji z A3) wywołuje `/payments/confirm`. KLIK
kalkuluje podział prowizji, zapisuje ledger entries w zależności od
`is_on_us`, i aktualizuje status transakcji.

Happy path (ACCEPTED) i rejected path (REJECTED) pokazane w jednym
diagramie przez blok `alt`.

```mermaid
---
config:
  theme: dark
---
sequenceDiagram
    autonumber
    participant BankN as Bank Nadawcy
    box System (KLIK)
        participant API as KLIK (Django API)
        participant DB as KLIK (PostgreSQL)
        participant Redis as KLIK (Redis)
    end
    participant Agent as Agent (Sklep/Bramka)

    %% ETAP 1: Confirm
    Note over BankN, API: ETAP 1: Bank potwierdza wynik autoryzacji
    BankN->>API: POST /payments/confirm<br/>(transaction_id, status, X-KLIK-Api-Key)
    API->>API: Uwierzytelnienie banku

    %% ETAP 2: Idempotency
    Note over API, DB: ETAP 2: Sprawdzenie idempotency
    API->>DB: SELECT Transaction WHERE id=tx_id
    DB-->>API: Transaction (status=AUTHORIZED lub COMPLETED/REJECTED)
    alt Transakcja już rozstrzygnięta (COMPLETED/REJECTED)
        API-->>BankN: HTTP 200 OK (idempotent replay, bez akcji)
    else Transakcja w stanie AUTHORIZED — procesujemy
        API->>API: Kontynuuj przetwarzanie

        %% ETAP 3: Rozgałęzienie happy vs rejected
        Note over API, DB: ETAP 3: Przetworzenie confirm w zależności od statusu
        alt status == ACCEPTED (happy path)
            %% Kalkulacja splitu
            Note over API, DB: ETAP 3a: Kalkulacja podziału prowizji
            API->>DB: SELECT MSCAgreement dla agenta
            DB-->>API: agent_fee_perc, klik_fee_perc
            API->>API: brutto = 150.00<br/>klik_fee = brutto * klik_perc<br/>agent_fee = brutto * agent_perc<br/>merchant_net = brutto - klik_fee - agent_fee

            %% Zapis ledger entries
            Note over API, DB: ETAP 3b: Zapis ledger entries (settled=False)
            alt is_on_us == False (transakcja międzybankowa)
                API->>DB: INSERT LedgerEntry<br/>(Bank_Nadawcy → Bank_Merchanta, merchant_net)
                API->>DB: INSERT LedgerEntry<br/>(Bank_Nadawcy → Bank_Agenta, agent_fee)
                API->>DB: INSERT LedgerEntry<br/>(Bank_Nadawcy → KLIK, klik_fee)
            else is_on_us == True (transakcja wewnętrzna)
                Note over API, DB: Merchant i nadawca w tym samym banku — <br/>przeniesienie 148.05 wewnątrz banku poza KLIK.<br/>KLIK rejestruje tylko prowizje.
                API->>DB: INSERT LedgerEntry<br/>(Bank_Nadawcy → Bank_Agenta, agent_fee)
                API->>DB: INSERT LedgerEntry<br/>(Bank_Nadawcy → KLIK, klik_fee)
            end
            DB-->>API: OK

            %% Update statusu
            Note over API, Redis: ETAP 3c: Aktualizacja statusu transakcji
            API->>DB: UPDATE Transaction SET status=COMPLETED
            API->>Redis: SET tx:{id} status=COMPLETED

        else status == REJECTED (bank odrzucił)
            Note over API, DB: ETAP 3a': Odrzucenie — brak ledger entries
            API->>DB: UPDATE Transaction SET status=REJECTED, reason=...
            API->>Redis: SET tx:{id} status=REJECTED
        end

        API-->>BankN: HTTP 200 OK
    end

    %% ETAP 4: Polling agenta
    Note over Agent, Redis: ETAP 4: Agent otrzymuje finalny status przez polling
    Agent->>API: GET /payments/status/{tx_id}
    API->>Redis: GET tx:{id}
    Redis-->>API: {status: COMPLETED lub REJECTED}
    API-->>Agent: HTTP 200 {status}
    Agent-->>Agent: Wyświetl wynik klientowi

    %% Scenariusze błędne
    Note over API, DB: Błędy:<br/>- transaction_id nie istnieje → 404<br/>- transaction w stanie PENDING (przed AUTHORIZED) → 409 CONFLICT<br/>- confirm przychodzi po expiry_time → 404 CODE_EXPIRED (transakcja → TIMEOUT)<br/>- nieznany bank wywołuje confirm cudzej transakcji → 403
```

---

## Tabela: Split prowizji — przykład liczbowy

| Pole | Wartość | Komentarz |
|------|---------|-----------|
| Kwota brutto (co płaci klient) | 150.00 PLN | Wpisane przez agenta w `/initiate` |
| `klik_fee_perc` | 0.3% | Z MSCAgreement (globalny lub per agent) |
| `agent_fee_perc` | 1.0% | Z MSCAgreement per agent |
| KLIK fee | 0.45 PLN | 150 * 0.003 |
| Agent fee | 1.50 PLN | 150 * 0.010 |
| Merchant netto | 148.05 PLN | 150 - 0.45 - 1.50 |

---

## Tabela: Ledger entries per scenariusz

**Założenia:** Brutto 150 PLN. KLIK fee 0.45, Agent fee 1.50, Merchant 148.05.

### Scenariusz OFF-US (Bank A nadawcy ≠ Bank B merchanta, Agent w Banku C)

| From | To | Amount | Cel |
|------|-----|--------|-----|
| Bank A | Bank B | 148.05 | Płatność do merchanta |
| Bank A | Bank C | 1.50 | Prowizja agenta |
| Bank A | KLIK | 0.45 | Prowizja KLIK |

### Scenariusz ON-US (Bank A nadawcy = Bank B merchanta, Agent w Banku C)

| From | To | Amount | Cel |
|------|-----|--------|-----|
| Bank A | Bank C | 1.50 | Prowizja agenta |
| Bank A | KLIK | 0.45 | Prowizja KLIK |

*(Przeniesienie 148.05 wewnątrz Banku A — poza KLIK, sprawa banku)*

---

## A5 — Sesja nettingowa i settlement przez RTGS

Celery Beat triggeruje koniec sesji per strefa. KLIK agreguje niesetlowane
ledger entries, robi multilateralny netting, dopasowuje dłużników do
wierzycieli (greedy matching), i wysyła instrukcje settlementu przez
odpowiedni RTGS gateway.

Częściowy commit: nieudane instrukcje lądują w kolejce do następnej sesji.

```mermaid
---
config:
  theme: dark
---
sequenceDiagram
    autonumber
    box System (KLIK)
        participant Beat as KLIK (Celery Beat)
        participant Worker as KLIK (Celery Worker)
        participant DB as KLIK (PostgreSQL)
        participant Dispatch as KLIK (RTGS Dispatcher)
    end
    participant RTGS as RTGS (SORBNET3/TARGET2/CHAPS/FedNow)
    participant Banki as Banki (email/webhook raport)

    %% ETAP 1: Trigger sesji
    Note over Beat, Worker: ETAP 1: Koniec sesji (per strefa, konfigurowalny interval)
    Beat->>Worker: Task: run_settlement_session(zone=PL)

    %% ETAP 2: Utworzenie sesji
    Note over Worker, DB: ETAP 2: Utworzenie rekordu sesji
    Worker->>DB: INSERT SettlementSession (zone, started_at, status=PROCESSING)
    DB-->>Worker: session_id

    %% ETAP 3: Agregacja
    Note over Worker, DB: ETAP 3: Pobranie niesetlowanych ledger entries
    Worker->>DB: SELECT LedgerEntry WHERE settled=False AND zone=PL<br/>(również entries z poprzednich nieudanych sesji)
    DB-->>Worker: Lista ledger_entries

    %% ETAP 4: Multilateralny netting
    Note over Worker: ETAP 4: Multilateralny netting
    Worker->>Worker: Dla każdego uczestnika (bank/KLIK/agenci):<br/>pozycja_netto = SUM(kredyty) - SUM(debety)
    Worker->>Worker: Podział na dłużników (netto < 0) i wierzycieli (netto > 0)

    %% ETAP 5: Matching dłużników z wierzycielami
    Note over Worker: ETAP 5: Greedy matching — minimalizacja liczby przelewów
    Worker->>Worker: Algorytm: największy dłużnik płaci największemu wierzycielowi,<br/>aż któryś się wyzeruje. Powtarzaj.
    Worker->>Worker: Wynik: lista SettlementInstruction (from_bank, to_bank, amount)

    %% ETAP 6: Zapis instrukcji
    Note over Worker, DB: ETAP 6: Persist instrukcji przed dispatch (dla audytu)
    Worker->>DB: INSERT SettlementInstruction[] (session_id, status=PENDING)
    DB-->>Worker: OK

    %% ETAP 7: Wybór gateway i dispatch
    Note over Worker, Dispatch: ETAP 7: Wybór gateway RTGS po strefie
    Worker->>Dispatch: dispatch(zone=PL, instructions)
    Dispatch->>Dispatch: Factory: zone=PL → SORBNET3Gateway

    %% ETAP 8: Wysłanie instrukcji (każda osobno, bo częściowy commit)
    Note over Dispatch, RTGS: ETAP 8: Wysłanie instrukcji pojedynczo (częściowy commit)
    loop Dla każdej SettlementInstruction
        Dispatch->>RTGS: POST /settle (from_bank, to_bank, amount, instruction_id)
        alt RTGS zwraca 200 OK
            RTGS-->>Dispatch: HTTP 200 {settled: true, rtgs_ref}
            Dispatch->>DB: UPDATE SettlementInstruction SET status=SETTLED
            Dispatch->>DB: UPDATE LedgerEntry[] powiązane z instrukcją SET settled=True
        else RTGS zwraca błąd (bank niewypłacalny, timeout)
            RTGS-->>Dispatch: HTTP 4xx/5xx {error}
            Dispatch->>DB: UPDATE SettlementInstruction SET status=FAILED, reason
            Note over Dispatch, DB: Ledger entries pozostają settled=False —<br/>zostaną doklejone do następnej sesji
        end
    end

    %% ETAP 9: Finalizacja sesji
    Note over Worker, DB: ETAP 9: Zamknięcie sesji
    Worker->>DB: UPDATE SettlementSession<br/>SET status=CLOSED, ended_at, stats (total_settled, total_failed)

    %% ETAP 10: Raport dla banków (opcjonalnie)
    Note over Worker, Banki: ETAP 10: Wysłanie raportu sesji do banków
    Worker->>Banki: POST {bank.report_webhook_url}/settlement-report<br/>(session_id, zone, twoje_zobowiązania, twoje_należności, settled/failed)
    Banki-->>Worker: HTTP 200

    %% Scenariusze błędne
    Note over Worker, RTGS: Błędy:<br/>- RTGS całkowicie niedostępny → sesja CLOSED z total_failed=100%,<br/>wszystkie entries zostają do następnej sesji, alert dla operatora<br/>- Worker crashuje w trakcie → SettlementSession pozostaje PROCESSING,<br/>recovery task sprząta zawieszone sesje<br/>- Raport dla banku timeout → retry 3x, potem do DLQ dla operatora
```

---

## Przykład: Multilateralny netting z greedy matching

**Setup:** Sesja PL, 4 uczestników: Bank A, Bank B, Bank C, KLIK.

**Ledger entries w sesji:**
```
Bank A → Bank B:   500 PLN (transakcja off-us)
Bank A → Bank C:    50 PLN (agent fee)
Bank A → KLIK:      15 PLN (klik fee)
Bank B → Bank A:   200 PLN (inna transakcja off-us w drugą stronę)
Bank B → KLIK:      10 PLN (klik fee)
Bank C → Bank A:   100 PLN (off-us, Bank C klient, Bank A merchant)
Bank C → KLIK:      5 PLN
```

**Krok 1 — Pozycje netto per uczestnik:**

| Uczestnik | Kredyty (otrzyma) | Debety (zapłaci) | Netto |
|-----------|-------------------|------------------|-------|
| Bank A | 200 + 100 = 300 | 500 + 50 + 15 = 565 | **-265** (dłużnik) |
| Bank B | 500 | 200 + 10 = 210 | **+290** (wierzyciel) |
| Bank C | 50 | 100 + 5 = 105 | **-55** (dłużnik) |
| KLIK | 15 + 10 + 5 = 30 | 0 | **+30** (wierzyciel) |

**Sanity check:** suma netto = 0 ✓

**Krok 2 — Greedy matching:**

Sortuj dłużników malejąco: [Bank A -265, Bank C -55]
Sortuj wierzycieli malejąco: [Bank B +290, KLIK +30]

- Bank A płaci Bank B min(265, 290) = 265 → Bank A: 0, Bank B: +25
- Bank C płaci Bank B min(55, 25) = 25 → Bank C: -30, Bank B: 0
- Bank C płaci KLIK min(30, 30) = 30 → Bank C: 0, KLIK: 0 ✓

**Rezultat — 3 przelewy RTGS zamiast 7 ledger entries:**

| From | To | Amount |
|------|-----|--------|
| Bank A | Bank B | 265 |
| Bank C | Bank B | 25 |
| Bank C | KLIK | 30 |

---
