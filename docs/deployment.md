# Deployment guide

KLIK Payments wspiera dwie topologie:

| Tryb | Kiedy używać | Komenda startowa |
|---|---|---|
| **Single-host** | dev / demo / showcase — wszystko na jednej maszynie | `make dev-d` (lub `make prod`) |
| **Split per-VPS** | prod / akademicki deploy — dwa osobne VPS, jeden dla KLIK, drugi dla mock-banku i RTGS | `docker compose -f docker-compose-vps-<a\|b>.yml pull && up -d` |

Single-host korzysta z istniejących `docker-compose.yml + docker-compose-dev.yml`
(lub `+ docker-compose-prod.yml`) i **buduje obrazy lokalnie**.

Split jest **pull-based**: obrazy buduje CI i pushuje na GHCR, a każdy VPS tylko je
ściąga (`docker compose pull`) — nie buduje nic u siebie. To kluczowe, bo jeden z
VPS-ów ma mało RAM-u i build (zwłaszcza `npm` + Vite) by go zarżnął. Wszystko niżej
dotyczy **split**, poza ostatnią sekcją.

---

## Obrazy i CD (GHCR) — „podwójne CD"

Pipeline `.github/workflows/cd.yml` robi dwa kroki:

1. **build** — buduje 5 obrazów i pushuje je na GitHub Container Registry (GHCR).
2. **deploy-vps-a** / **deploy-vps-b** — loguje się przez SSH na każdy VPS i robi
   `docker compose -f docker-compose-vps-<a|b>.yml pull && up -d`. VPS-y odpalają się
   równolegle — stąd „podwójne".

### Obrazy

| Komponent | Obraz GHCR | Używany na | Z czego |
|---|---|---|---|
| `backend` | `ghcr.io/marshallbjorn/klik-payments/backend` | VPS-A (`web`, `worker`, `beat`) | `backend/Dockerfile` |
| `agent` | `ghcr.io/marshallbjorn/klik-payments/agent` | VPS-A (`agent`) | `agent/Dockerfile` (Vue build → nginx + proxy `/api`) |
| `rtgs-mock` | `ghcr.io/marshallbjorn/klik-payments/rtgs-mock` | VPS-B | `rtgs_mock/Dockerfile` |
| `bank-mock-backend` | `ghcr.io/marshallbjorn/klik-payments/bank-mock-backend` | VPS-B | `bank_IO/backend/Dockerfile` |
| `bank-mock-frontend` | `ghcr.io/marshallbjorn/klik-payments/bank-mock-frontend` | VPS-B | `bank_IO/frontend/Dockerfile` (Vue build → nginx statyk) |

> Frontendy w split lecą jako **statyk pod nginx** (nie Vite dev server) — lekkie i
> bez `npm install` na VPS-ie. `agent` ma w nginx reverse proxy `/api → web:8000`,
> `bank-mock-frontend` to czysty statyk (backend konfiguruje operator w UI runtime).

### Trigger i tagi

| Zdarzenie | Tagi obrazów | Deploy na VPS? |
|---|---|---|
| push na `main` | `latest`, `main`, `sha-<short>` | tak (`IMAGE_TAG=latest`) |
| tag `v*` (np. `v1.2.3`) | `1.2.3`, `1.2` | tak (`IMAGE_TAG=1.2.3`) |
| `workflow_dispatch` (ręcznie) | jak ref | tylko gdy ref = main/tag |

### Wymagane sekrety GitHub (Settings → Secrets and variables → Actions)

Build używa wbudowanego `GITHUB_TOKEN` (ma `packages: write`) — nic nie trzeba dodawać.
Deploy potrzebuje per-VPS (zalecane: w **Environments** `vps-a` / `vps-b`):

| Sekret | Opis |
|---|---|
| `VPS_A_HOST` / `VPS_B_HOST` | IP lub domena VPS-a |
| `VPS_A_USER` / `VPS_B_USER` | użytkownik SSH |
| `VPS_A_SSH_KEY` / `VPS_B_SSH_KEY` | prywatny klucz SSH (PEM) |
| `VPS_A_PORT` / `VPS_B_PORT` | port SSH (np. 22) |
| `VPS_A_PATH` / `VPS_B_PATH` | ścieżka do sklonowanego repo na VPS-ie |
| `GHCR_USER` + `GHCR_TOKEN` | *(opcjonalne)* login do GHCR na VPS-ie, jeśli pakiety są **prywatne**. PAT z `read:packages`. Gdy pakiety publiczne — zostaw puste. |

> **Auth GHCR na VPS-ie:** najprościej ustawić pakiety jako *public*
> (GitHub → Packages → dany obraz → Package settings → Change visibility).
> Wtedy `docker compose pull` działa bez logowania i `GHCR_*` są zbędne.

### Założenia deployu przez SSH

Skrypt deploy zakłada, że na każdym VPS-ie jest **sklonowane repo** w `VPS_*_PATH`
z wypełnionym `.env` (patrz niżej). Krok robi `git fetch` + checkout commita,
opcjonalny `docker login`, `compose pull`, `up -d` i `docker image prune -f`.


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

