# KLIK

> Centralny operator i router płatności mobilnych w ekosystemie bankowym — odpowiednik BLIK dla wielu stref walutowych.

**Projekt akademicki:** Aplikacje Biznesowe.

---

## Spis treści

- [KLIK](#klik)
  - [Spis treści](#spis-treści)
  - [O projekcie](#o-projekcie)
  - [Zakres](#zakres)
    - [W zakresie projektu](#w-zakresie-projektu)
    - [Poza zakresem](#poza-zakresem)
  - [Stack technologiczny](#stack-technologiczny)
  - [Struktura repozytorium](#struktura-repozytorium)
  - [Dokumentacja](#dokumentacja)
    - [Moduł C2B (Kody)](#moduł-c2b-kody)
    - [Moduł P2P (Telefony)](#moduł-p2p-telefony)
    - [Moduł Cheques (Czeki)](#moduł-cheques-czeki)
    - [Moduł Recurring (Regularne transfery)](#moduł-recurring-regularne-transfery)
    - [Testowanie integracji](#testowanie-integracji)
    - [Deployment](#deployment)
  - [Development workflow](#development-workflow)
    - [Pierwszy setup po klonowaniu repo](#pierwszy-setup-po-klonowaniu-repo)
    - [Codzienna praca](#codzienna-praca)
    - [CI](#ci)
  - [Status projektu](#status-projektu)
  - [Autorzy](#autorzy)

---

## O projekcie

KLIK pełni rolę centralnego operatora i routera płatności mobilnych. Zapewnia cztery moduły:

1. **KLIK Kody (C2B)** — autoryzacja płatności w punktach sprzedaży za pomocą 6-cyfrowych kodów generowanych na żądanie banku.
2. **KLIK Telefon (P2P)** — rejestr aliasów mapujący numery telefonów na dane bankowe, umożliwiający przelewy na numer telefonu.
3. **KLIK Czeki (Cheques)** — 9-cyfrowe kody o ustalonej kwocie i długim TTL (1h–72h), realizowane jednorazowo przez agenta. Hold środków po stronie banku wystawcy.
4. **KLIK Regularne transfery (Recurring)** — zlecenia stałe oparte na aliasie P2P (DAILY / WEEKLY / MONTHLY). KLIK orkiestruje czas wykonania, bank wystawcy realizuje przelew RTP.

KLIK działa jako niezależny mikroserwis (orkiestrator). **Nie przechowuje środków pieniężnych** — zarządza logiką autoryzacji (Kody) oraz routingiem danych (Telefony). Rozliczenia międzybankowe realizowane są w sesjach nettingowych przez systemy RTGS (SORBNET3 / TARGET2 / CHAPS / FedNow).

System obsługuje cztery strefy walutowo-krajowe (PL, EU, UK, US) z rygorystyczną izolacją strefową — transakcje cross-zone są odrzucane.

> [!IMPORTANT]
> **Zakres implementacji**<br>
> Szanowne zespoły bankowe. W ramach tego przedmiotu, z systemu KLIK musicie zaintegrować następujące moduły: C2B (płatnośc kodem) oraz P2P (płatność na telefon).<br>
> Problemom dotyczącym integracji NIEOBOWIĄZKOWYCH modułów będzie nadawany najniższy priorytet.

## Zakres

### W zakresie projektu

- **KLIK Kody (C2B)** — generowanie kodów, autoryzacja, split prowizji, netting, dispatch do RTGS
- **KLIK Telefon (P2P)** — rejestracja aliasów, lookup, daily fee accrual
- **KLIK Czeki (Cheques)** — issue / redeem / cancel, expire cron, integracja z Transaction i ledgerem C2B
- **KLIK Regularne transfery (Recurring)** — mandate, dispatch cron, webhook /execute do banku, auto-pause
- **Agent rozliczeniowy (Vue)** — symulowany terminal/bramka płatnicza
- **Dispatcher RTGS** — 4 strategie dla 4 systemów bankowości centralnej
- **Panel operatora** — Django Admin

### Poza zakresem

- **Aplikacje banków** — mockowane minimalnie, tylko do zamknięcia flow integracyjnego
- **Systemy RTP dla P2P** (Elixir Express / Faster Payments / SEPA Instant / FedNow RTP) — zakłada się że banki realizują je poza KLIK
- **Pełna implementacja AML, chargeback, dokumenty SWIFT** — wytyczne przedmiotu dla innych zakresów projektowych

## Stack technologiczny

- **Backend:** Django 5 + Django REST Framework
- **Baza aliasów i ledgera:** PostgreSQL
- **Baza kodów (krótkotrwała):** Redis (TTL 120s)
- **Zadania asynchroniczne:** Celery + Redis broker
- **Scheduler:** Celery Beat (sesje rozliczeniowe)
- **Frontend agenta:** Vue
- **Konteneryzacja:** Docker + Docker Compose
- **Panel operatora:** Django Admin

## Struktura repozytorium

```
klik_proj/
├── backend/              # Aplikacja Django (KLIK)
├── agent/                # Frontend Vue — terminal agenta (:5175, w Dockerze)
├── bank_IO/              # Mock banku (backend FastAPI :8100 + frontend Vue :5174)
├── rtgs_mock/            # Mock RTGS (4 systemy: SORBNET3 / TARGET2 / CHAPS / FedNow)
├── docs/                 # Cała dokumentacja projektu
│   ├── c2b/              # Moduł Kody (C2B)
│   │   ├── bpmn/         # Diagramy BPMN + eksporty PNG
│   │   ├── diagrams/     # Diagramy Mermaid (stany, sekwencje, ERD)
│   │   └── integration/  # Dokumentacja integracyjna dla banków
│   ├── p2p/              # Moduł Telefony (P2P)
│   │   ├── diagrams/
│   │   └── integration/
│   ├── cheques/          # Moduł Czeki (Cheques)
│   │   ├── diagrams/
│   │   └── integration/
│   └── recurring/        # Moduł Regularne transfery (Recurring)
│       ├── diagrams/
│       └── integration/
├── klik_proj/
│   ├── docker-compose.yml
│   ├── docker-compose-dev.yml
│   ├── docker-compose-prod.yml
│   └── .env.example
└── README.md
```

## Dokumentacja

Szczegółowa dokumentacja podzielona jest tematycznie. README zawiera tylko podstawowe informacje — po szczegóły zajrzyj do odpowiednich plików.

### Moduł C2B (Kody)

| Dokument | Zawartość |
|---|---|
| [docs/c2b/integration/INFO.md](./docs/c2b/integration/INFO.md) | **Dokumentacja integracyjna dla banków** — słownik domenowy, API reference, error codes, webhooki, onboarding |
| [docs/c2b/diagrams/WORKFLOW.md](./docs/c2b/diagrams/WORKFLOW.md) | Diagramy sekwencji (A0–A5) — pełny cykl życia płatności |
| [docs/c2b/diagrams/STATE.md](./docs/c2b/diagrams/STATE.md) | Diagramy stanów (Code, Transaction, LedgerEntry) oraz ERD i dispatcher RTGS |
| [docs/c2b/bpmn/](./docs/c2b/bpmn/) | Diagramy BPMN procesu biznesowego (main + 4 subprocess'y) |

### Moduł P2P (Telefony)

| Dokument | Zawartość |
|---|---|
| [docs/p2p/integration/INFO.md](./docs/p2p/integration/INFO.md) | Dokumentacja integracyjna dla banków — pricing model, account identifier per strefa, API reference |
| [docs/p2p/diagrams/WORKFLOW.md](./docs/p2p/diagrams/WORKFLOW.md) | Diagramy sekwencji (P0–P4) + stany Alias |

### Moduł Cheques (Czeki)

| Dokument | Zawartość |
|---|---|
| [docs/cheques/integration/INFO.md](./docs/cheques/integration/INFO.md) | API reference, model rozliczeniowy (HOLD po stronie banku), TTL config, error codes, webhooki end-of-life |
| [docs/cheques/diagrams/WORKFLOW.md](./docs/cheques/diagrams/WORKFLOW.md) | Diagramy sekwencji (CH0–CH4) — issue, redeem, cancel, expire, settlement |
| [docs/cheques/diagrams/STATE.md](./docs/cheques/diagrams/STATE.md) | Stany Cheque + Transaction (cheque-redemption) + ERD update |

### Moduł Recurring (Regularne transfery)

| Dokument | Zawartość |
|---|---|
| [docs/recurring/integration/INFO.md](./docs/recurring/integration/INFO.md) | API reference, schedule i cykle, failure handling, auto-pause, webhooki banku |
| [docs/recurring/diagrams/WORKFLOW.md](./docs/recurring/diagrams/WORKFLOW.md) | Diagramy sekwencji (R0–R6) — create, execution, pause/resume, cancel, auto-pause, end_date |
| [docs/recurring/diagrams/STATE.md](./docs/recurring/diagrams/STATE.md) | Stany RecurringTransfer + RecurringExecution + ERD update |

### Testowanie integracji

Przykładowe wywołania API dla banków znajdziesz w [INFO.md](./docs/c2b/integration/INFO.md#api-reference).

### Deployment

| Dokument | Zawartość |
|---|---|
| [docs/deployment.md](./docs/deployment.md) | Dwie topologie: **single-host** (dev/demo, `make dev-d`) oraz **split per-VPS** (prod, `docker-compose-vps-a.yml` + `docker-compose-vps-b.yml`). Lista zmiennych env per VPS, diagram komunikacji, checklist, troubleshooting. |

## Development workflow

### Pierwszy setup po klonowaniu repo

```bash
# 1. Skopiuj env
cp .env.example .env
# Wygeneruj SECRET_KEY i wklej
python -c "import secrets; print(secrets.token_urlsafe(50))"

# 2. Pre-commit hooks (lokalne, jednorazowo)
pip install pre-commit detect-secrets
pre-commit install
detect-secrets scan > .secrets.baseline

# 3. Uruchom
make dev
```

### Codzienna praca

```bash
make dev              # Start środowiska
make dev-d            # Start w tle
make logs             # Logi live
make shell            # Bash w kontenerze web
make test             # Testy
make smoke            # Smoke testy
make lint             # Sprawdzenie linterów (ruff)
make format           # Auto-format kodu
make pre-commit       # Uruchom wszystkie hooki
```

UI dostępne pod:

| URL | Co to |
|---|---|
| `http://localhost:8000` | KLIK API + Django Admin (`/admin/`) |
| `http://localhost:5175` | Agent (terminal) UI |
| `http://localhost:5174` | Mock-bank UI (operator) |
| `http://localhost:8100` | Mock-bank backend (webhook + REST dla UI) |
| `http://localhost:9000` | Mock RTGS (4 systemy pod prefiksami) |

> Dla **prod / split-deployment** (KLIK na jednym VPS, mock-bank na drugim) zerknij do [docs/deployment.md](./docs/deployment.md).

### CI

Każdy push i PR przechodzi przez GitHub Actions:
- **Lint** — ruff check + format
- **Tests** — pytest z coverage
- **Docker build** — sprawdzenie buildowania obrazu

PR nie zostanie zmergowany jeśli CI jest czerwony.

## Status projektu
| Moduł | Status |
|---|---|
| Dokumentacja C2B | ✅ kompletna |
| Dokumentacja P2P | ✅ kompletna |
| Dokumentacja Cheques | ✅ kompletna |
| Dokumentacja Recurring | ✅ kompletna |
| Szkielet Django | ✅ kompletny |
| Moduł C2B — backend | ✅ kompletny |
| Moduł P2P — backend | ✅ kompletny |
| Moduł Cheques — backend | ✅ kompletny  |
| Moduł Recurring — backend | ✅ kompletny  |
| Dispatcher RTGS | ✅ kompletny |
| Mock RTGS | ✅ kompletny |
| SORBNET, TARGET, CHAPS, FEDNOW | ❌ ✅ ✅ 🟡 |
| Sesje rozliczeniowe (netting + settlement) | ✅ kompletny |
| Agent rozliczeniowy (Vue, :5175, w Dockerze) | ✅ kompletny |
| Mock banku — C2B (:8100 / :5174) | ✅ kompletny |
| Mock banku — P2P (register / lookup / delete w UI) | ✅ kompletny |
| Deployment guide (single-host + split per-VPS) | ✅ kompletny — patrz [docs/deployment.md](./docs/deployment.md) |

## Autorzy

- Oleksii Nawrocki
- Tomasz Nowak

---

**Przedmiot:** Aplikacje Biznesowe
**Prowadzący:** mgr inż. Marcin Mrukowicz
**Rok akademicki:** 2025/2026
