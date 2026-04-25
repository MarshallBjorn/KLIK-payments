"""
Współdzielone abstrakcje modelu domenowego.

`Zone` i `Currency` są wykorzystywane przez wiele apek (banks, agents,
merchants, ledger), więc trzymamy je tutaj. ERD: docs/c2b/diagrams/STATE.md (C1).
"""

import uuid

from django.db import models


class Zone(models.TextChoices):
    """Strefy walutowo-krajowe obsługiwane przez KLIK.

    Mapowanie strefa → RTGS gateway w docs/c2b/diagrams/STATE.md (C2).
    """

    PL = 'PL', 'Polska (SORBNET3)'
    EU = 'EU', 'Strefa Euro (TARGET2)'
    UK = 'UK', 'Wielka Brytania (CHAPS)'
    US = 'US', 'Stany Zjednoczone (FedNow)'


class Currency(models.TextChoices):
    """Waluty obsługiwane przez KLIK (1:1 z Zone)."""

    PLN = 'PLN', 'Złoty polski'
    EUR = 'EUR', 'Euro'
    GBP = 'GBP', 'Funt brytyjski'
    USD = 'USD', 'Dolar amerykański'


# Mapowanie wymuszające spójność strefa ↔ waluta.
# Bank w strefie PL musi rozliczać w PLN itd. — patrz INFO.md, error 422_CURRENCY_MISMATCH.
ZONE_CURRENCY = {
    Zone.PL: Currency.PLN,
    Zone.EU: Currency.EUR,
    Zone.UK: Currency.GBP,
    Zone.US: Currency.USD,
}


class TimestampedModel(models.Model):
    """Bazowa klasa: UUID jako PK + created_at/updated_at.

    UUID zamiast autoinkrement bo:
    - ID są przekazywane do banków/agentów (publiczne) — nie chcemy ujawniać liczności
    - łatwiej generować przed insertem (idempotency, korelacja z webhookami)
    - ERD wszystkie encje mają `uuid id PK`.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True
