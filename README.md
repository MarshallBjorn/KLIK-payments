# KLIK

> Centralny operator i router płatności mobilnych w ekosystemie bankowym — odpowiednik BLIK dla wielu stref walutowych.

**Projekt akademicki:** Aplikacje Biznesowe, prowadzący: mgr inż. Marcin Mrukowicz.

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
  - [Quickstart](#quickstart)
    - [Wymagania](#wymagania)
    - [Uruchomienie środowiska developerskiego](#uruchomienie-środowiska-developerskiego)
    - [Testowanie integracji](#testowanie-integracji)
  - [Status projektu](#status-projektu)
  - [Autorzy](#autorzy)

---

## O projekcie

KLIK pełni rolę centralnego operatora i routera płatności mobilnych. Zapewnia dwa główne moduły:

1. **KLIK Kody (C2B)** — autoryzacja płatności w punktach sprzedaży za pomocą 6-cyfrowych kodów generowanych na żądanie banku.
2. **KLIK Telefon (P2P)** — rejestr aliasów mapujący numery telefonów na dane bankowe, umożliwiający przelewy na numer telefonu.

KLIK działa jako niezależny mikroserwis (orkiestrator). **Nie przechowuje środków pieniężnych** — zarządza logiką autoryzacji (Kody) oraz routingiem danych (Telefony). Rozliczenia międzybankowe realizowane są w sesjach nettingowych przez systemy RTGS (SORBNET3 / TARGET2 / CHAPS / FedNow).

System obsługuje cztery strefy walutowo-krajowe (PL, EU, UK, US) z rygorystyczną izolacją strefową — transakcje cross-zone są odrzucane.

## Zakres

### W zakresie projektu

- **KLIK Kody (C2B)** — generowanie kodów, autoryzacja, split prowizji, netting, dispatch do RTGS
- **KLIK Telefon (P2P)** — rejestracja aliasów, lookup (bez clearingu po stronie KLIK w wersji 1.0)
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
├── agent/                # Frontend Vue (symulowany terminal) [TBD]
├── docs/                 # Cała dokumentacja projektu
│   ├── c2b/              # Moduł Kody (C2B)
│   │   ├── bpmn/         # Diagramy BPMN + eksporty PNG
│   │   ├── diagrams/     # Diagramy Mermaid (stany, sekwencje, ERD)
│   │   └── integration/  # Dokumentacja integracyjna dla banków
│   └── p2p/              # Moduł Telefony (P2P) [TBD]
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

Dokumentacja w przygotowaniu. Zakres modułu ograniczony — patrz [sekcja Zakres](#zakres).

### Testowanie integracji

Przykładowe wywołania API dla banków znajdziesz w [INFO.md](./docs/c2b/integration/INFO.md#api-reference).

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
make logs             # Logi live
make shell            # Bash w kontenerze web
make test             # Testy
make lint             # Sprawdzenie linterów (ruff)
make format           # Auto-format kodu
make pre-commit       # Uruchom wszystkie hooki
```

### CI

Każdy push i PR przechodzi przez GitHub Actions:
- **Lint** — ruff check + format
- **Tests** — pytest z coverage
- **Docker build** — sprawdzenie buildowania obrazu

PR nie zostanie zmergowany jeśli CI jest czerwony.

## Status projektu

Projekt w trakcie implementacji. Stan poszczególnych modułów:

| Moduł | Status |
|---|---|
| Dokumentacja C2B | ✅ kompletna |
| Dokumentacja P2P | 🟡 minimum |
| Szkielet Django | 🟡 w trakcie |
| Moduł C2B — backend | 🔴 TBD |
| Moduł P2P — backend | 🔴 TBD |
| Dispatcher RTGS | 🔴 TBD |
| Agent (Vue) | 🔴 TBD |
| Mock banku i RTGS | 🔴 TBD |

## Autorzy

- Oleksii Nawrocki
- Tomasz Nowak

---

**Przedmiot:** Aplikacje Biznesowe
**Prowadzący:** mgr inż. Marcin Mrukowicz
**Rok akademicki:** 2025/2026