# Obrazy GHCR
IMAGE_TAG=latest                            # lub konkretna wersja, np. 1.2.3
AGENT_PORT=5175
```

# Agent / terminal — w split obraz agenta to nginx (statyk + proxy /api → web).
AGENT_KLIK_UPSTREAM=web:8000                # wewnątrz sieci compose VPS-A

### `docker-compose-vps-a.yml`

Gotowy plik jest w repo — odnosi się tylko do serwisów VPS-A (`db`, `redis`, `web`,
`worker`, `beat`, `agent`), używa `core.settings.prod` + gunicorna i **ciągnie obrazy
z GHCR** (`image:`, bez `build:`). Migracje robi `web` w `command` przy starcie.

### Start

```bash
cp .env.example .env && nano .env       # ustaw sekcje 0 + VPS-A
# (jeśli pakiety GHCR prywatne) docker login ghcr.io -u <user>
docker compose -f docker-compose-vps-a.yml pull
docker compose -f docker-compose-vps-a.yml up -d
```

> CD robi te same `pull` + `up -d` po SSH automatycznie po pushu na `main` / tagu.
> Ręczny start jest potrzebny tylko za pierwszym razem (lub gdy deployujesz spoza CI).

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

> W split obraz `bank-mock-frontend` to statyk pod nginx — `BANK_MOCK_FRONTEND_ALLOWED_HOSTS`
> jest wtedy nieużywane (dotyczy tylko Vite dev w single-host). Port hosta ustawiasz przez
> `BANK_MOCK_FRONTEND_PORT` (default 5174).

### `docker-compose-vps-b.yml`

Gotowy plik jest w repo — odnosi się tylko do serwisów VPS-B (`rtgs-mock`,
`bank-mock-backend`, `bank-mock-frontend`) i **ciągnie obrazy z GHCR** (`image:`, bez
`build:`). Mock-y są stateless in-memory, więc VPS-B nie potrzebuje DB ani Redisa.

### Start

```bash
cp .env.example .env && nano .env       # ustaw sekcje 0 + VPS-B
# (jeśli pakiety GHCR prywatne) docker login ghcr.io -u <user>
docker compose -f docker-compose-vps-b.yml pull
docker compose -f docker-compose-vps-b.yml up -d
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

### CI/CD (jednorazowo)
- [ ] Sekrety `VPS_A_*` i `VPS_B_*` dodane (Environments `vps-a` / `vps-b`)
- [ ] Pakiety GHCR publiczne **lub** `GHCR_USER` + `GHCR_TOKEN` ustawione
- [ ] Repo sklonowane na obu VPS-ach w `VPS_*_PATH`, z wypełnionym `.env`

### VPS-A
- [ ] `cp .env.example .env` i wypełnij sekcje 0–6
- [ ] `IMAGE_TAG` ustawiony (`latest` lub konkretna wersja)
- [ ] `SECRET_KEY` świeży, ≥50 znaków
- [ ] `POSTGRES_PASSWORD` ≠ `change-me`
- [ ] `ALLOWED_HOSTS` zawiera publiczną domenę KLIK
- [ ] `CORS_ALLOWED_ORIGINS` zawiera publiczną domenę terminala
- [ ] RTGS URL-e wskazują na publiczne `rtgs.kolega.com`
- [ ] `docker compose -f docker-compose-vps-a.yml pull && up -d` (migracje robi `web` w command)
- [ ] Stwórz superusera: `docker compose -f docker-compose-vps-a.yml exec web python manage.py createsuperuser`
- [ ] Reverse proxy + TLS skonfigurowane

### VPS-B
- [ ] `cp .env.example .env` i wypełnij sekcje 0 + 7–9
- [ ] `BANK_MOCK_KLIK_BASE_URL` = publiczny KLIK
- [ ] `BANK_MOCK_KLIK_BANK_API_KEY` = klucz wygenerowany na VPS-A
- [ ] `IMAGE_TAG` zgodny z VPS-A
- [ ] `docker compose -f docker-compose-vps-b.yml pull && up -d`
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
| `denied` / `manifest unknown` przy `compose pull` | pakiety GHCR prywatne i brak `docker login ghcr.io` (ustaw `GHCR_USER`/`GHCR_TOKEN` albo zrób pakiety publiczne), albo zły `IMAGE_TAG` — sprawdź czy build dla tego tagu przeszedł |
| Job `deploy-vps-*` wisi / `ssh: handshake failed` | zły `VPS_*_HOST`/`PORT`, klucz `VPS_*_SSH_KEY` nie pasuje do `authorized_keys`, albo firewall blokuje SSH z runnera GitHuba |
| `compose pull` na VPS ciągnie stary obraz | tag `latest` nieprzemigrowany — wymuś `docker compose pull` ręcznie; dla powtarzalności deployuj tagiem `v*` zamiast `latest` |

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
