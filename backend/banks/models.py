"""
Model Bank.

Reprezentuje uczestnika ekosystemu KLIK po stronie wystawcy kart / banku
nadawcy. Zgodnie z ERD (docs/c2b/diagrams/STATE.md, sekcja C1).

Kluczowe założenia:
- Bank operuje w jednej strefie i jednej walucie (zgodność strefa↔waluta
  wymuszona przez `clean()` oraz constraint w DB).
- `api_key_hash` przechowuje SHA-256 plaintext klucza — sam plaintext jest
  pokazywany tylko raz, w momencie generowania (Django Admin), zgodnie z A0.
- `active=False` to default po `INSERT`. Bank staje się aktywny dopiero
  po pozytywnym pingu webhooka (krok 4 w A0). Operator może też zablokować
  bank ręcznie — wtedy autoryzacja działa, ale endpointy zwracają 403_BANK_INACTIVE.
- `debt_limit` to maksymalne saldo netto debetowe banku w sesji rozliczeniowej.
  Przekroczenie powoduje że bank trafi do następnej sesji (mechanizm w
  module ledger, A5).
"""

import hashlib
import secrets

from django.core.exceptions import ValidationError
from django.db import models

from common.enums import ZONE_CURRENCY, Currency, TimestampedModel, Zone


def hash_api_key(plaintext: str) -> str:
    """Hashuje klucz API algorytmem SHA-256.

    Używamy SHA-256 zamiast bcrypt/argon2 ponieważ:
    - Klucze API są długie i losowe (256 bitów), więc atak słownikowy jest bezsensowny
    - Każde uwierzytelnienie banku wymaga porównania → musi być szybkie
    - Nie chronimy hasła użytkownika, tylko losowy token
    """
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def generate_api_key() -> tuple[str, str]:
    """Generuje nowy klucz API.

    Zwraca tuple (plaintext, hash). Plaintext widoczny tylko raz —
    do przekazania bankowi bezpiecznym kanałem (krok 2 w A0).

    Format: prefix `klik_` + 32 bajty losowe (base64url) ≈ 43 znaki.
    Prefix ułatwia rozpoznanie klucza w logach/grepach (i secret-scannerom).
    """
    plaintext = f"klik_{secrets.token_urlsafe(32)}"
    return plaintext, hash_api_key(plaintext)


class Bank(TimestampedModel):
    """Bank uczestniczący w ekosystemie KLIK."""

    name = models.CharField(
        max_length=128,
        unique=True,
        help_text="Nazwa banku (unikalna w obrębie systemu).",
    )

    api_key_hash = models.CharField(
        max_length=64,
        unique=True,
        help_text="SHA-256 hash klucza API. Plaintext nie jest przechowywany.",
    )

    zone = models.CharField(
        max_length=2,
        choices=Zone.choices,
        help_text="Strefa walutowo-krajowa, w której bank operuje.",
    )

    currency = models.CharField(
        max_length=3,
        choices=Currency.choices,
        help_text="Waluta rozliczeniowa banku. Musi pasować do strefy.",
    )

    debt_limit = models.DecimalField(
        max_digits=18,
        decimal_places=2,
        default=0,
        help_text=(
            "Maksymalne saldo debetowe banku w sesji rozliczeniowej "
            "(w walucie banku). Przekroczenie wyklucza bank z sesji."
        ),
    )

    active = models.BooleanField(
        default=False,
        help_text=(
            "Czy bank może wywoływać API. False = onboarding niezakończony "
            "lub bank zablokowany ręcznie przez operatora."
        ),
    )

    webhook_url = models.URLField(
        max_length=500,
        blank=True,
        default="",
        help_text=(
            "URL endpointu webhooka autoryzacyjnego banku. "
            "KLIK uderza tu z payloadem /authorize (patrz A3 w WORKFLOW.md)."
        ),
    )

    class Meta:
        verbose_name = "Bank"
        verbose_name_plural = "Banki"
        ordering = ["name"]
        constraints = [
            # Spójność strefa ↔ waluta na poziomie DB. Drugą linią obrony jest clean().
            models.CheckConstraint(
                name="bank_zone_currency_match",
                condition=(
                    models.Q(zone="PL", currency="PLN")
                    | models.Q(zone="EU", currency="EUR")
                    | models.Q(zone="UK", currency="GBP")
                    | models.Q(zone="US", currency="USD")
                ),
            ),
            models.CheckConstraint(
                name="bank_debt_limit_non_negative",
                condition=models.Q(debt_limit__gte=0),
            ),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.zone})"

    # ------------------------------------------------------------------
    # DRF compatibility
    # ------------------------------------------------------------------
    # DRF's IsAuthenticated permission sprawdza `request.user.is_authenticated`.
    # Standardowo to property na `auth.User`, ale my zwracamy Bank jako user-a
    # (banki nie mają encji User w MVP). Hardkodujemy True bo sam fakt że Bank
    # trafił do request.user oznacza że auth class go zwaliował.
    # Analogicznie `is_anonymous` musi być False dla symetrii.

    @property
    def is_authenticated(self) -> bool:
        return True

    @property
    def is_anonymous(self) -> bool:
        return False

    def clean(self) -> None:
        """Walidacja na poziomie modelu — wywoływana przez admin i serializery."""
        super().clean()
        expected_currency = ZONE_CURRENCY.get(Zone(self.zone)) if self.zone else None
        if expected_currency and self.currency != expected_currency:
            raise ValidationError(
                {
                    "currency": (
                        f"Waluta {self.currency} nie pasuje do strefy {self.zone}. "
                        f"Oczekiwano: {expected_currency}."
                    )
                }
            )

    def rotate_api_key(self) -> str:
        """Generuje nowy klucz API, zapisuje hash, zwraca plaintext.

        Plaintext jest dostępny tylko jako wartość zwracana — wywołujący jest
        odpowiedzialny za przekazanie go bankowi i zapomnienie o nim.

        Nie commitujemy save() w tej metodzie — to zostawiamy wywołującemu,
        żeby mógł zrobić to w transakcji razem z innymi zmianami.
        """
        plaintext, hashed = generate_api_key()
        self.api_key_hash = hashed
        return plaintext
