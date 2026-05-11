# Mock Banku — KLIK demo (C2B)

Symulator banku nadawcy uczestniczącego w ekosystemie KLIK. Domyka happy-path C2B
end-to-end bez ręcznego `curl`-a:

- operator klika **„Wygeneruj kod”** → mock woła KLIK `POST /codes/generate`
- agent (osobna apka, `agent/`, :5173) wpisuje kod i kwotę → KLIK `POST /payments/initiate`
- KLIK uderza w webhook tego mocka `POST /webhook/authorize` → autoryzacja ląduje w `/pending`
- operator klika **„Autoryzuj PIN-em”** (PIN `1234`) → mock woła KLIK `POST /payments/confirm`
  z `decision=ACCEPTED`, debetuje saldo klienta
- agent w 1–2 s widzi `✓ COMPLETED` z `merchant_net` / `fee`

W MVP wyłącznie moduł **C2B**. Stan **in-memory** — restart resetuje.

## Struktura

```
bank_mock/
├── backend/    FastAPI (:8100) — wzorzec: rtgs_mock/main.py
└── frontend/   Vue 3 + Vite + Tailwind (:5174) — wzorzec: agent/
```

## Endpointy backendu

| Metoda | Ścieżka | Opis |
|---|---|---|
| POST | `/webhook/authorize` | webhook od KLIK; zapisuje pending, zwraca `{received, will_prompt_user}` natychmiast |
| POST | `/webhook/ping` | proof-of-liveness (opcjonalny w specie) |
| GET | `/api/info` | dane do dashboardu (nazwa banku, strefa, czy API key skonfigurowany, liczba pending) |
| GET | `/api/clients` | lista klientów (`id`, `name`, `balance`) |
| POST | `/api/clients/{user_id}/generate-code` | woła KLIK `/codes/generate`, zwraca kod + TTL |
| GET | `/api/pending` | oczekujące autoryzacje (`seconds_left`, `client_balance`, `sufficient_balance`) |
| POST | `/api/pending/{tx}/accept` | body `{pin}`; sprawdza PIN, sprawdza saldo (za mało → auto-reject `INSUFFICIENT_FUNDS`), inaczej KLIK `/payments/confirm` `decision=ACCEPTED` + debet salda |
| POST | `/api/pending/{tx}/reject` | body `{reject_reason}`; KLIK `/payments/confirm` `decision=REJECTED` |
| GET | `/api/history` | audit log (`CODE_GENERATED` / `WEBHOOK_RECEIVED` / `AUTHORIZED` / `REJECTED` / `EXPIRED`) |
| GET | `/healthz` | liveness |

## Konfiguracja (env backendu)

| Zmienna | Default | Opis |
|---|---|---|
| `KLIK_BASE_URL` | `http://localhost:8000/api/v1` | API KLIK; w compose: `http://web:8000/api/v1` |
| `KLIK_BANK_API_KEY` | — | klucz banku z Django Admin (**wymagany**) |
| `BANK_NAME` | `BANK_MOCK` | kosmetyka UI |
| `BANK_ZONE` | `PL` | strefa wysyłana do `/codes/generate` |
| `KLIK_CODE_TTL_SECONDS` | `120` | TTL kodu (zgodne z KLIK) |
| `KLIK_HTTP_TIMEOUT` | `10` | timeout HTTP do KLIK (s) |

Nagłówki wysyłane do KLIK: `X-KLIK-Bank-Api-Key`, `Content-Type: application/json`,
`Idempotency-Key` (UUID v4 per request).

## Setup developerski

1. W **Django Admin** KLIK utwórz `Bank`:
   - `name = BANK_MOCK`, `zone = PL`, `c2b_enabled = True`, `active = True`
   - `webhook_url`:
     - docker-compose: `http://bank-mock-backend:8100/webhook`
     - bez dockera: `http://localhost:8100/webhook`
     > KLIK uderza w `{webhook_url}/authorize`, więc ścieżka kończy się na `/webhook`.
   - skopiuj wygenerowany `api_key`.
