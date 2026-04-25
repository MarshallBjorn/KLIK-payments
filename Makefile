.PHONY: help dev dev-d prod down logs shell migrate makemigrations createsuperuser test lint format pre-commit clean

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
	@echo ""
	@echo "  Quality:"
	@echo "    make test             Uruchom testy z coverage"
	@echo "    make lint             Sprawdź kod (ruff)"
	@echo "    make format           Sformatuj kod (ruff)"
	@echo "    make pre-commit       Uruchom pre-commit hooks na całym repo"

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
