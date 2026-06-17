"""Celery tasks dla modułu recurring.

Tasks:
- dispatch_due_recurring_transfers — cron Beat (co RECURRING_DISPATCH_INTERVAL_SECONDS):
  wybiera mandate-y z next_run_at <= now i kolejkuje per-mandate subtaski (R2 ETAP 1-2)
- execute_recurring_run            — pojedynczy run: lookup aliasu (PŁATNY) +
  webhook /execute do banku nadawcy + zapis wyniku (R2 ETAP 3-5)
- notify_recurring_auto_paused     — webhook POST {bank_url}/auto-paused (R5)
- notify_recurring_cancelled       — webhook POST {bank_url}/cancelled (R4/R6,
  reason: USER_REQUEST / MANDATE_REVOKED_LOCALLY / ACCOUNT_CLOSED / END_DATE_REACHED)

Model rozliczeniowy: KLIK nie uczestniczy w transferze środków. Bank nadawcy
wykonuje przelew RTP poza KLIK. Jedyny ślad w ledgerze to P2P_LOOKUP_FEE —
counter w Redisie inkrementowany przez AliasService.lookup_for_bank (ten sam
mechanizm i klucz co ad-hoc P2P; agregacja w P4 daily accrual).
"""

import logging

import httpx
from celery import shared_task
from django.conf import settings
from django.db import transaction as db_transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

logger = logging.getLogger('klik')

RETRY_COUNTDOWN = [5, 30, 120]  # 3 próby: 5s, 30s, 2min (jak cheques)
NOTIFY_HTTP_TIMEOUT = 10
EXECUTE_HTTP_TIMEOUT = 30  # webhook /execute — bank robi przelew RTP synchronicznie

DISPATCH_BATCH_LIMIT = 1000

# reject_reason dozwolone od banku w odpowiedzi REJECTED. Nieznane → OTHER.
ALLOWED_REJECT_REASONS = frozenset(
    {
        'INSUFFICIENT_FUNDS',
        'MANDATE_REVOKED_LOCALLY',
        'ACCOUNT_CLOSED',
        'AML_BLOCK',
        'OTHER',
    }
)
# reject_reason → mandate CANCELLED od razu (klient odwołał w banku / konto zamknięte)
CANCELLING_REASONS = frozenset({'MANDATE_REVOKED_LOCALLY', 'ACCOUNT_CLOSED'})


# ---------------------------------------------------------------------------
# Cron — dispatch due mandate-ów (R2 ETAP 1-2)
# ---------------------------------------------------------------------------


@shared_task(name='recurring.dispatch_due_recurring_transfers')
def dispatch_due_recurring_transfers():
    """Co RECURRING_DISPATCH_INTERVAL_SECONDS: kolejkuje runy due mandate-ów.

    FOR UPDATE SKIP LOCKED pozwala uruchomić wielu workerów równolegle na tym
    samym Beat tick — każdy bierze inną porcję bez zakleszczenia. Limit 1000
    per tick — bezpiecznik (reszta wejdzie w kolejnym ticku za 5 min, SLA
    recurring to "tego dnia", nie "tej minuty").
    """
    from recurring.models import RecurringTransfer, RecurringTransferStatus

    now = timezone.now()

    with db_transaction.atomic():
        due_ids = list(
            RecurringTransfer.objects.filter(
                status=RecurringTransferStatus.ACTIVE,
                next_run_at__lte=now,
            )
            .select_for_update(skip_locked=True)
            .values_list('id', flat=True)[:DISPATCH_BATCH_LIMIT]
        )

    # Enqueue po zwolnieniu locków — każdy mandate w osobnym subtaskcie.
    for mandate_id in due_ids:
        execute_recurring_run.delay(str(mandate_id))

    if due_ids:
        logger.info('recurring dispatch: queued %d runs', len(due_ids))
    return len(due_ids)


