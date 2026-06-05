"""Modele apki cheques — Cheque."""

import uuid

from django.db import models

from common.enums import Zone


class ChequeStatus(models.TextChoices):
    ACTIVE = 'ACTIVE', 'Aktywny'
    REDEEMED = 'REDEEMED', 'Zrealizowany'
    CANCELLED = 'CANCELLED', 'Anulowany'
    EXPIRED = 'EXPIRED', 'Wygasły'


class Cheque(models.Model):
    """
    Czek KLIK — 9-cyfrowy kod o ustalonej kwocie i TTL 1h–72h.

    Wystawiany przez bank (który uprzednio blokuje środki klienta).
    Realizowany jednorazowo przez agenta w punkcie sprzedaży.
    Cykl życia: ACTIVE → REDEEMED | CANCELLED | EXPIRED.

    Partial unique index na (code, status=ACTIVE) — ten sam kod może
    istnieć w historii wielokrotnie (po EXPIRED/CANCELLED), ale tylko
    jeden ACTIVE naraz.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # 9-cyfrowy kod pokazywany klientowi
    code = models.CharField(
        max_length=9,
        db_index=True,
        help_text='9-cyfrowy kod czeku. Unikalny wśród ACTIVE (partial index).',
    )

    # Wystawca
    issuer_bank = models.ForeignKey(
        'banks.Bank',
        on_delete=models.PROTECT,
        related_name='issued_cheques',
        help_text='Bank który wystawił czek (wywołał /cheques/issue).',
    )
    issuer_user_id = models.CharField(
        max_length=200,
        help_text='Wewnętrzny identyfikator klienta po stronie banku wystawcy.',
    )

    # Kwota i waluta
    amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        help_text='Kwota czeku. Niezmienna po wystawieniu.',
    )
    currency = models.CharField(max_length=3)
    zone = models.CharField(max_length=2, choices=Zone.choices)

    # Stan
    status = models.CharField(
        max_length=10,
        choices=ChequeStatus.choices,
        default=ChequeStatus.ACTIVE,
        db_index=True,
    )

    # TTL
    issued_at = models.DateTimeField()
    expires_at = models.DateTimeField(db_index=True)

    # Terminalny — uzupełniane przy zmianie stanu
    redeemed_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    expired_at = models.DateTimeField(null=True, blank=True)

    # FK do Transaction — ustawiane przy /redeem
    transaction = models.OneToOneField(
        'codes.Transaction',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='cheque',
        help_text='Transakcja powstała przy realizacji czeku.',
    )

    # Idempotency dla /issue
    idempotency_key = models.CharField(
        max_length=64,
        db_index=True,
        help_text='Idempotency-Key z POST /cheques/issue.',
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['status', 'expires_at']),
            models.Index(fields=['issuer_bank', 'status', '-created_at']),
            models.Index(fields=['idempotency_key', 'issuer_bank']),
        ]
        verbose_name = 'Czek'
        verbose_name_plural = 'Czeki'

    def __str__(self):
        return f'Czek {self.code} ({self.status}) — {self.amount} {self.currency}'

    @property
    def is_active(self):
        return self.status == ChequeStatus.ACTIVE

    def get_effective_webhook_url(self):
        """Zwraca URL dla webhooków cheques.

        Priorytet: bank.cheques_webhook_url → bank.webhook_url + '/cheques'
        """
        bank = self.issuer_bank
        if bank.cheques_webhook_url:
            return bank.cheques_webhook_url.rstrip('/')
        if bank.webhook_url:
            return bank.webhook_url.rstrip('/') + '/cheques'
        return None
