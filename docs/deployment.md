# Deployment guide

KLIK Payments wspiera dwie topologie:

| Tryb | Kiedy używać | Komenda startowa |
|---|---|---|
| **Single-host** | dev / demo / showcase — wszystko na jednej maszynie | `make dev-d` (lub `make prod`) |
| **Split per-VPS** | prod / pre-prod — dwa osobne VPS, jeden dla KLIK, drugi dla mock-banku i RTGS | `docker compose -f docker-compose-vps-<a\|b>.yml up -d` |

Single-host korzysta z istniejących `docker-compose.yml + docker-compose-dev.yml`
(lub `+ docker-compose-prod.yml`). Wszystko niżej dotyczy **split**.

---

## Topologia split

```
       ┌────────────────────────────────────┐         ┌────────────────────────────────────┐
       │ VPS-A — KLIK + Terminal            │         │ VPS-B — Mock-bank + Mock-RTGS      │
       │                                    │         │                                    │
       │   db (postgres) ──── redis         │         │   bank-mock-backend  (:8100)       │
       │           ▲             ▲          │         │           ▲                        │
       │           │             │          │         │           │ webhook                │
       │   web (Django/gunicorn) :8000  ────┼─────────┼──→ bank.webhook_url (HTTPS)        │
       │     ▲           ▲      ▲           │         │           │                        │
       │     │           │      │           │  HTTPS  │           │                        │
       │  worker      beat    agent :5175 ──┼─────────┼──→ rtgs-mock         (:9000)       │
       │                                    │         │     ▲                              │
       │                                    │         │   bank-mock-frontend (:5174)       │
       │                                    │   ←─────┼── BANK_MOCK_KLIK_BASE_URL          │
       └────────────────────────────────────┘  HTTPS  └────────────────────────────────────┘

         Domeny (przykład):                              Domeny (przykład):
           https://klik.user.com                           https://bank.kolega.com   (UI)
           https://terminal.user.com                       https://bank-api.kolega.com (webhook)
                                                           https://rtgs.kolega.com
```

**Komunikacja cross-VPS** odbywa się wyłącznie po publicznych HTTPS-ach.
Żaden serwis na VPS-A nie zna nazw kontenerów VPS-B (i odwrotnie) — sieci dockerowe są
osobne, izolowane per VPS.

### Co biegnie gdzie

| VPS-A (`docker-compose-vps-a.yml`) | VPS-B (`docker-compose-vps-b.yml`) |
|---|---|
| `db` (postgres + volume `db_data`) | `bank-mock-backend` (FastAPI, in-memory) |
| `redis` (cache + Celery broker) | `bank-mock-frontend` (Vue + Vite) |
| `web` (Django, gunicorn, prod settings) | `rtgs-mock` (FastAPI, 4 RTGS-y pod prefiksami) |
| `worker` (Celery — netting, accruals) | — |
| `beat` (Celery Beat — sesje rozliczeniowe, P2P fee accrual) | — |
| `agent` (Vue terminal, Vite dev) | — |

Mock-bank i mock-RTGS są stateless in-memory — **VPS-B nie potrzebuje Postgresa ani Redis-a**.

---

## VPS-A — konfiguracja

### `.env` (sekcje obowiązkowe)

```bash
# Django
DJANGO_SETTINGS_MODULE=core.settings.prod
SECRET_KEY=<wygenerowane: python -c "import secrets; print(secrets.token_urlsafe(50))">
ALLOWED_HOSTS=klik.user.com,terminal.user.com,localhost,web

# CORS — origin-y które UI klienckie mogą bić do KLIK API z przeglądarki.
# Tu wpisz publiczny URL terminala (agenta).
CORS_ALLOWED_ORIGINS=https://terminal.user.com,http://localhost:5175

# Postgres
POSTGRES_DB=klik
POSTGRES_USER=klik
POSTGRES_PASSWORD=<silne hasło>
DATABASE_URL=postgres://klik:<silne hasło>@db:5432/klik

# Redis / Celery
REDIS_URL=redis://redis:6379/0
CELERY_BROKER_URL=redis://redis:6379/1
CELERY_RESULT_BACKEND=redis://redis:6379/2

# RTGS — publiczne URL-e u kolegi
SORBNET3_URL=https://rtgs.kolega.com/sorbnet3
TARGET2_URL=https://rtgs.kolega.com/target2
CHAPS_URL=https://rtgs.kolega.com/chaps
FEDNOW_URL=https://rtgs.kolega.com/fednow

# Agent / terminal
AGENT_VITE_PROXY_TARGET=http://web:8000     # wewnątrz sieci compose VPS-A
AGENT_ALLOWED_HOSTS=terminal.user.com,localhost
```

