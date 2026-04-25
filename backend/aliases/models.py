"""
Model Alias — rejestr P2P mapujący numer telefonu na konto bankowe.

Rola w systemie KLIK Telefon (P2P), zgodnie z diagramem sekwencji w docs/p2p:

  ETAP 1: Bank A (odbiorcy) wywołuje POST /aliases/register po włączeniu
          przez klienta funkcji "Przelew na telefon".
  ETAP 2: Bank B (nadawcy) wywołuje GET /aliases/lookup/{phone} żeby
          dowiedzieć się dokąd routować przelew (bank_id + IBAN odbiorcy).
  ETAP 3: Bank B realizuje przelew BEZPOŚREDNIO przez RTP (Elixir Express,
          Faster Payments, SEPA Instant, FedNow RTP) — z pominięciem KLIK.

Założenia projektowe:

- `phone` w E.164 (`+48...`), unique GLOBALNIE — jeden numer to jedno
  aktywne konto bankowe w całym systemie. Klient zmieniający bank musi
  najpierw wyrejestrować stary alias.
- `bank` to FK do `banks.Bank` z CASCADE: usunięcie banku z systemu kasuje
  jego aliasy (bank wypadający z KLIK nie ma sensu pozostawiać w rejestrze).
- `account_identifier` jako JSONField — spójność z `agents.Agent` i
  obsługa US (routing_number + account_number) bez zmian schemy.
- `zone` redundantna względem `bank.zone`, ale trzymamy ją wprost żeby:
  (a) walidacja prefix telefonu ↔ zone była lokalna,
  (b) lookup po phone nie wymagał JOIN-a do Bank.
  Spójność (alias.zone == bank.zone) wymuszamy w `clean()`.
"""

from django.core.exceptions import ValidationError
from django.db import models

from common.account import validate_account_identifier
from common.enums import TimestampedModel, Zone
from common.phone import validate_e164, validate_phone_matches_zone


class Alias(TimestampedModel):
    """Mapowanie numeru telefonu → konto bankowe (rejestr P2P)."""

    phone = models.CharField(
        max_length=16,  # E.164 max 15 cyfr + znak '+'
        unique=True,
        db_index=True,
        help_text='Numer telefonu w formacie E.164 (np. +48501234567).',
    )

    bank = models.ForeignKey(
        'banks.Bank',
        on_delete=models.CASCADE,
        related_name='aliases',
        help_text='Bank klienta — beneficjent przelewów P2P na ten numer.',
    )

    account_identifier = models.JSONField(
        help_text=(
            'Strukturalny identyfikator konta. Format zależy od strefy: '
            'IBAN dla PL/EU/UK ({"type": "iban", "value": "..."}), '
            'routing+account dla US. Schemat opisany w common.account.'
        ),
    )

    zone = models.CharField(
        max_length=2,
        choices=Zone.choices,
        help_text=(
            'Strefa rejestracji aliasu. Musi być zgodna z prefiksem telefonu '
            'oraz strefą banku.'
        ),
    )

    class Meta:
        verbose_name = 'Alias'
        verbose_name_plural = 'Aliasy'
        ordering = ['phone']
        # Globalny unique na phone wymuszamy już przez `unique=True` na polu,
        # ale dodatkowo dodajemy named constraint dla czytelności w komunikatach
        # IntegrityError (DB → 409_ALIAS_ALREADY_EXISTS).
        constraints = [
            models.UniqueConstraint(
                fields=['phone'],
                name='alias_phone_unique',
            ),
        ]

    def __str__(self) -> str:
        return f'{self.phone} → {self.bank.name} ({self.zone})'

    def save(self, *args, **kwargs):
        # Wymusza walidację przy każdym save() — bezpieczna sieć dla bezpośrednich
        # użyć modelu (admin, shell, fixtures). Serializer i tak waliduje wcześniej.
        self.full_clean()
        super().save(*args, **kwargs)

    def clean(self) -> None:
        """Walidacja domenowa.

        Trzy kontrole (rzucamy `ValidationError` z kluczem pola — w widoku
        mapujemy je na konkretne `error.code` typu `422_ZONE_MISMATCH`):

        1. Format telefonu (E.164) i zgodność prefiksu ze strefą.
        2. Strefa aliasu == strefa banku.
        3. account_identifier zgodny strukturalnie ze strefą (IBAN PL itd.).
        """
        super().clean()

        # 1. Telefon ↔ strefa
        if self.phone and self.zone:
            try:
                validate_phone_matches_zone(self.phone, self.zone)
            except ValidationError as exc:
                # Ponownie pakujemy z kluczem 'zone' — to ustawia `error.code`
                # na 422_ZONE_MISMATCH w widoku (patrz aliases/views.py).
                raise ValidationError({'zone': exc.messages}) from exc
        elif self.phone:
            # Sam telefon bez strefy — sprawdźmy przynajmniej format.
            try:
                validate_e164(self.phone)
            except ValidationError as exc:
                raise ValidationError({'phone': exc.messages}) from exc

        # 2. Strefa aliasu == strefa banku
        if self.bank_id and self.zone and self.zone != self.bank.zone:
            raise ValidationError(
                {
                    'zone': (
                        f'Strefa aliasu ({self.zone}) nie zgadza się ze strefą '
                        f'banku ({self.bank.zone}).'
                    )
                }
            )

        # 3. account_identifier ↔ strefa
        if self.account_identifier and self.zone:
            try:
                validate_account_identifier(self.account_identifier, self.zone)
            except ValidationError as exc:
                raise ValidationError({'account_identifier': exc.messages}) from exc