2. Wpisz `api_key` do env:
   - docker-compose: `BANK_MOCK_KLIK_BANK_API_KEY=...` w głównym `.env`
   - lokalnie: `KLIK_BANK_API_KEY=...` w `bank_mock/backend/.env` (skopiuj z `.env.example`)
3. Uruchom mock — jedna z opcji:

### A) docker-compose (zintegrowane)
```bash
docker compose -f docker-compose.yml -f docker-compose-dev.yml up bank-mock-backend bank-mock-frontend
# backend: http://localhost:8100   frontend: http://localhost:5174
```
(Albo po prostu `docker compose -f docker-compose.yml -f docker-compose-dev.yml up` żeby wstać wszystko: KLIK + worker + bank mock + rtgs mock.)

### B) lokalnie, bez dockera
```bash
# backend
cd bank_mock/backend
cp .env.example .env        # uzupełnij KLIK_BANK_API_KEY
pip install -r requirements.txt
set -a; source .env; set +a
uvicorn main:app --host 0.0.0.0 --port 8100 --reload

# frontend (drugi terminal)
cd bank_mock/frontend
npm install
npm run dev                 # http://localhost:5174
```

4. Wejdź na `http://localhost:5174`, w ekranie konfiguracji zostaw `http://localhost:8100`.

## Happy path (smoke test)

1. `/clients` → „Wygeneruj kod KLIK” dla **Jan Kowalski** (`user-1`) → modal z 6-cyfrowym kodem + countdown 120 s.
2. Terminal agenta `http://localhost:5173` → wpisz kod, kwotę (np. `150.00`), wybierz merchanta, „Zapłać”.
3. W mocku `/pending` (auto-refresh co 2 s) pojawia się kafelek **Jan Kowalski — <merchant> 150.00 PLN**.
4. „Autoryzuj PIN-em” → `1234` → „Autoryzuj”.
5. W terminalu agenta w 1–2 s: `✓ COMPLETED` z `merchant_net` / `klik_fee` / `agent_fee`.
6. W mocku `/history`: wpisy `CODE_GENERATED`, `WEBHOOK_RECEIVED`, `AUTHORIZED`. Saldo Jana zmniejszone o 150.

## Edge case'y

- **REJECTED z powodem** — w `/pending` → „Odrzuć ▾” → wybierz reason. Agent widzi `✗` z powodem.
- **Wygaśnięcie autoryzacji** — nie klikaj nic przez 120 s; kafelek znika, toast „Autoryzacja wygasła…”, w `/history` wpis `EXPIRED`.
- **Brak środków** — wygeneruj kod dla **Piotr Wiśniewski** (`user-3`, saldo 80 PLN), zapłać 150 PLN; w `/pending` kafelek na czerwono, „Autoryzuj PIN-em” od razu odrzuca `INSUFFICIENT_FUNDS` (bez pytania o PIN), agent widzi `✗ INSUFFICIENT_FUNDS`.

## Znane uproszczenia / quirki

- **Korelacja webhook ↔ klient**: webhook od KLIK (`backend/codes/tasks.py`) nie zawiera
  `user_id` ani kodu. Mock przypisuje webhook do najstarszego niewygasłego kodu z własnej
  kolejki FIFO. Dla demo (jeden przepływ naraz) działa deterministycznie; przy
  równoległych przepływach kolejność może się rozjechać. Jeśli KLIK zacznie wysyłać
  `user_id` w payloadzie webhooka — mock automatycznie go użyje.
- `/payments/confirm` w realnym KLIK przyjmuje pole **`decision`** (`ACCEPTED`/`REJECTED`),
  nie `status` jak w `INFO.md` — mock wysyła `decision`.
- Stan w pamięci; brak persistencji, autentykacji operatora, multi-bank — zgodnie z zakresem MVP.