### `docker-compose-vps-a.yml` (template)

Standalone — odnosi się tylko do serwisów VPS-A, używa `core.settings.prod` i gunicorna.

```yaml
services:
  db:
    image: postgres:15
    volumes:
      - ./db_data:/var/lib/postgresql/data
    environment:
      POSTGRES_DB: ${POSTGRES_DB}
      POSTGRES_USER: ${POSTGRES_USER}
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U ${POSTGRES_USER} -d ${POSTGRES_DB}"]
      interval: 5s
      timeout: 5s
      retries: 5
    restart: unless-stopped
    networks: [klik_net]

  redis:
    image: redis:7-alpine
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      timeout: 3s
      retries: 5
    restart: unless-stopped
    networks: [klik_net]

  web:
    build: ./backend
    command: >
      sh -c "python manage.py migrate &&
             gunicorn core.wsgi:application --bind 0.0.0.0:8000 --workers 4
                     --access-logfile - --error-logfile -"
    env_file: .env
    environment:
      DJANGO_SETTINGS_MODULE: core.settings.prod
    depends_on:
      db: { condition: service_healthy }
      redis: { condition: service_healthy }
    ports:
      - "8000:8000"  # za reverse proxy (nginx/Caddy) — nie wystawiać prosto na publik
    restart: unless-stopped
    networks: [klik_net]

  worker:
    build: ./backend
    command: celery -A core worker --loglevel=info
    env_file: .env
    environment:
      DJANGO_SETTINGS_MODULE: core.settings.prod
    depends_on:
      db: { condition: service_healthy }
      redis: { condition: service_healthy }
    restart: unless-stopped
    networks: [klik_net]

  beat:
    build: ./backend
    command: celery -A core beat --loglevel=info --scheduler django_celery_beat.schedulers:DatabaseScheduler
    env_file: .env
    environment:
      DJANGO_SETTINGS_MODULE: core.settings.prod
    depends_on:
      worker: { condition: service_started }
    restart: unless-stopped
    networks: [klik_net]

  agent:
    image: node:20-alpine
    working_dir: /app
    command: sh -c "npm install && npm run dev -- --host 0.0.0.0 --port 5175"
    environment:
      VITE_PROXY_TARGET: ${AGENT_VITE_PROXY_TARGET}
      VITE_ALLOWED_HOSTS: ${AGENT_ALLOWED_HOSTS}
    volumes:
      - ./agent:/app
    ports:
      - "5175:5175"
    depends_on:
      - web
    restart: unless-stopped
    networks: [klik_net]

networks:
  klik_net:
    driver: bridge
```

### Start

```bash
cp .env.example .env && nano .env       # ustaw sekcje VPS-A
docker compose -f docker-compose-vps-a.yml up -d --build
docker compose -f docker-compose-vps-a.yml logs -f
```

---

## VPS-B — konfiguracja

### `.env` (sekcje obowiązkowe)

```bash
# Django (nieużywane bezpośrednio, ale env_file musi parsować się czysto)
DJANGO_SETTINGS_MODULE=core.settings.prod
SECRET_KEY=<dowolne, nieużywane>

# Mock-bank → KLIK (publiczny URL VPS-A)
BANK_MOCK_NAME=BANK_MOCK
BANK_MOCK_ZONE=PL
BANK_MOCK_KLIK_BASE_URL=https://klik.user.com/api/v1
BANK_MOCK_KLIK_BANK_API_KEY=<klucz wygenerowany w Django Admin KLIK>

# Mock-bank UI — origins
BANK_MOCK_FRONTEND_ALLOWED_HOSTS=bank.kolega.com,localhost

# RTGS — failure injection (opcjonalne, demo)
RTGS_LATENCY_MIN_MS=50
RTGS_LATENCY_MAX_MS=300
RTGS_FAIL_RATE=0.0
RTGS_TIMEOUT_RATE=0.0
RTGS_BLACKLIST=

# KLIK code TTL (musi się zgadzać z VPS-A)
KLIK_CODE_TTL_SECONDS=120
```

