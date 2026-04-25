"""Modele apki merchants — Merchant."""

import uuid

from django.core.exceptions import ValidationError
from django.db import models

from common.account import validate_account_identifier
from common.enums import Zone


class Merchant(models.Model):
    """
    Merchant — sklep/punkt sprzedaży będący beneficjentem płatności.

    Merchant nie loguje się do KLIK bezpośrednio — interakcja przebiega
    przez agenta (sklep/bramka), który ma swój api_key. Merchant
    identyfikowany jest przez settlement_bank + account_identifier
    (gdzie KLIK kieruje należność po nettingu).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=200)
    settlement_bank = models.ForeignKey(
        'banks.Bank',
        on_delete=models.PROTECT,
        related_name='merchants',
        help_text='Bank w którym merchant ma konto rozliczeniowe.',
    )
    account_identifier = models.JSONField(
        help_text=(
            'Strukturalny identyfikator konta. IBAN dla PL/EU/UK, '
            'routing+account dla US. Schemat w common.account.'
        ),
    )
    zone = models.CharField(max_length=2, choices=Zone.choices)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return f'{self.name} ({self.zone})'

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def clean(self):
        """Walidacja: zone == settlement_bank.zone, account_identifier zgodny ze strefą."""
        super().clean()

        if self.settlement_bank_id and self.zone != self.settlement_bank.zone:
            raise ValidationError(
                {
                    'zone': (
                        f'Strefa merchanta ({self.zone}) musi być zgodna ze strefą '
                        f'settlement_bank ({self.settlement_bank.zone}).'
                    )
                }
            )

        if self.account_identifier and self.zone:
            try:
                validate_account_identifier(self.account_identifier, self.zone)
            except ValidationError as e:
                raise ValidationError({'account_identifier': e.messages}) from e
