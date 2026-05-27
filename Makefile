.PHONY: help dev dev-d prod down logs shell migrate makemigrations createsuperuser startapp test lint format pre-commit clean

help:
	@echo "KLIK — dostępne komendy:"
	@echo ""
	@echo "  Środowisko:"
	@echo "    make dev              Uruchom dev (foreground)"
	@echo "    make dev-d            Uruchom dev (background)"
	@echo "    make prod             Uruchom prod"
	@echo "    make down             Zatrzymaj wszystkie kontenery"
	@echo "    make logs             Pokaż logi (live)"
	@echo "    make clean            Wyczyść wszystko (kontenery, volumes, images)"
	@echo ""
	@echo "  Django:"
	@echo "    make shell            Wejdź do kontenera web (bash)"
	@echo "    make migrate          Uruchom migracje"
	@echo "    make makemigrations   Wygeneruj migracje"
	@echo "    make createsuperuser  Utwórz superusera"
	@echo "    make startapp APP=... Utwórz nową aplikację Django"
	@echo ""
	@echo "  Quality:"
	@echo "    make test             Uruchom testy z coverage"
	@echo "    make lint             Sprawdź kod (ruff)"
	@echo "    make format           Sformatuj kod (ruff)"
	@echo "    make pre-commit       Uruchom pre-commit hooks na całym repo"
	@echo "    make smoke [MODULE=c2b|settle|all] [SCENARIO=...]"

DEV = docker compose -f docker-compose.yml -f docker-compose-dev.yml
PROD = docker compose -f docker-compose.yml -f docker-compose-prod.yml

# Środowisko
dev:
	$(DEV) up --build

dev-d:
	$(DEV) up --build -d

prod:
	$(PROD) up --build -d

down:
	docker compose down

logs:
	$(DEV) logs -f

clean:
	docker compose down -v --rmi all
	rm -rf db_data/

# Django
shell:
	$(DEV) exec web bash

migrate:
	$(DEV) exec web python manage.py migrate

makemigrations:
	$(DEV) exec web python manage.py makemigrations

createsuperuser:
	$(DEV) exec web python manage.py createsuperuser

startapp:
	$(DEV) exec web python manage.py startapp $(APP)

# Quality
test:
	$(DEV) exec web pytest

lint:
	$(DEV) exec web ruff check .

format:
	$(DEV) exec web ruff check --fix .
	$(DEV) exec web ruff format .

pre-commit:
	pre-commit run --all-files

lint-fix:
	$(DEV) exec web ruff check --fix .


# Smoke: zewnętrzne testy E2E przez realne kontenery
# Użycie:
#   make smoke                          # wszystko (c2b + settle)
#   make smoke MODULE=c2b               # tylko C2B (mock-bank)
#   make smoke MODULE=settle            # tylko settlement (mock-RTGS)
#   make smoke MODULE=c2b SCENARIO=happy
#   make smoke MODULE=settle SCENARIO=partial
MODULE ?= all
SCENARIO ?= all

ifeq ($(MODULE),c2b)
smoke:
	$(DEV) exec web python manage.py smoke_c2b --scenario $(SCENARIO)
else ifeq ($(MODULE),settle)
smoke:
	$(DEV) exec web python manage.py settle_smoke --scenario $(SCENARIO)
else ifeq ($(MODULE),all)
smoke:
	$(DEV) exec web python manage.py smoke_c2b --scenario all
	$(DEV) exec web python manage.py settle_smoke --scenario all
else
smoke:
	@echo "Nieznany MODULE=$(MODULE). Uzyj: c2b | settle | all"
	@exit 1
endif
