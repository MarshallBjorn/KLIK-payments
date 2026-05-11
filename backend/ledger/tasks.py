"""
Celery tasks dla apki ledger.

Zawiera:
- run_settlement_session(zone) — pełny pipeline sesji settlement: create →
  assign → netting → dispatch RTGS → mark_settled. Wywoływany przez Celery
  Beat per strefa (interwał z `.env`).
- accrue_p2p_lookup_fees(date) — daily fee accrual dla P2P. Wywoływany przez
  Celery Beat raz dziennie (UTC 23:55).

Patrz dla pełnego workflow:
    docs/c2b/diagrams/WORKFLOW.md (sekcja A5 — settlement)
    docs/p2p/diagrams/WORKFLOW.md (sekcja P4 — daily fee accrual)

Beat schedule jest definiowany w `core.celery_beat_schedule` (kompatybilny
z `django-celery-beat` schedulerem opartym o DB — można też zmodyfikować
schedule przez Django Admin → Periodic Tasks).
"""

from __future__ import annotations

import logging
from datetime import date as date_cls

from celery import shared_task
from django.utils import timezone

from common.enums import Zone
from ledger.enums import SettlementSessionStatus
from ledger.exceptions import ActiveSessionExistsError
from ledger.rtgs import RTGSDispatcher, TransferRequest, TransferStatus
from ledger.rtgs.exceptions import RTGSUnavailableError
from ledger.services import LedgerService

logger = logging.getLogger('klik')


# ----------------------------------------------------------------------
# A5 — pełna sesja settlement per strefa
# ----------------------------------------------------------------------