### `docker-compose-vps-b.yml` (template)

```yaml
services:
  rtgs-mock:
    build: ./rtgs_mock
    command: uvicorn main:app --host 0.0.0.0 --port 9000
    environment:
      RTGS_LATENCY_MIN_MS: ${RTGS_LATENCY_MIN_MS:-50}
      RTGS_LATENCY_MAX_MS: ${RTGS_LATENCY_MAX_MS:-300}
      RTGS_FAIL_RATE: ${RTGS_FAIL_RATE:-0.0}
      RTGS_TIMEOUT_RATE: ${RTGS_TIMEOUT_RATE:-0.0}
      RTGS_BLACKLIST: ${RTGS_BLACKLIST:-}
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:9000/healthz"]
      interval: 10s
      timeout: 3s
      retries: 5
    ports:
      - "9000:9000"  # za reverse proxy
    restart: unless-stopped
    networks: [bank_net]

  bank-mock-backend:
    build: ./bank_IO/backend
    command: uvicorn main:app --host 0.0.0.0 --port 8100
    environment:
      BANK_NAME: ${BANK_MOCK_NAME:-BANK_MOCK}
      BANK_ZONE: ${BANK_MOCK_ZONE:-PL}
      KLIK_BASE_URL: ${BANK_MOCK_KLIK_BASE_URL}
      KLIK_BANK_API_KEY: ${BANK_MOCK_KLIK_BANK_API_KEY}
      KLIK_CODE_TTL_SECONDS: ${KLIK_CODE_TTL_SECONDS:-120}
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8100/healthz"]
      interval: 10s
      timeout: 3s
      retries: 5
    ports:
      - "8100:8100"  # za reverse proxy
    restart: unless-stopped
    networks: [bank_net]

  bank-mock-frontend:
    image: node:20-alpine
    working_dir: /app
    command: sh -c "npm install && npm run dev -- --host 0.0.0.0 --port 5174"
    environment:
      VITE_ALLOWED_HOSTS: ${BANK_MOCK_FRONTEND_ALLOWED_HOSTS}
    volumes:
      - ./bank_IO/frontend:/app
    ports:
      - "5174:5174"
    depends_on:
      - bank-mock-backend
    restart: unless-stopped
    networks: [bank_net]

networks:
  bank_net:
    driver: bridge
```

### Start

```bash
cp .env.example .env && nano .env       # ustaw sekcje VPS-B
docker compose -f docker-compose-vps-b.yml up -d --build
docker compose -f docker-compose-vps-b.yml logs -f
```

---

## Cross-VPS — co ustawić po starcie

### 1. Klucz API banku (jednorazowo, ręcznie)

Na VPS-A:
```bash
docker compose -f docker-compose-vps-a.yml exec web python manage.py shell
>>> from banks.models import Bank
>>> bank = Bank.objects.create(
...     name="BANK_MOCK",
...     zone="PL",
...     c2b_enabled=True,
...     active=True,
...     webhook_url="https://bank-api.kolega.com/webhook",
...     # api_key generuje się automatycznie w save() — sprawdź w admin
... )
>>> print(bank.api_key)
```

Skopiuj `api_key` → na VPS-B wklej do `.env` jako `BANK_MOCK_KLIK_BANK_API_KEY`, zrestartuj
`bank-mock-backend`.

> Alternatywnie: zaloguj się do Django Admin pod `https://klik.user.com/admin/` i dodaj
> Bank z tego UI.

### 2. Webhook URL

Pole `Bank.webhook_url` w KLIK-u musi wskazywać na publicznie dostępny mock-bank:
```
https://bank-api.kolega.com/webhook
```
KLIK uderza w `{webhook_url}/authorize` więc trailing `/webhook` jest poprawne (nie `/webhook/authorize`).

### 3. Reverse proxy (zalecane)

