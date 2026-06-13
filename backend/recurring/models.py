"""Modele apki recurring — RecurringTransfer (Mandate) + RecurringExecution (Run).

ERD: docs/reccuring/diagrams/STATE.md (C-R1). Maszyny stanów: B-R1, B-R2.

RecurringTransfer to długo żyjący mandate (tygodnie/miesiące/lata) — definicja
zlecenia stałego podpisanego PIN-em u banku wystawcy. RecurringExecution to
pojedyncze wykonanie (run) generowane przez cron KLIK — audit trail
"co kiedy poszło".

Świadomie BRAK FK z `recipient_phone` do `Alias.phone` — alias może zostać
usunięty niezależnie od mandate-ów (patrz STATE.md, "Uwagi do modelu" pkt 2).
Zamiast tego `RecurringExecution.lookup_response_snapshot` zachowuje dane
aliasu z momentu wykonania.
"""

from django.db import models

from common.enums import TimestampedModel, Zone


class RecurringCycle(models.TextChoices):
    DAILY = 'DAILY', 'Codziennie'
    WEEKLY = 'WEEKLY', 'Co tydzień'
    MONTHLY = 'MONTHLY', 'Co miesiąc'


class RecurringTransferStatus(models.TextChoices):
    ACTIVE = 'ACTIVE', 'Aktywny'
    PAUSED = 'PAUSED', 'Wstrzymany'
    CANCELLED = 'CANCELLED', 'Anulowany'
    COMPLETED = 'COMPLETED', 'Zakończony'


class RecurringExecutionStatus(models.TextChoices):
    SCHEDULED = 'SCHEDULED', 'Zaplanowany'
    EXECUTING = 'EXECUTING', 'W trakcie'
    SUCCESS = 'SUCCESS', 'Sukces'
    FAILED = 'FAILED', 'Nieudany'
    SKIPPED = 'SKIPPED', 'Pominięty'


class ExecutionFailureReason(models.TextChoices):
    """Powody nieudanej execution.

    Pierwsze cztery przychodzą od banku w odpowiedzi REJECTED,
    RECIPIENT_ALIAS_GONE i NETWORK_TIMEOUT są KLIK-internal.
    """

    INSUFFICIENT_FUNDS = 'INSUFFICIENT_FUNDS', 'Brak środków'
    MANDATE_REVOKED_LOCALLY = 'MANDATE_REVOKED_LOCALLY', 'Mandate odwołany w banku'
    ACCOUNT_CLOSED = 'ACCOUNT_CLOSED', 'Konto zamknięte'
    AML_BLOCK = 'AML_BLOCK', 'Blokada AML'
    RECIPIENT_ALIAS_GONE = 'RECIPIENT_ALIAS_GONE', 'Alias odbiorcy usunięty'
    NETWORK_TIMEOUT = 'NETWORK_TIMEOUT', 'Timeout sieci do banku'
    OTHER = 'OTHER', 'Inny'


# Stany terminalne mandate-a — brak przejść wyjściowych.
RECURRING_TERMINAL_STATUSES = (
    RecurringTransferStatus.CANCELLED,
    RecurringTransferStatus.COMPLETED,
)