@shared_task(bind=True, name='ledger.run_settlement_session')
def run_settlement_session(self, zone: str) -> dict:
    """Pełny pipeline sesji settlement dla strefy.

    Workflow (zgodnie z A5 w docs/c2b/diagrams/WORKFLOW.md):
        1. create_session(zone)                        — INSERT SettlementSession (OPEN)
        2. assign_pending_entries_to_session(session)  — UPDATE entries, status NETTING
        3. run_netting(session)                        — multilateral netting + greedy
                                                          → SettlementTransfer rows,
                                                          status SETTLING
        4. RTGSDispatcher.dispatch(zone, ..., transfers) — wysłanie do RTGS
        5. mark_settled(session, results)              — status COMPLETED/FAILED

    Idempotency: jeśli już istnieje aktywna sesja dla strefy
    (`ActiveSessionExistsError`), task loguje warning i kończy się no-op.
    To zabezpiecza przed double-trigger (Beat × manualny trigger).

    Robustness:
    - Brak entries → sesja COMPLETED z 0 transferów (no-op, ale rekord powstaje
      dla audytu).
    - RTGS niedostępny → cała sesja FAILED, entries zostają w puli na następną.
    - Worker crash mid-session → SettlementSession w stanie NETTING/SETTLING
      będzie blokować nowe sesje przez ActiveSessionExistsError; recovery wymaga
      manualnej interwencji operatora (tu można dorobić cleanup task).

    Args:
        zone: kod strefy (PL/EU/UK/US).

    Returns:
        Dict z podsumowaniem sesji (do flower/admin).
    """
    logger.info('run_settlement_session: start dla strefy %s', zone)

    # Walidacja strefy zanim cokolwiek zrobimy w DB
    try:
        Zone(zone)
    except ValueError:
        logger.error('run_settlement_session: nieznana strefa %r', zone)
        return {'status': 'ERROR', 'reason': f'Unknown zone: {zone}'}

    # ETAP 1: Utworzenie sesji
    try:
        session = LedgerService.create_session(zone)
    except ActiveSessionExistsError as exc:
        logger.warning(
            'run_settlement_session: aktywna sesja %s dla strefy %s już istnieje — pomijam',
            exc.existing_session_id,
            zone,
        )
        return {
            'status': 'SKIPPED',
            'reason': 'active_session_exists',
            'existing_session_id': str(exc.existing_session_id),
        }

    # ETAP 2: Przypisanie pending entries
    entries_count = LedgerService.assign_pending_entries_to_session(session)

    # No-op edge case: brak entries → zamykamy sesję jako COMPLETED z 0 transferami.
    # Można by usuwać sesję, ale audit value jest większa gdy widać że "o 12:00
    # sprawdzaliśmy strefę PL i nie było nic do rozliczenia".
    if entries_count == 0:
        session.status = SettlementSessionStatus.COMPLETED
        session.ended_at = timezone.now()
        session.save(update_fields=['status', 'ended_at', 'updated_at'])
        logger.info(
            'run_settlement_session: brak entries dla strefy %s, sesja %s → COMPLETED (no-op)',
            zone,
            session.id,
        )
        return {
            'status': 'COMPLETED',
            'session_id': str(session.id),
            'zone': zone,
            'entries_count': 0,
            'transfers_count': 0,
        }

    # ETAP 3: Netting (NETTING → SETTLING)
    transfers = LedgerService.run_netting(session)

    if not transfers:
        # Wszystko zbilansowane wewnętrznie (suma wszystkich pozycji netto = 0
        # i każdy uczestnik osobno też ma 0 — np. A→B 100 i B→A 100). Po marku
        # wszystkich entries jako settled, sesja COMPLETED.
        session.refresh_from_db()
        from ledger.models import LedgerEntry

        LedgerEntry.objects.filter(session=session, settled=False).update(
            settled=True,
            settled_at=timezone.now(),
        )
        session.status = SettlementSessionStatus.COMPLETED
        session.ended_at = timezone.now()
        session.save(update_fields=['status', 'ended_at', 'updated_at'])
        logger.info(
            'run_settlement_session: sesja %s zbilansowana wewnętrznie → COMPLETED',
            session.id,
        )
        return {
            'status': 'COMPLETED',
            'session_id': str(session.id),
            'zone': zone,
            'entries_count': entries_count,
            'transfers_count': 0,
        }

    # ETAP 4: Dispatch do RTGS
    dispatcher = RTGSDispatcher.from_settings()
    transfer_requests = [
        TransferRequest(
            transfer_id=t.id,
            from_bank_code=t.from_bank.name,
            to_bank_code=t.to_bank.name,
            amount=t.amount,
            currency=t.currency,
        )
        for t in transfers
    ]

    try:
        results = dispatcher.dispatch(zone, session.id, transfer_requests)
    except RTGSUnavailableError as exc:
        # Cały RTGS down — wszystkie transfery fail, sesja FAILED.
        # Entries pozostają niesettled, trafią do następnej sesji.
        logger.error(
            'run_settlement_session: RTGS %s niedostępny dla sesji %s: %s',
            exc.system_name,
            session.id,
            exc.reason,
        )
        _ = {str(t.id): SettlementSessionStatus.FAILED for t in transfers}
        # Mark wszystkich jako FAILED przez mark_settled — jeden punkt finalizacji
        from ledger.enums import SettlementTransferStatus

        all_failed_dict = {str(t.id): SettlementTransferStatus.FAILED for t in transfers}
        LedgerService.mark_settled(session, all_failed_dict)
        return {
            'status': 'FAILED',
            'session_id': str(session.id),
            'zone': zone,
            'reason': f'RTGS unavailable: {exc.reason}',
            'entries_count': entries_count,
            'transfers_count': len(transfers),
        }

    # ETAP 5: Mark settled (na podstawie wyniku RTGS)
    from ledger.enums import SettlementTransferStatus

    transfer_results = {
        str(r.transfer_id): (
            SettlementTransferStatus.COMPLETED
            if r.status == TransferStatus.SUCCESS
            else SettlementTransferStatus.FAILED
        )
        for r in results
    }

    # Zapis rtgs_reference / failure_reason na transferach (przed mark_settled
    # bo mark zmienia status, nie chcemy go nadpisać).

    by_id = {str(t.id): t for t in transfers}
    for r in results:
        t = by_id.get(str(r.transfer_id))
        if not t:
            continue
        update_fields = []
        if r.rtgs_reference:
            t.rtgs_reference = r.rtgs_reference
            update_fields.append('rtgs_reference')
        if r.failure_reason:
            if hasattr(t, 'failure_reason'):
                t.failure_reason = r.failure_reason
                update_fields.append('failure_reason')
        if update_fields:
            t.save(update_fields=update_fields)

    final_session = LedgerService.mark_settled(session, transfer_results)

    successes = sum(1 for v in transfer_results.values() if v == SettlementTransferStatus.COMPLETED)
    fails = len(transfer_results) - successes

    logger.info(
        'run_settlement_session: sesja %s zakończona, status=%s, transferów=%d (OK=%d, FAIL=%d)',
        final_session.id,
        final_session.status,
        len(transfers),
        successes,
        fails,
    )
    return {
        'status': str(final_session.status),
        'session_id': str(final_session.id),
        'zone': zone,
        'entries_count': entries_count,
        'transfers_count': len(transfers),
        'transfers_succeeded': successes,
        'transfers_failed': fails,
    }