Wystawianie surowych portów `:8000`, `:8100`, `:9000` na świat to słaby pomysł — brak TLS,
brak rate-limiting, brak access logów. Postaw przed wszystkimi serwisami **nginx** lub
**Caddy** z TLS-em (Let's Encrypt). Przykładowy minimalny `Caddyfile` na VPS-A:

```
klik.user.com {
    reverse_proxy localhost:8000
}

terminal.user.com {
    reverse_proxy localhost:5175
}
```

Analogicznie na VPS-B dla `bank.kolega.com` (→ `:5174`), `bank-api.kolega.com` (→ `:8100`),
`rtgs.kolega.com` (→ `:9000`).

---

## Checklist deployu

### VPS-A
- [ ] `cp .env.example .env` i wypełnij sekcje 1–6
- [ ] `SECRET_KEY` świeży, ≥50 znaków
- [ ] `POSTGRES_PASSWORD` ≠ `change-me`
- [ ] `ALLOWED_HOSTS` zawiera publiczną domenę KLIK
- [ ] `CORS_ALLOWED_ORIGINS` zawiera publiczną domenę terminala
- [ ] RTGS URL-e wskazują na publiczne `rtgs.kolega.com`
- [ ] `AGENT_ALLOWED_HOSTS` zawiera publiczną domenę terminala
- [ ] `docker compose -f docker-compose-vps-a.yml up -d --build`
- [ ] `docker compose -f docker-compose-vps-a.yml exec web python manage.py migrate` (jeśli compose nie robi w command)
- [ ] Stwórz superusera: `... exec web python manage.py createsuperuser`
- [ ] Reverse proxy + TLS skonfigurowane

### VPS-B
- [ ] `cp .env.example .env` i wypełnij sekcje 7–9
- [ ] `BANK_MOCK_KLIK_BASE_URL` = publiczny KLIK
- [ ] `BANK_MOCK_KLIK_BANK_API_KEY` = klucz wygenerowany na VPS-A
- [ ] `BANK_MOCK_FRONTEND_ALLOWED_HOSTS` zawiera publiczną domenę UI banku
- [ ] `docker compose -f docker-compose-vps-b.yml up -d --build`
- [ ] Reverse proxy + TLS skonfigurowane

### Cross-VPS
- [ ] W KLIK Admin: Bank `BANK_MOCK` ma `webhook_url=https://bank-api.kolega.com/webhook`
- [ ] Smoke test ręczny: w mock-bank UI → wygeneruj kod → wpisz w terminalu → autoryzuj → COMPLETED
- [ ] Smoke P2P: w mock-bank UI → zarejestruj alias → lookup → delete

---

## Troubleshooting

| Objaw | Diagnoza |
|---|---|
| `Bad Gateway` z `klik.user.com` | reverse proxy nie dosięga `:8000` — sprawdź `docker compose ps`, czy `web` healthy |
| `CORS error` w przeglądarce na terminalu | `CORS_ALLOWED_ORIGINS` w `.env` VPS-A nie zawiera schemy+domeny+portu terminala. Restart `web` po zmianie. |
| `MOCK_BANK_UNREACHABLE` przy `smoke_c2b` | VPS-A nie dosięga `bank.webhook_url` — sprawdź DNS / TLS / firewall / Caddy konfig na VPS-B |
| `KLIK_UNREACHABLE` w logach `bank-mock-backend` | VPS-B nie dosięga `BANK_MOCK_KLIK_BASE_URL` — analogicznie |
| `401 INVALID_API_KEY` w logach mock-banku | `BANK_MOCK_KLIK_BANK_API_KEY` rozjechał się z wartością w DB KLIK-a. Wygeneruj nowy w Admin, zaktualizuj `.env` VPS-B, restart `bank-mock-backend` |
| Vite "Blocked request… not allowed host" | `AGENT_ALLOWED_HOSTS` / `BANK_MOCK_FRONTEND_ALLOWED_HOSTS` nie zawiera hostname-u z którego browser ładuje stronę. Dodaj i zrestartuj kontener |
| Sesje rozliczeniowe nie ruszają | sprawdź `docker compose logs beat` — Celery Beat musi być healthy; ewentualnie `SESSION_INTERVAL_MINUTES_PL=2` dla demo, żeby cykl odpalał się co 2 minuty |

---

## Single-host (na koniec, dla kompletności)

Dla devu / showcase / demo zostają obecne pliki:
```bash
make dev-d                  # docker-compose.yml + docker-compose-dev.yml
# lub
make prod                   # docker-compose.yml + docker-compose-prod.yml (gunicorn, na jednym hoście)
```

Wszystkie serwisy w jednej sieci compose, komunikacja po nazwach kontenerów
(`web`, `db`, `redis`, `rtgs-mock`, `bank-mock-backend`). Porty zmapowane na hosta
(`8000`, `8100`, `9000`, `5174`, `5175`) dla łatwego dostępu z przeglądarki / curla.
