"""Modele apki ledger — LedgerEntry, SettlementSession, SettlementTransfer."""

import uuid

from django.core.exceptions import ValidationError
from django.db import models

from common.enums import ZONE_CURRENCY, Zone
from ledger.enums import (
    BeneficiaryType,
    LedgerEntryType,
    SettlementSessionStatus,
    SettlementTransferStatus,
)


class SettlementSession(models.Model):
    """
    Okno czasowe nettingu dla danej strefy. Sesje powstają cyklicznie
    (Celery Beat) i agregują wszystkie LedgerEntry z `settled=False`
    i `session=NULL` z danej strefy.

    Cykl życia: OPEN → NETTING → SETTLING → COMPLETED (lub FAILED).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    zone = models.CharField(max_length=2, choices=Zone.choices, db_index=True)
    started_at = models.DateTimeField()
    ended_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(
        max_length=20,
        choices=SettlementSessionStatus.choices,
        default=SettlementSessionStatus.OPEN,
        db_index=True,
    )
    total_entries_count = models.IntegerField(default=0)
    total_volume = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=0,
        help_text='Suma kwot brutto wszystkich entries w sesji.',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-started_at']
        indexes = [
            models.Index(fields=['zone', 'status', 'started_at']),
        ]

    def __str__(self):
        return f'Session {self.id} ({self.zone}, {self.status})'


class LedgerEntry(models.Model):
    """
    Pojedynczy wpis księgowy: "Bank A jest winien {beneficjentowi} kwotę X
    w danej sesji". Beneficjent jest polimorficzny: BANK, KLIK lub AGENT.

    Idempotency przez `source_ref` — odwołanie do źródłowej operacji
    (idempotency_key transakcji C2B lub identyfikator agregacji P2P).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # Strony fizycznego transferu RTGS
    from_bank = models.ForeignKey(
        'banks.Bank',
        on_delete=models.PROTECT,
        related_name='ledger_entries_from',
        help_text='Bank-dłużnik (płaci przez RTGS).',
    )
    to_bank = models.ForeignKey(
        'banks.Bank',
        on_delete=models.PROTECT,
        related_name='ledger_entries_to',
        help_text='Bank fizycznie otrzymujący środki (gdzie KLIK/agent ma konto).',
    )

    # Logiczny beneficjent (komu należy się kawałek pieniędzy)
    beneficiary_type = models.CharField(
        max_length=10,
        choices=BeneficiaryType.choices,
    )
    beneficiary_ref = models.UUIDField(
        null=True,
        blank=True,
        help_text='Bank.id lub Agent.id; null gdy beneficiary_type=KLIK.',
    )

    # Kwota i kontekst
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    currency = models.CharField(max_length=3)
    zone = models.CharField(max_length=2, choices=Zone.choices, db_index=True)
    entry_type = models.CharField(
        max_length=20,
        choices=LedgerEntryType.choices,
    )

    # Audyt + idempotency
    source_ref = models.CharField(
        max_length=100,
        db_index=True,
        help_text='idempotency_key transakcji C2B lub id agregacji P2P. Klucz idempotency dla record_*.',
    )

    # Settlement tracking
    session = models.ForeignKey(
        SettlementSession,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='entries',
    )
    settled = models.BooleanField(default=False)
    settled_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        constraints = [
            models.CheckConstraint(
                condition=models.Q(amount__gt=0),
                name='ledger_amount_positive',
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(beneficiary_type='KLIK', beneficiary_ref__isnull=True)
                    | (~models.Q(beneficiary_type='KLIK') & models.Q(beneficiary_ref__isnull=False))
                ),
                name='ledger_beneficiary_consistency',
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(settled=False, settled_at__isnull=True)
                    | models.Q(settled=True, settled_at__isnull=False)
                ),
                name='ledger_settled_at_consistency',
            ),
        ]
        indexes = [
            models.Index(fields=['zone', 'settled', 'created_at']),
            models.Index(fields=['source_ref']),
            models.Index(fields=['session', 'settled']),
        ]

    def __str__(self):
        return (
            f'Entry {self.id} ({self.entry_type}, {self.amount} {self.currency}, '
            f'{self.from_bank_id} → {self.to_bank_id})'
        )

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def clean(self):
        """Walidacje cross-field."""
        super().clean()

        # Currency musi pasować do strefy
        expected_currency = ZONE_CURRENCY.get(self.zone)
        if expected_currency and self.currency != expected_currency:
            raise ValidationError(
                {
                    'currency': (
                        f'Strefa {self.zone} wymaga waluty {expected_currency}, '
                        f'otrzymano {self.currency}.'
                    )
                }
            )

        # Strefy banków muszą być zgodne ze strefą entry
        if self.from_bank_id and self.from_bank.zone != self.zone:
            raise ValidationError(
                {'from_bank': f'from_bank.zone ({self.from_bank.zone}) ≠ entry.zone ({self.zone}).'}
            )
        if self.to_bank_id and self.to_bank.zone != self.zone:
            raise ValidationError(
                {'to_bank': f'to_bank.zone ({self.to_bank.zone}) ≠ entry.zone ({self.zone}).'}
            )


class SettlementTransfer(models.Model):
    """
    Wynik nettingu sesji — pojedynczy przelew RTGS.

    Po nettingu z wielu LedgerEntry powstaje pojedynczy SettlementTransfer
    np. "Bank A → Bank B: 1500 PLN" (suma wszystkich zobowiązań A wobec B).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.ForeignKey(
        SettlementSession,
        on_delete=models.CASCADE,
        related_name='transfers',
    )
    from_bank = models.ForeignKey(
        'banks.Bank',
        on_delete=models.PROTECT,
        related_name='settlement_transfers_from',
    )
    to_bank = models.ForeignKey(
        'banks.Bank',
        on_delete=models.PROTECT,
        related_name='settlement_transfers_to',
    )
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    currency = models.CharField(max_length=3)
    status = models.CharField(
        max_length=20,
        choices=SettlementTransferStatus.choices,
        default=SettlementTransferStatus.PENDING,
    )
    rtgs_reference = models.CharField(
        max_length=100,
        blank=True,
        help_text='Identyfikator zwrócony przez RTGS po dispatch.',
    )

    failure_reason = models.CharField(
        max_length=255,
        blank=True,
        default='',
        help_text='Powód odrzucenia transferu prezez RTGS (wypełniany przy statusie FAILED).',
    )

    created_at = models.DateTimeField(auto_now_add=True)
    dispatched_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
        constraints = [
            models.CheckConstraint(
                condition=models.Q(amount__gt=0),
                name='transfer_amount_positive',
            ),
            models.CheckConstraint(
                condition=~models.Q(from_bank=models.F('to_bank')),
                name='transfer_from_to_different',
            ),
        ]
        indexes = [
            models.Index(fields=['session', 'status']),
        ]

    def __str__(self):
        return (
            f'Transfer {self.id} ({self.from_bank_id} → {self.to_bank_id}, '
            f'{self.amount} {self.currency}, {self.status})'
        )
