"""Celery tasks dla modułu cheques.

Tasks:
- notify_cheque_redeemed  — webhook POST {bank_url}/redeemed (po realizacji)
- notify_cheque_released  — webhook POST {bank_url}/released (po cancel/expire)
- expire_due_cheques      — cron: wygaszanie przeterminowanych czeków (co 60s)
"""

import logging

import httpx
from celery import shared_task
from django.db import transaction as db_transaction
from django.utils import timezone

logger = logging.getLogger('klik')

RETRY_COUNTDOWN = [5, 30, 120]  # 3 próby: 5s, 30s, 2min
HTTP_TIMEOUT = 10


# ---------------------------------------------------------------------------
# Webhook — czek zrealizowany
# ---------------------------------------------------------------------------


@shared_task(bind=True, max_retries=3, default_retry_delay=5)
def notify_cheque_redeemed(self, cheque_id: str):
    """Powiadom bank wystawcy że czek został zrealizowany."""
    from cheques.models import Cheque

    try:
        cheque = Cheque.objects.select_related('issuer_bank', 'transaction').get(id=cheque_id)
    except Cheque.DoesNotExist:
        logger.error('notify_cheque_redeemed: cheque %s not found', cheque_id)
        return

    webhook_base = cheque.get_effective_webhook_url()
    if not webhook_base:
        logger.warning('notify_cheque_redeemed: bank %s has no webhook url', cheque.issuer_bank_id)
        return

    url = f'{webhook_base}/redeemed'
    payload = {
        'cheque_id': str(cheque.id),
        'transaction_id': str(cheque.transaction_id) if cheque.transaction_id else None,
        'amount': str(cheque.amount),
        'currency': cheque.currency,
        'redeemed_at': cheque.redeemed_at.isoformat() if cheque.redeemed_at else None,
        'user_id': cheque.issuer_user_id,
    }

    try:
        resp = httpx.post(url, json=payload, timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
        logger.info('notify_cheque_redeemed: sent to %s → %s', url, resp.status_code)
    except Exception as exc:
        retry_num = self.request.retries
        countdown = RETRY_COUNTDOWN[retry_num] if retry_num < len(RETRY_COUNTDOWN) else 120
        logger.warning(
            'notify_cheque_redeemed: failed (attempt %d) → %s, retry in %ds',
            retry_num + 1, exc, countdown,
        )
        raise self.retry(exc=exc, countdown=countdown)


# ---------------------------------------------------------------------------
# Webhook — czek anulowany / wygasły → bank zwalnia hold
# ---------------------------------------------------------------------------


@shared_task(bind=True, max_retries=3, default_retry_delay=5)
def notify_cheque_released(self, cheque_id: str, reason: str):
    """Powiadom bank wystawcy że hold może zostać zwolniony (cancel lub expire)."""
    from cheques.models import Cheque

    try:
        cheque = Cheque.objects.select_related('issuer_bank').get(id=cheque_id)
    except Cheque.DoesNotExist:
        logger.error('notify_cheque_released: cheque %s not found', cheque_id)
        return

    webhook_base = cheque.get_effective_webhook_url()
    if not webhook_base:
        logger.warning('notify_cheque_released: bank %s has no webhook url', cheque.issuer_bank_id)
        return

    url = f'{webhook_base}/released'
    released_at = cheque.cancelled_at or cheque.expired_at
    payload = {
        'cheque_id': str(cheque.id),
        'amount': str(cheque.amount),
        'currency': cheque.currency,
        'reason': reason,
        'released_at': released_at.isoformat() if released_at else timezone.now().isoformat(),
        'user_id': cheque.issuer_user_id,
    }

    try:
        resp = httpx.post(url, json=payload, timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
        logger.info('notify_cheque_released: sent to %s → %s', url, resp.status_code)
    except Exception as exc:
        retry_num = self.request.retries
        countdown = RETRY_COUNTDOWN[retry_num] if retry_num < len(RETRY_COUNTDOWN) else 120
        logger.warning(
            'notify_cheque_released: failed (attempt %d) → %s, retry in %ds',
            retry_num + 1, exc, countdown,
        )
        raise self.retry(exc=exc, countdown=countdown)


# ---------------------------------------------------------------------------
# Cron — wygaszanie przeterminowanych czeków (CH3)
# ---------------------------------------------------------------------------


@shared_task
def expire_due_cheques():
    """
    Cron task (co 60s): wygasz czeki z expires_at <= now i status=ACTIVE.
    Limit 1000 per run — bezpiecznik przed eksplozją.
    """
    from cheques.models import Cheque, ChequeStatus

    now = timezone.now()
    expired_count = 0

    # Batch: SELECT FOR UPDATE SKIP LOCKED aby uniknąć kolizji z redeem/cancel
    cheques_to_expire = (
        Cheque.objects.filter(status=ChequeStatus.ACTIVE, expires_at__lte=now)
        .select_for_update(skip_locked=True)
        [:1000]
    )

    with db_transaction.atomic():
        ids_to_expire = list(cheques_to_expire.values_list('id', flat=True))

    for cheque_id in ids_to_expire:
        try:
            with db_transaction.atomic():
                cheque = Cheque.objects.select_for_update().get(
                    id=cheque_id, status=ChequeStatus.ACTIVE
                )
                cheque.status = ChequeStatus.EXPIRED
                cheque.expired_at = now
                cheque.save(update_fields=['status', 'expired_at', 'updated_at'])
                expired_count += 1

            # Webhook asynchronicznie — poza transakcją
            notify_cheque_released.delay(str(cheque_id), 'EXPIRED')

        except Cheque.DoesNotExist:
            # Race condition — ktoś już zrealizował/anulował → OK
            pass
        except Exception:
            logger.exception('expire_due_cheques: failed for cheque %s', cheque_id)

    if expired_count:
        logger.info('expire_due_cheques: expired %d cheques', expired_count)

    return expired_count