class RecurringTransfer(TimestampedModel):
    """Zlecenie stałe (mandate): kto płaci, komu (alias P2P), ile, jak często.

    Tworzony raz przez `POST /recurring/create` po podpisaniu PIN-em u banku
    wystawcy. Kolejne wykonania nie wymagają potwierdzenia klienta.
    Cykl życia: ACTIVE ↔ PAUSED → CANCELLED | COMPLETED.
    """

    payer_bank = models.ForeignKey(
        'banks.Bank',
        on_delete=models.PROTECT,
        related_name='recurring_transfers',
        help_text='Bank nadawcy — właściciel mandate (wywołał /recurring/create).',
    )
    payer_user_id = models.CharField(
        max_length=200,
        help_text='Wewnętrzny identyfikator klienta po stronie banku nadawcy.',
    )

    recipient_phone = models.CharField(
        max_length=16,  # E.164 max 15 cyfr + znak '+'
        help_text='Alias odbiorcy w E.164. Świadomie bez FK do Alias (patrz docstring modułu).',
    )

    amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        help_text='Stała kwota pojedynczego przelewu. Niezmienna po utworzeniu.',
    )
    currency = models.CharField(max_length=3)
    zone = models.CharField(max_length=2, choices=Zone.choices)

    cycle = models.CharField(max_length=10, choices=RecurringCycle.choices)
    start_date = models.DateField()
    end_date = models.DateField(
        null=True,
        blank=True,
        help_text='NULL = mandate open-ended (bez daty końcowej).',
    )

    next_run_at = models.DateTimeField(
        help_text='Czas najbliższego runu. Kalkulowany z cycle, zakotwiczony w start_date.',
    )
    last_run_at = models.DateTimeField(null=True, blank=True)
    last_execution = models.ForeignKey(
        'recurring.RecurringExecution',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='+',
        help_text='Ostatnia zakończona execution (SUCCESS).',
    )

    status = models.CharField(
        max_length=10,
        choices=RecurringTransferStatus.choices,
        default=RecurringTransferStatus.ACTIVE,
        db_index=True,
    )

    failed_runs_count = models.PositiveIntegerField(
        default=0,
        help_text='Licznik failów Z RZĘDU. Reset przy SUCCESS oraz przy resume.',
    )

    mandate_signed_at = models.DateTimeField(
        help_text='Pole audytowe z payloadu /create — kiedy klient podpisał PIN-em. KLIK ufa bankowi.',
    )

    idempotency_key = models.CharField(
        max_length=64,
        help_text='Idempotency-Key z POST /recurring/create.',
    )

    paused_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            # Cron query co RECURRING_DISPATCH_INTERVAL_SECONDS — najczęstszy
            # access pattern. Partial: dispatch filtruje status=ACTIVE.
            models.Index(
                fields=['next_run_at'],
                name='recurring_dispatch_idx',
                condition=models.Q(status='ACTIVE'),
            ),
            # Listing mandate-ów klienta (GET /recurring?payer_user_id=).
            models.Index(
                fields=['payer_bank', 'payer_user_id', 'status'],
                name='recurring_payer_idx',
            ),
        ]
        constraints = [
            # Idempotency lookup dla /create — replay tego samego klucza przez
            # ten sam bank zwraca istniejący mandate.
            models.UniqueConstraint(
                fields=['idempotency_key', 'payer_bank'],
                name='recurring_idempotency_unique',
            ),
            models.CheckConstraint(
                name='recurring_amount_positive',
                condition=models.Q(amount__gt=0),
            ),
        ]
        verbose_name = 'Zlecenie stałe'
        verbose_name_plural = 'Zlecenia stałe'

    def __str__(self):
        return (
            f'Mandate {self.id} ({self.status}) — {self.amount} {self.currency} '
            f'{self.cycle} → {self.recipient_phone}'
        )

    @property
    def is_active(self):
        return self.status == RecurringTransferStatus.ACTIVE

    def get_effective_webhook_url(self):
        """Zwraca bazowy URL dla webhooków recurring.

        Priorytet: bank.recurring_webhook_url → bank.webhook_url + '/recurring'
        (spójnie z Cheque.get_effective_webhook_url).
        """
        bank = self.payer_bank
        if bank.recurring_webhook_url:
            return bank.recurring_webhook_url.rstrip('/')
        if bank.webhook_url:
            return bank.webhook_url.rstrip('/') + '/recurring'
        return None


class RecurringExecution(TimestampedModel):
    """Pojedyncze wykonanie mandate-a w określonym dniu.

    Krótki cykl życia (zwykle <30s):
    SCHEDULED → EXECUTING → SUCCESS | FAILED, lub SCHEDULED → SKIPPED
    (race z pause/cancel).
    """

    recurring_transfer = models.ForeignKey(
        RecurringTransfer,
        on_delete=models.CASCADE,
        related_name='executions',
    )

    scheduled_for = models.DateTimeField(
        help_text='Kiedy run miał być wykonany (= mandate.next_run_at w momencie dispatch).',
    )
    executed_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text='Kiedy bank faktycznie wykonał przelew RTP (z odpowiedzi webhooka).',
    )

    status = models.CharField(
        max_length=10,
        choices=RecurringExecutionStatus.choices,
        default=RecurringExecutionStatus.SCHEDULED,
        db_index=True,
    )

    rtp_reference = models.CharField(
        max_length=200,
        blank=True,
        default='',
        help_text='ID przelewu RTP od banku (przy SUCCESS).',
    )
    failure_reason = models.CharField(
        max_length=30,
        blank=True,
        default='',
        choices=ExecutionFailureReason.choices,
        help_text='Wypełniany przy FAILED. Pusty dla SKIPPED (nie liczy się do auto-pause).',
    )

    lookup_response_snapshot = models.JSONField(
        null=True,
        blank=True,
        help_text='Dane aliasu odbiorcy z lookupu w momencie execution (audit).',
    )

    class Meta:
        ordering = ['-scheduled_for']
        indexes = [
            # Listing executions (GET /recurring/{id}/executions).
            models.Index(
                fields=['recurring_transfer', '-scheduled_for'],
                name='recurring_exec_mandate_idx',
            ),
            # Cleanup osieroconych executions (worker padł między INSERT a UPDATE).
            models.Index(
                fields=['status'],
                name='recurring_exec_scheduled_idx',
                condition=models.Q(status='SCHEDULED'),
            ),
        ]
        verbose_name = 'Wykonanie zlecenia'
        verbose_name_plural = 'Wykonania zleceń'

    def __str__(self):
        return f'Execution {self.id} ({self.status}) mandate={self.recurring_transfer_id}'