# ---------------------------------------------------------------------------
# Pojedynczy run (R2 ETAP 3-5)
# ---------------------------------------------------------------------------


@shared_task(bind=True, max_retries=3, name='recurring.execute_recurring_run')
def execute_recurring_run(self, mandate_id: str, execution_id: str | None = None):
    """Wykonuje pojedynczy run mandate-a.

    Pierwsze wywołanie (execution_id=None):
      1. Claim: tworzy RecurringExecution (SCHEDULED→EXECUTING) i od razu
         przesuwa mandate.next_run_at na kolejny slot. next_run_at jest
         aktualizowane NIEZALEŻNIE od wyniku (fail nie zatrzymuje cyklu),
         a wczesny claim chroni przed podwójnym dispatch przy wolnym runie.
      2. Świeży lookup aliasu odbiorcy przez AliasService.lookup_for_bank —
         inkrementuje counter P2P banku nadawcy (PŁATNY). Snapshot wyniku
         trafia do execution.lookup_response_snapshot.
    Retry po network failure (execution_id podany):
      - reużywa istniejącą execution i snapshot lookupu (lookup NIE jest
        powtarzany — bank nie płaci drugi raz, webhook leci z tym samym
        execution_id, bank wykrywa duplikat).
    """
    from recurring.models import RecurringExecution, RecurringExecutionStatus

    if execution_id is None:
        execution = _claim_execution(mandate_id)
        if execution is None:
            return  # mandate już nie-ACTIVE / claimed przez innego workera / completed
        recipient = _lookup_recipient(execution)
        if recipient is None:
            return  # alias zniknął — execution sfinalizowana jako FAILED
    else:
        # Retry — odtwórz kontekst z DB.
        try:
            execution = RecurringExecution.objects.select_related(
                'recurring_transfer__payer_bank'
            ).get(id=execution_id)
        except RecurringExecution.DoesNotExist:
            logger.error('execute_recurring_run: execution %s not found on retry', execution_id)
            return
        if execution.status != RecurringExecutionStatus.EXECUTING:
            return  # już sfinalizowana (np. równoległy retry)
        recipient = execution.lookup_response_snapshot

    _call_bank_and_finalize(self, execution, recipient)


def _claim_execution(mandate_id: str):
    """ETAP 3: atomowy claim runu. Zwraca świeżą execution (EXECUTING) lub None."""
    from recurring import schedule
    from recurring.models import (
        RecurringExecution,
        RecurringExecutionStatus,
        RecurringTransfer,
        RecurringTransferStatus,
    )

    now = timezone.now()

    with db_transaction.atomic():
        try:
            mandate = (
                RecurringTransfer.objects.select_for_update()
                .select_related('payer_bank')
                .get(id=mandate_id)
            )
        except RecurringTransfer.DoesNotExist:
            logger.error('execute_recurring_run: mandate %s not found', mandate_id)
            return None

        if mandate.status != RecurringTransferStatus.ACTIVE:
            # Race z pause/cancel — pause/cancel wygrał. Ewentualne wiszące
            # SCHEDULED (worker padł między INSERT a UPDATE) → SKIPPED.
            mandate.executions.filter(status=RecurringExecutionStatus.SCHEDULED).update(
                status=RecurringExecutionStatus.SKIPPED,
                updated_at=now,
            )
            return None

        if mandate.next_run_at > now:
            return None  # claimed przez innego workera w tym samym ticku

        # Guard: end_date minął zanim run wszedł (np. seria failów przesunęła
        # next_run_at poza end_date) → naturalne zakończenie bez runu.
        if mandate.end_date and mandate.next_run_at.date() > mandate.end_date:
            mandate.status = RecurringTransferStatus.COMPLETED
            mandate.save(update_fields=['status', 'updated_at'])
            notify_recurring_cancelled.delay(str(mandate.id), 'END_DATE_REACHED')
            return None

        scheduled_for = mandate.next_run_at

        # INSERT(SCHEDULED) → UPDATE(EXECUTING) w jednej tx — okno SCHEDULED
        # jest milisekundami (zgodnie z B-R2), ale stan istnieje w audit trail.
        execution = RecurringExecution.objects.create(
            recurring_transfer=mandate,
            scheduled_for=scheduled_for,
            status=RecurringExecutionStatus.SCHEDULED,
        )
        execution.status = RecurringExecutionStatus.EXECUTING
        execution.save(update_fields=['status', 'updated_at'])

        # Advance — kolejny slot liczony od scheduled_for (kotwica w start_date,
        # brak driftu).
        mandate.next_run_at = schedule.compute_next_run_at(
            start_date=mandate.start_date,
            cycle=mandate.cycle,
            after=scheduled_for,
        )
        mandate.save(update_fields=['next_run_at', 'updated_at'])

    # Po wyjściu z tx — przeładowane relacje dla dalszych etapów.
    execution.recurring_transfer = mandate
    return execution


