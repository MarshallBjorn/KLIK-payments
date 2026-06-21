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

Identyfikatory RTGS per strefa (przekazywane do TransferRequest):
    EU (TARGET2):  bank.bic, bank.settlement_iban
    US (FedNow):   bank.fednow_routing_number, bank.fednow_account_number
    PL (SORBNET3): '' (gateway identyfikuje po bank.name)
    UK (CHAPS):    '' (gateway identyfikuje po bank.name)
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


def _get_rtgs_identifiers(bank, zone: str) -> tuple[str, str]:
    """Zwraca (primary_id, secondary_id) dla banku w danej strefie.

    Mapowanie na pola TransferRequest.from_bic/to_bic i from_iban/to_iban:

    - EU (TARGET2):  bic → from_bic/to_bic, settlement_iban → from_iban/to_iban
    - US (FedNow):   fednow_routing_number → from_bic/to_bic,
                     fednow_account_number → from_iban/to_iban
    - PL (SORBNET3): '' / '' — gateway identyfikuje po bank.name
    - UK (CHAPS):    '' / '' — gateway identyfikuje po bank.name

    Reużywamy pól from_bic/to_bic i from_iban/to_iban dla obu stref XML
    (TARGET2 i FedNow) — każdy gateway wie jak je zinterpretować:
    - TARGET2Gateway: traktuje je jako BIC i IBAN (ISO 20022)
    - FedNowGateway:  traktuje je jako RTN i account_number (pacs.008 US)
    """
    if zone == Zone.EU:
        return (getattr(bank, 'bic', '') or ''), (getattr(bank, 'settlement_iban', '') or '')
    if zone == Zone.US:
        return (
            getattr(bank, 'fednow_routing_number', '') or '',
            getattr(bank, 'fednow_account_number', '') or '',
        )
    # PL i UK: mock gateway identyfikuje po nazwie banku
    return '', ''


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

    Identyfikatory RTGS przekazywane do TransferRequest zależą od strefy:
    - EU: bic + settlement_iban (TARGET2 ISO 20022)
    - US: fednow_routing_number + fednow_account_number (FedNow pacs.008)
    - PL/UK: tylko name (mock RTGS)
    """
    logger.info('run_settlement_session: start dla strefy %s', zone)

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
    # Budujemy TransferRequest z odpowiednimi identyfikatorami per strefa:
    # - EU → bic + settlement_iban (TARGET2 ISO 20022)
    # - US → fednow_routing_number + fednow_account_number (FedNow pacs.008)
    # - PL/UK → '' (mock identyfikuje po nazwie)
    dispatcher = RTGSDispatcher.from_settings()
    transfer_requests = []
    for t in transfers:
        from_primary, from_secondary = _get_rtgs_identifiers(t.from_bank, zone)
        to_primary, to_secondary = _get_rtgs_identifiers(t.to_bank, zone)
        transfer_requests.append(
            TransferRequest(
                transfer_id=t.id,
                from_bank_code=t.from_bank.name,
                to_bank_code=t.to_bank.name,
                amount=t.amount,
                currency=t.currency,
                from_bic=from_primary,
                to_bic=to_primary,
                from_iban=from_secondary,
                to_iban=to_secondary,
            )
        )

    try:
        results = dispatcher.dispatch(zone, session.id, transfer_requests)
    except RTGSUnavailableError as exc:
        logger.error(
            'run_settlement_session: RTGS %s niedostępny dla sesji %s: %s',
            exc.system_name,
            session.id,
            exc.reason,
        )
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

    by_id = {str(t.id): t for t in transfers}
    for r in results:
        t = by_id.get(str(r.transfer_id))
        if not t:
            continue
        update_fields = []
        if r.rtgs_reference:
            t.rtgs_reference = r.rtgs_reference
            update_fields.append('rtgs_reference')
        if r.failure_reason and hasattr(t, 'failure_reason'):
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
# Beat schedule fan-out
# ----------------------------------------------------------------------


@shared_task(name='ledger.run_settlement_all_zones')
def run_settlement_all_zones() -> dict:
    """Triggeruje settlement dla wszystkich 4 stref równolegle (asynchronicznie)."""
    results = {}
    for zone in Zone:
        async_result = run_settlement_session.delay(zone.value)
        results[zone.value] = async_result.id

    logger.info('run_settlement_all_zones: enqueued %d tasków', len(results))
    return {'status': 'ENQUEUED', 'tasks': results}