# ----------------------------------------------------------------------
# P4 — Daily P2P fee accrual
# ----------------------------------------------------------------------


@shared_task(
    bind=True,
    name='ledger.accrue_p2p_lookup_fees',
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=300,
    retry_jitter=True,
    max_retries=3,
)
def accrue_p2p_lookup_fees(self, target_date_iso: str | None = None) -> dict:
    """Daily P2P lookup fee accrual.

    Schedule: codziennie o 23:55 UTC (wywoływany przez Celery Beat).

    Workflow:
        1. SCAN po kluczach `aliases:lookups:*:{date}` w Redisie.
        2. Per bank: count × p2p_lookup_fee → LedgerEntry (P2P_LOOKUP_FEE).
        3. DEL counter w Redisie.

    Idempotency: source_ref `p2p:{bank_id}:{date}` — drugi run dla tej samej
    daty nie tworzy duplikatów (LedgerService sprawdza istniejące entries).

    Retry: pełny task retry z exponential backoff (do 5 minut). Pierwsze
    przyczyny failu to zwykle Redis unreachable / DB lock.

    Args:
        target_date_iso: data w ISO format (YYYY-MM-DD) dla manual trigger /
            backfill. Default: dzień bieżący UTC (Beat woła task o 23:55,
            więc agregujemy "ten dzień który zaraz się skończy").

    Returns:
        Dict ze statystykami.
    """
    if target_date_iso:
        try:
            target_date = date_cls.fromisoformat(target_date_iso)
        except ValueError:
            logger.error('accrue_p2p_lookup_fees: zła data %r', target_date_iso)
            return {'status': 'ERROR', 'reason': f'Invalid date: {target_date_iso}'}
    else:
        target_date = timezone.now().date()

    logger.info('accrue_p2p_lookup_fees: start dla %s', target_date)

    entries = LedgerService.record_p2p_lookup_fees(target_date)

    return {
        'status': 'OK',
        'date': target_date.isoformat(),
        'entries_created': len(entries),
        'total_amount': str(
            sum((e.amount for e in entries), start=__import__('decimal').Decimal('0'))
        ),
    }


# ----------------------------------------------------------------------
# Beat schedule fan-out — jedno zadanie per strefa
# ----------------------------------------------------------------------
# Celery Beat schedule (w core/settings/base.py) wywołuje per-zone settlement.
# Tu trzymamy convenience task który orkiestruje wszystkie strefy naraz —
# operator może go odpalić ręcznie z django shell żeby zamknąć wszystkie sesje.


@shared_task(name='ledger.run_settlement_all_zones')
def run_settlement_all_zones() -> dict:
    """Triggeruje settlement dla wszystkich 4 stref równolegle (asynchronicznie).

    Każda strefa idzie jako osobny task — workery mogą obsługiwać równolegle.
    Funkcja nie czeka na zakończenie — tylko enqueue.
    """
    results = {}
    for zone in Zone:
        async_result = run_settlement_session.delay(zone.value)
        results[zone.value] = async_result.id

    logger.info('run_settlement_all_zones: enqueued %d tasków', len(results))
    return {'status': 'ENQUEUED', 'tasks': results}
