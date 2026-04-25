import uuid
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from common.enums import Zone


class Agent(models.Model):
    """
    Agent rozliczeniowy — pośrednik między sklepem/bramką a KLIK.
    Pobiera prowizję od transakcji zgodnie z aktywnym MSCAgreement.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=200)
    api_key_hash = models.CharField(
        max_length=128,
        unique=True,
        db_index=True,
        help_text='SHA-256 hash kluczu API. Plaintext nie jest przechowywany.',
    )
    settlement_bank = models.ForeignKey(
        models.Bank,
        on_delete=models.PROTECT,
        related_name='agents',
        help_text='Bank w którym agent ma konto rozliczeniowe.',
    )
    iban = models.CharField(max_length=34)
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
        """Walidacja: agent musi być w tej samej strefie co jego settlement_bank."""
        super().clean()
        if self.settlement_bank_id and self.zone != self.settlement_bank.zone:
            raise ValidationError(
                {
                    'zone': (
                        f'Strefa agenta ({self.zone}) musi być zgodna ze strefą '
                        f'settlement_bank ({self.settlement_bank.zone}).'
                    )
                }
            )


class MSCAgreement(models.Model):
    """
    Merchant Service Charge Agreement — umowa określająca stawki prowizji
    dla agenta. Agent może mieć wiele umów w czasie (zmiana stawek), ale
    aktywne okresy nie mogą się nakładać.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    agent = models.ForeignKey(
        Agent,
        on_delete=models.CASCADE,
        related_name='msc_agreements',
    )
    klik_fee_perc = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        help_text='Procent prowizji dla KLIK od kwoty brutto (np. 0.30 = 0.30%).',
    )
    agent_fee_perc = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        help_text='Procent prowizji dla agenta od kwoty brutto (np. 1.00 = 1.00%).',
    )
    valid_from = models.DateTimeField()
    valid_to = models.DateTimeField(
        null=True,
        blank=True,
        help_text='Null = bezterminowa.',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-valid_from']
        constraints = [
            models.CheckConstraint(
                condition=models.Q(valid_to__isnull=True)
                | models.Q(valid_to__gt=models.F('valid_from')),
                name='msc_valid_to_after_valid_from',
            ),
            models.CheckConstraint(
                condition=models.Q(klik_fee_perc__gte=0) & models.Q(agent_fee_perc__gte=0),
                name='msc_fees_non_negative',
            ),
        ]

    def __str__(self):
        return (
            f'MSC {self.agent.name}: KLIK {self.klik_fee_perc}% + '
            f'agent {self.agent_fee_perc}% (od {self.valid_from})'
        )

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def is_active_at(self, when=None) -> bool:
        """Czy umowa jest aktywna w danej chwili (default: now)."""
        when = when or timezone.now()
        if when < self.valid_from:
            return False
        if self.valid_to is not None and when >= self.valid_to:
            return False
        return True

    def clean(self):
        """Walidacja: suma prowizji <100% i brak overlap z innymi umowami."""
        super().clean()

        # Sprawdzenie sumy prowizji
        klik = self.klik_fee_perc or Decimal('0')
        agent = self.agent_fee_perc or Decimal('0')
        if klik + agent >= Decimal('100'):
            raise ValidationError(f'Suma prowizji ({klik + agent}%) musi być mniejsza niż 100%.')

        # Sprawdzenie overlap z innymi umowami tego agenta
        if self.agent_id and self.valid_from:
            overlapping = MSCAgreement.objects.filter(agent_id=self.agent_id)
            if self.pk:
                overlapping = overlapping.exclude(pk=self.pk)

            for other in overlapping:
                if self._overlaps_with(other):
                    raise ValidationError(
                        f'Okres tej umowy nakłada się z istniejącą umową: {other}'
                    )

    def _overlaps_with(self, other: 'MSCAgreement') -> bool:
        """Sprawdza czy okres tej umowy nakłada się z inną."""
        # Self: [self.valid_from, self.valid_to or +inf)
        # Other: [other.valid_from, other.valid_to or +inf)
        # Brak overlap gdy: self kończy się przed startem other LUB self startuje po końcu other
        self_end_before_other_start = (
            self.valid_to is not None and self.valid_to <= other.valid_from
        )
        self_start_after_other_end = (
            other.valid_to is not None and self.valid_from >= other.valid_to
        )
        return not (self_end_before_other_start or self_start_after_other_end)
