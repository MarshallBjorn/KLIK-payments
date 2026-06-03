"""Celery tasks dla apki codes."""

import logging

import requests
from celery import shared_task
from celery.exceptions import MaxRetriesExceededError
from django.utils import timezone

from codes.enums import RejectReason, TransactionStatus
from codes.models import Transaction
from codes.services import CodeService

logger = logging.getLogger('klik')

WEBHOOK_TIMEOUT_SECONDS = 30


@shared_task(
    bind=True,
    autoretry_for=(requests.RequestException,),
    retry_backoff=True,
    retry_backoff_max=30,
    retry_jitter=True,
    max_retries=3,
)
def authorize_webhook_task(self, transaction_id: str):
    """
    Wywołuje webhook autoryzacyjny banku nadawcy.

    Retry policy: 3 próby z exponential backoff.
    Po wyczerpaniu retry: status TIMEOUT.
    """
    try:
        transaction = Transaction.objects.select_related('sender_bank', 'merchant').get(
            id=transaction_id,
        )
    except Transaction.DoesNotExist:
        logger.error('Transaction %s nie istnieje przy webhook task', transaction_id)
        return

    if transaction.status != TransactionStatus.PENDING:
        logger.warning(
            'Transaction %s ma status %s (oczekiwane PENDING) — pomijam webhook',
            transaction_id,
            transaction.status,
        )
        return

    bank = transaction.sender_bank
    if not bank.webhook_url:
        logger.error('Bank %s nie ma webhook_url — TIMEOUT', bank.id)
        _mark_timeout(transaction)
        return

    payload = {
        'transaction_id': str(transaction.id),
        'user_id': transaction.user_id,
        'amount': str(transaction.amount_gross),
        'currency': transaction.currency,
        'merchant_name': transaction.merchant.name,
        'is_on_us': transaction.is_on_us,
        'expiry_time': (transaction.created_at + timezone.timedelta(seconds=120)).isoformat(),
        'zone': transaction.zone,
    }
    headers = {
        'Content-Type': 'application/json',
        'X-KLIK-Webhook-Source': 'klik-orchestrator',
    }

    try:
        response = requests.post(
            f'{bank.webhook_url}/authorize',
            json=payload,
            headers=headers,
            timeout=WEBHOOK_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        logger.warning(
            'Webhook do banku %s failed (próba %d/3): %s',
            bank.id,
            self.request.retries + 1,
            exc,
        )
        try:
            raise self.retry(exc=exc) from exc
        except MaxRetriesExceededError:
            logger.error(
                'Webhook do banku %s exceded max retries — TIMEOUT',
                bank.id,
            )
            _mark_timeout(transaction)
            return

    # Sukces — bank odpowiedział 200. Bank potem wywoła /payments/confirm.
    # Tutaj nie zmieniamy statusu na AUTHORIZED — to /confirm zrobi.
    logger.info(
        'Webhook do banku %s OK dla transaction %s',
        bank.id,
        transaction_id,
    )


def _mark_timeout(transaction: Transaction):
    """Oznacza transakcję jako TIMEOUT i aktualizuje cache."""
    transaction.status = TransactionStatus.TIMEOUT
    transaction.reject_reason = RejectReason.OTHER
    transaction.save(update_fields=['status', 'reject_reason'])

    service = CodeService()
    service.cache_transaction_status(
        str(transaction.id),
        TransactionStatus.TIMEOUT,
    )
