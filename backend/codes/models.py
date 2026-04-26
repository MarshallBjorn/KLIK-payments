"""Modele apki codes — Transaction."""

import uuid
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import models

from codes.enums import RejectReason, TransactionStatus
from common.enums import Zone

ZONE_TO_CURRENCY = {
    Zone.PL: 'PLN',
    Zone.EU: 'EUR',
    Zone.UK: 'GBP',
    Zone.US: 'USD',
}


class Transaction(models.Model):
    """
    Transakcja KLIK Kody (C2B) — powstaje w momencie inicjacji płatności
    przez agenta (POST /payments/initiate). Cykl życia opisany w STATE.md (B2).

    Postgres = source of truth. Status cache w Redis (key: tx:{id}) dla
    szybkiego pollingu agenta.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # Strony transakcji
    sender_bank = models.ForeignKey(
        'banks.Bank',
        on_delete=models.PROTECT,
        related_name='transactions_as_sender',
    )
    agent = models.ForeignKey(
        'agents.Agent',
        on_delete=models.PROTECT,
        related_name='transactions',
    )
    merchant = models.ForeignKey(
        'merchants.Merchant',
        on_delete=models.PROTECT,
        related_name='transactions',
    )

    # Kod (snapshot do audytu — nie służy do lookupów)
    code_snapshot = models.CharField(
        max_length=6,
        help_text='Kod KLIK użyty w transakcji. Audytowy — nie do lookupu.',
    )

    # Kwoty
    amount_gross = models.DecimalField(max_digits=12, decimal_places=2)
    klik_fee = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        help_text='Wyliczane przy /confirm.',
    )
    agent_fee = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
    )
    merchant_net = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
    )
    currency = models.CharField(max_length=3)
    zone = models.CharField(max_length=2, choices=Zone.choices)

    # Flaga on-us / off-us — wyliczana przy create
    is_on_us = models.BooleanField(
        help_text='True gdy sender_bank == merchant.settlement_bank.',
    )

    # Status i error tracking
    status = models.CharField(
        max_length=20,
        choices=TransactionStatus.choices,
        default=TransactionStatus.PENDING,
        db_index=True,
    )
    reject_reason = models.CharField(
        max_length=30,
        choices=RejectReason.choices,
        blank=True,
    )

    # Idempotency
    idempotency_key = models.CharField(max_length=100, db_index=True)

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    authorized_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['agent', 'idempotency_key'],
                name='tx_unique_agent_idempotency',
            ),
            models.CheckConstraint(
                condition=models.Q(amount_gross__gt=0),
                name='tx_amount_gross_positive',
            ),
        ]
        indexes = [
            models.Index(fields=['status', 'zone']),
            models.Index(fields=['created_at']),
        ]

    def __str__(self):
        return f'Tx {self.id} ({self.status}, {self.amount_gross} {self.currency})'

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def clean(self):
        """Walidacje cross-field."""
        super().clean()

        # Currency musi pasować do strefy
        expected_currency = ZONE_TO_CURRENCY.get(self.zone)
        if expected_currency and self.currency != expected_currency:
            raise ValidationError(
                {
                    'currency': (
                        f'Strefa {self.zone} wymaga waluty {expected_currency}, '
                        f'otrzymano {self.currency}.'
                    )
                }
            )

        # Wszystkie strony muszą być w tej samej strefie
        if self.sender_bank_id and self.zone != self.sender_bank.zone:
            raise ValidationError(
                {
                    'zone': f'Strefa transakcji ({self.zone}) ≠ strefa sender_bank ({self.sender_bank.zone}).'
                }
            )
        if self.agent_id and self.zone != self.agent.zone:
            raise ValidationError(
                {'zone': f'Strefa transakcji ({self.zone}) ≠ strefa agent ({self.agent.zone}).'}
            )
        if self.merchant_id and self.zone != self.merchant.zone:
            raise ValidationError(
                {
                    'zone': f'Strefa transakcji ({self.zone}) ≠ strefa merchant ({self.merchant.zone}).'
                }
            )

        # Status COMPLETED wymaga wypełnionych fees
        if self.status == TransactionStatus.COMPLETED:
            if self.merchant_net is None or self.klik_fee is None or self.agent_fee is None:
                raise ValidationError(
                    'Transakcja COMPLETED wymaga wypełnionych klik_fee, agent_fee, merchant_net.'
                )

        # Status REJECTED wymaga reject_reason
        if self.status == TransactionStatus.REJECTED and not self.reject_reason:
            raise ValidationError({'reject_reason': 'Transakcja REJECTED wymaga reject_reason.'})

    @property
    def total_fees(self) -> Decimal:
        """Suma prowizji (do diagnostyki/raportów)."""
        klik = self.klik_fee or Decimal('0')
        agent = self.agent_fee or Decimal('0')
        return klik + agent