def _lookup_recipient(execution) -> dict | None:
    """ETAP 4: świeży lookup aliasu odbiorcy — PŁATNY (counter P2P +1).

    Reużywamy AliasService.lookup_for_bank: ten sam mechanizm i klucz Redis
    (aliases:lookups:{bank_id}:YYYYMMDD) co ad-hoc P2P — daily accrual P4
    agreguje recurring i zwykłe lookupy razem. Counter inkrementowany TYLKO
    przy znalezieniu aliasu (miss = 404 = nie naliczamy).
    """
    from aliases.services import AliasService
    from aliases.services.exceptions import AliasDoesNotExistError
    from recurring.models import ExecutionFailureReason

    mandate = execution.recurring_transfer

    try:
        alias = AliasService().lookup_for_bank(
            querying_bank=mandate.payer_bank,
            phone=mandate.recipient_phone,
        )
    except AliasDoesNotExistError:
        # Alias odbiorcy usunięty z KLIK między runami. Counter NIE
        # inkrementowany (lookup nie znalazł = 404).
        logger.warning(
            'execute_recurring_run: alias gone phone=%s mandate=%s',
            mandate.recipient_phone,
            mandate.id,
        )
        _finalize_failure(execution.id, ExecutionFailureReason.RECIPIENT_ALIAS_GONE)
        return None

    recipient = {
        'phone': alias.phone,
        'bank_id': str(alias.bank.id),
        'bank_code': alias.bank.name,
        'account_identifier': alias.account_identifier,
    }

    # Snapshot do audytu + do reużycia przy retry (bez ponownego naliczania).
    execution.lookup_response_snapshot = recipient
    execution.save(update_fields=['lookup_response_snapshot', 'updated_at'])
    return recipient


