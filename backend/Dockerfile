FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# psycopg[binary] nie wymaga gcc/libpq-dev, więc tylko minimum
# curl dla healthchecks (alternatywa do urllib w Pythonie)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy wymagań osobno żeby cache działał
COPY requirements.txt /app/
RUN pip install -r requirements.txt

# User nie-root
RUN useradd --create-home --shell /bin/bash klik && \
    chown -R klik:klik /app
USER klik

COPY --chown=klik:klik . /app/

EXPOSE 8000