def _call_bank_and_finalize(task, execution, recipient: dict):
    """ETAP 5: webhook /execute do banku nadawcy + zapis wyniku.

    Bank MUSI wykonać przelew RTP przed odpowiedzeniem EXECUTED — KLIK ufa.
    Network failure → retry 5s/30s/2min z tym samym execution_id, po
    wyczerpaniu prób execution → FAILED(NETWORK_TIMEOUT).
    """
    from recurring.models import ExecutionFailureReason

    mandate = execution.recurring_transfer
    webhook_base = mandate.get_effective_webhook_url()
    if not webhook_base:
        # Bank bez webhooka nie powinien mieć recurring_enabled — traktujemy
        # jak permanentny network failure bez retry.
        logger.error(
            'execute_recurring_run: bank %s has no recurring webhook url',
            mandate.payer_bank_id,
        )
        _finalize_failure(execution.id, ExecutionFailureReason.NETWORK_TIMEOUT)
        return

    url = f'{webhook_base}/execute'
    payload = {
        'recurring_transfer_id': str(mandate.id),
        'execution_id': str(execution.id),
        'payer_user_id': mandate.payer_user_id,
        'amount': str(mandate.amount),
        'currency': mandate.currency,
        'scheduled_for': execution.scheduled_for.isoformat(),
        'mandate_signed_at': mandate.mandate_signed_at.isoformat(),
        'recipient': recipient,
    }

    try:
        resp = httpx.post(url, json=payload, timeout=EXECUTE_HTTP_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        result_status = data.get('status')
        if result_status not in ('EXECUTED', 'REJECTED'):
            raise ValueError(f'Niepoprawny status w odpowiedzi banku: {result_status!r}')
    except Exception as exc:
        retry_num = task.request.retries
        if retry_num >= task.max_retries:
            logger.error(
                'execute_recurring_run: bank unreachable after %d attempts, execution=%s',
                retry_num + 1,
                execution.id,
            )
            _finalize_failure(execution.id, ExecutionFailureReason.NETWORK_TIMEOUT)
            return
        countdown = RETRY_COUNTDOWN[min(retry_num, len(RETRY_COUNTDOWN) - 1)]
        logger.warning(
            'execute_recurring_run: webhook failed (attempt %d) → %s, retry in %ds',
            retry_num + 1,
            exc,
            countdown,
        )
        # Retry z tym samym execution_id — bank wykryje duplikat po execution_id.
        raise task.retry(
            exc=exc,
            countdown=countdown,
            kwargs={'mandate_id': str(mandate.id), 'execution_id': str(execution.id)},
        ) from exc

    if result_status == 'EXECUTED':
        _finalize_success(
            execution.id,
            rtp_reference=str(data.get('rtp_reference') or ''),
            executed_at_raw=data.get('executed_at'),
        )
    else:
        reject_reason = data.get('reject_reason')
        if reject_reason not in ALLOWED_REJECT_REASONS:
            logger.warning(
                'execute_recurring_run: unknown reject_reason %r from bank %s → OTHER',
                reject_reason,
                mandate.payer_bank_id,
            )
            reject_reason = ExecutionFailureReason.OTHER
        _finalize_failure(execution.id, reject_reason)


def _finalize_success(execution_id, *, rtp_reference: str, executed_at_raw):
    """Happy path: execution SUCCESS + update mandate + ewentualny COMPLETED (R6)."""
    from recurring.models import (
        RecurringExecution,
        RecurringExecutionStatus,
        RecurringTransfer,
        RecurringTransferStatus,
    )

    now = timezone.now()
    executed_at = (parse_datetime(executed_at_raw) if executed_at_raw else None) or now

    completed_mandate_id = None
    with db_transaction.atomic():
        execution = RecurringExecution.objects.select_for_update().get(id=execution_id)
        execution.status = RecurringExecutionStatus.SUCCESS
        execution.executed_at = executed_at
        execution.rtp_reference = rtp_reference
        execution.save(update_fields=['status', 'executed_at', 'rtp_reference', 'updated_at'])

        mandate = RecurringTransfer.objects.select_for_update().get(
            id=execution.recurring_transfer_id
        )
        mandate.last_run_at = now
        mandate.last_execution = execution
        mandate.failed_runs_count = 0  # reset przy każdym SUCCESS
        update_fields = ['last_run_at', 'last_execution', 'failed_runs_count', 'updated_at']

        # R6 — naturalne zakończenie: wszystkie planowane runy wykonane.
        # next_run_at został już przesunięty przy claimie.
        if (
            mandate.status == RecurringTransferStatus.ACTIVE
            and mandate.end_date
            and mandate.next_run_at.date() > mandate.end_date
        ):
            mandate.status = RecurringTransferStatus.COMPLETED
            update_fields.append('status')
            completed_mandate_id = str(mandate.id)

        mandate.save(update_fields=update_fields)

    if completed_mandate_id:
        notify_recurring_cancelled.delay(completed_mandate_id, 'END_DATE_REACHED')

    logger.info(
        'recurring run success: execution=%s mandate=%s rtp_ref=%s',
        execution_id,
        execution.recurring_transfer_id,
        rtp_reference,
    )


def _finalize_failure(execution_id, reason: str):
    """Reject path: execution FAILED + klasyfikacja reason (R2/R5).

    - MANDATE_REVOKED_LOCALLY / ACCOUNT_CLOSED → mandate CANCELLED od razu
    - AML_BLOCK → mandate PAUSED + alert
    - reszta → failed_runs_count++, auto-pause przy threshold
    """
    from recurring.models import (
        RecurringExecution,
        RecurringExecutionStatus,
        RecurringTransfer,
        RecurringTransferStatus,
    )

    now = timezone.now()
    threshold = settings.RECURRING_AUTO_PAUSE_FAILURE_THRESHOLD

    notify = None  # (task, args) — enqueue po commit
    with db_transaction.atomic():
        execution = RecurringExecution.objects.select_for_update().get(id=execution_id)
        execution.status = RecurringExecutionStatus.FAILED
        execution.failure_reason = reason
        execution.save(update_fields=['status', 'failure_reason', 'updated_at'])

        mandate = RecurringTransfer.objects.select_for_update().get(
            id=execution.recurring_transfer_id
        )
        mandate.failed_runs_count += 1
        update_fields = ['failed_runs_count', 'updated_at']

        # Klasyfikacja — zmieniamy stan tylko jeśli mandate dalej ACTIVE
        # (mógł zostać w międzyczasie spauzowany/anulowany przez bank).
        if mandate.status == RecurringTransferStatus.ACTIVE:
            if reason in CANCELLING_REASONS:
                mandate.status = RecurringTransferStatus.CANCELLED
                mandate.cancelled_at = now
                update_fields += ['status', 'cancelled_at']
                notify = (notify_recurring_cancelled, (str(mandate.id), reason))
            elif reason == 'AML_BLOCK':
                # Alert dla operatora KLIK — przez log ERROR (MVP).
                logger.error(
                    'recurring AML_BLOCK: mandate=%s bank=%s — mandate paused, '
                    'wymaga uwagi operatora',
                    mandate.id,
                    mandate.payer_bank_id,
                )
                mandate.status = RecurringTransferStatus.PAUSED
                mandate.paused_at = now
                update_fields += ['status', 'paused_at']
                notify = (notify_recurring_auto_paused, (str(mandate.id), reason))
            elif mandate.failed_runs_count >= threshold:
                mandate.status = RecurringTransferStatus.PAUSED
                mandate.paused_at = now
                update_fields += ['status', 'paused_at']
                notify = (notify_recurring_auto_paused, (str(mandate.id), reason))

        mandate.save(update_fields=update_fields)

    if notify:
        task, args = notify
        task.delay(*args)

    logger.info(
        'recurring run failed: execution=%s mandate=%s reason=%s failed_runs=%d',
        execution_id,
        execution.recurring_transfer_id,
        reason,
        mandate.failed_runs_count,
    )


# ---------------------------------------------------------------------------
# Webhook — mandate auto-pauzowany (R5)
# ---------------------------------------------------------------------------


@shared_task(bind=True, max_retries=3, name='recurring.notify_recurring_auto_paused')
def notify_recurring_auto_paused(self, mandate_id: str, last_failure_reason: str):
    """Powiadom bank nadawcy że mandate został auto-pauzowany.

    Bank powinien powiadomić klienta (push: "Twoje zlecenie zostało
    wstrzymane po N nieudanych próbach. Wznów w aplikacji.").
    """
    from recurring.models import RecurringTransfer

    try:
        mandate = RecurringTransfer.objects.select_related('payer_bank').get(id=mandate_id)
    except RecurringTransfer.DoesNotExist:
        logger.error('notify_recurring_auto_paused: mandate %s not found', mandate_id)
        return

    webhook_base = mandate.get_effective_webhook_url()
    if not webhook_base:
        logger.warning(
            'notify_recurring_auto_paused: bank %s has no webhook url', mandate.payer_bank_id
        )
        return

    url = f'{webhook_base}/auto-paused'
    payload = {
        'recurring_transfer_id': str(mandate.id),
        'payer_user_id': mandate.payer_user_id,
        'paused_at': (mandate.paused_at or timezone.now()).isoformat(),
        'failed_runs_count': mandate.failed_runs_count,
        'last_failure_reason': last_failure_reason,
    }

    try:
        resp = httpx.post(url, json=payload, timeout=NOTIFY_HTTP_TIMEOUT)
        resp.raise_for_status()
        logger.info('notify_recurring_auto_paused: sent to %s → %s', url, resp.status_code)
    except Exception as exc:
        retry_num = self.request.retries
        if retry_num >= self.max_retries:
            # Po 3 failach — alert dla operatora KLIK (przez log ERROR w MVP).
            logger.error(
                'notify_recurring_auto_paused: giving up after %d attempts, mandate=%s',
                retry_num + 1,
                mandate_id,
            )
            return
        countdown = RETRY_COUNTDOWN[min(retry_num, len(RETRY_COUNTDOWN) - 1)]
        logger.warning(
            'notify_recurring_auto_paused: failed (attempt %d) → %s, retry in %ds',
            retry_num + 1,
            exc,
            countdown,
        )
        raise self.retry(exc=exc, countdown=countdown) from exc


# ---------------------------------------------------------------------------
# Webhook — mandate zakończony (R4 cancel / R2 auto-cancel / R6 end_date)
# ---------------------------------------------------------------------------


@shared_task(bind=True, max_retries=3, name='recurring.notify_recurring_cancelled')
def notify_recurring_cancelled(self, mandate_id: str, reason: str):
    """Powiadom bank nadawcy że mandate został wycofany / zakończony.

    reason: USER_REQUEST / MANDATE_REVOKED_LOCALLY / ACCOUNT_CLOSED /
    END_DATE_REACHED. Jeden endpoint dla całego end-of-life — bank
    rozpoznaje po reason czy to cancel czy naturalne zakończenie.
    """
    from recurring.models import RecurringTransfer

    try:
        mandate = RecurringTransfer.objects.select_related('payer_bank').get(id=mandate_id)
    except RecurringTransfer.DoesNotExist:
        logger.error('notify_recurring_cancelled: mandate %s not found', mandate_id)
        return

    webhook_base = mandate.get_effective_webhook_url()
    if not webhook_base:
        logger.warning(
            'notify_recurring_cancelled: bank %s has no webhook url', mandate.payer_bank_id
        )
        return

    url = f'{webhook_base}/cancelled'
    payload = {
        'recurring_transfer_id': str(mandate.id),
        'payer_user_id': mandate.payer_user_id,
        # COMPLETED nie ustawia cancelled_at — fallback na updated_at.
        'cancelled_at': (mandate.cancelled_at or mandate.updated_at).isoformat(),
        'reason': reason,
    }

    try:
        resp = httpx.post(url, json=payload, timeout=NOTIFY_HTTP_TIMEOUT)
        resp.raise_for_status()
        logger.info('notify_recurring_cancelled: sent to %s → %s', url, resp.status_code)
    except Exception as exc:
        retry_num = self.request.retries
        if retry_num >= self.max_retries:
            logger.error(
                'notify_recurring_cancelled: giving up after %d attempts, mandate=%s',
                retry_num + 1,
                mandate_id,
            )
            return
        countdown = RETRY_COUNTDOWN[min(retry_num, len(RETRY_COUNTDOWN) - 1)]
        logger.warning(
            'notify_recurring_cancelled: failed (attempt %d) → %s, retry in %ds',
            retry_num + 1,
            exc,
            countdown,
        )
        raise self.retry(exc=exc, countdown=countdown) from exc
