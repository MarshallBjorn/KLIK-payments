"""LedgerService — operacje na księdze i sesjach rozliczeniowych."""

import logging
from decimal import Decimal

from django.db import transaction as db_transaction
from django.db.models import Sum
from django.utils import timezone

from codes.enums import TransactionStatus
from codes.models import Transaction
from ledger.enums import (
    BeneficiaryType,
    LedgerEntryType,
    SettlementSessionStatus,
    SettlementTransferStatus,
)
from ledger.exceptions import (
    ActiveSessionExistsError,
    FeesNotCalculatedError,
    InvalidSessionStateError,
    TransactionNotCompletedError,
)
from ledger.models import LedgerEntry, SettlementSession, SettlementTransfer

logger = logging.getLogger('klik')

ACTIVE_SESSION_STATUSES = (
    SettlementSessionStatus.OPEN,
    SettlementSessionStatus.NETTING,
    SettlementSessionStatus.SETTLING,
)


class LedgerService:
    """Logika księgowa: rejestracja entries i orkiestracja sesji rozliczeniowych."""

    # ------------------------------------------------------------------
    # C2B — księgowanie transakcji po /confirm
    # ------------------------------------------------------------------

    @staticmethod
    @db_transaction.atomic
    def record_c2b_transaction(transaction: Transaction) -> list[LedgerEntry]:
        """
        Tworzy LedgerEntry dla zakończonej transakcji C2B.

        - on-us (sender_bank == merchant.settlement_bank): 2 entries
            * KLIK_FEE_C2B (od sender_bank do KLIK)
            * AGENT_FEE (od sender_bank do agent.settlement_bank, beneficiary AGENT)
            (merchant_net pomijany — sender_bank księguje wewnętrznie)
        - off-us: 3 entries
            * BANK_TO_BANK merchant_net (od sender_bank do merchant.settlement_bank)
            * KLIK_FEE_C2B
            * AGENT_FEE

        Idempotent przez `source_ref=transaction.idempotency_key`. Drugi call
        zwraca istniejące entries bez tworzenia nowych.

        Raises:
            TransactionNotCompletedError: tx.status != COMPLETED
            FeesNotCalculatedError: brak policzonych fees
        """
        if transaction.status != TransactionStatus.COMPLETED:
            raise TransactionNotCompletedError(transaction.id, transaction.status)

        if (
            transaction.merchant_net is None
            or transaction.klik_fee is None
            or transaction.agent_fee is None
        ):
            raise FeesNotCalculatedError(transaction.id)

        # Idempotency: czy już zaksięgowane?
        existing = list(LedgerEntry.objects.filter(source_ref=transaction.idempotency_key))
        if existing:
            logger.info(
                'record_c2b_transaction: entries już istnieją dla source_ref=%s, '
                'zwracam istniejące (%d entries).',
                transaction.idempotency_key,
                len(existing),
            )
            return existing

        sender_bank = transaction.sender_bank
        merchant = transaction.merchant
        agent = transaction.agent
        currency = transaction.currency
        zone = transaction.zone
        source_ref = transaction.idempotency_key

        entries: list[LedgerEntry] = []

        # 1. merchant_net — tylko dla off-us (on-us = wewnątrz banku)
        if not transaction.is_on_us:
            entries.append(
                LedgerEntry.objects.create(
                    from_bank=sender_bank,
                    to_bank=merchant.settlement_bank,
                    beneficiary_type=BeneficiaryType.BANK,
                    beneficiary_ref=merchant.settlement_bank.id,
                    amount=transaction.merchant_net,
                    currency=currency,
                    zone=zone,
                    entry_type=LedgerEntryType.BANK_TO_BANK,
                    source_ref=source_ref,
                )
            )

        # 2. klik_fee — od sender_bank do KLIK (settlement_bank KLIKa = sender_bank
        #    w MVP, czyli to_bank=sender_bank — KLIK ma konto wirtualne tam).
        #    Realistycznie KLIK miałby konto w *jednym* banku per strefa,
        #    ale w MVP przyjmujemy że to_bank=sender_bank (KLIK pobiera "u źródła").
        if transaction.klik_fee > 0:
            entries.append(
                LedgerEntry.objects.create(
                    from_bank=sender_bank,
                    to_bank=sender_bank,  # KLIK collect at source
                    beneficiary_type=BeneficiaryType.KLIK,
                    beneficiary_ref=None,
                    amount=transaction.klik_fee,
                    currency=currency,
                    zone=zone,
                    entry_type=LedgerEntryType.KLIK_FEE_C2B,
                    source_ref=source_ref,
                )
            )

        # 3. agent_fee — od sender_bank do agent.settlement_bank, beneficiary AGENT
        if transaction.agent_fee > 0:
            entries.append(
                LedgerEntry.objects.create(
                    from_bank=sender_bank,
                    to_bank=agent.settlement_bank,
                    beneficiary_type=BeneficiaryType.AGENT,
                    beneficiary_ref=agent.id,
                    amount=transaction.agent_fee,
                    currency=currency,
                    zone=zone,
                    entry_type=LedgerEntryType.AGENT_FEE,
                    source_ref=source_ref,
                )
            )

        logger.info(
            'record_c2b_transaction: utworzono %d entries dla tx=%s (on_us=%s).',
            len(entries),
            transaction.id,
            transaction.is_on_us,
        )
        return entries

    # ------------------------------------------------------------------
    # P2P — naliczanie prowizji za lookupy (skeleton, implementacja w P4)
    # ------------------------------------------------------------------

    @staticmethod
    def record_p2p_lookup_fees(date) -> list[LedgerEntry]:
        """
        Naliczenie LedgerEntry dla prowizji P2P z agregacji counterów lookup.
        Wywoływane przez Celery Beat (P4) raz dziennie.

        TODO: implementacja w osobnym tasku po dispatcherze RTGS.
        """
        raise NotImplementedError(
            'P4 daily P2P fee accrual — będzie zaimplementowane razem ' 'z Celery Beat schedulerem.'
        )

    # ------------------------------------------------------------------
    # Sesje rozliczeniowe — lifecycle
    # ------------------------------------------------------------------

    @staticmethod
    @db_transaction.atomic
    def create_session(zone: str) -> SettlementSession:
        """
        Tworzy nową sesję dla strefy w statusie OPEN. Odrzuca jeśli już
        istnieje aktywna sesja (OPEN/NETTING/SETTLING) dla tej strefy.
        """
        existing = SettlementSession.objects.filter(
            zone=zone,
            status__in=ACTIVE_SESSION_STATUSES,
        ).first()
        if existing is not None:
            raise ActiveSessionExistsError(zone, existing.id)

        session = SettlementSession.objects.create(
            zone=zone,
            started_at=timezone.now(),
            status=SettlementSessionStatus.OPEN,
        )
        logger.info('create_session: utworzono sesję %s dla strefy %s', session.id, zone)
        return session

    @staticmethod
    @db_transaction.atomic
    def assign_pending_entries_to_session(session: SettlementSession) -> int:
        """
        Przypisuje wszystkie LedgerEntry strefy `session.zone` z `settled=False`
        i `session=NULL` do tej sesji. Zmienia status sesji na NETTING.

        Returns:
            Liczba przypisanych entries.
        """
        if session.status != SettlementSessionStatus.OPEN:
            raise InvalidSessionStateError(
                session.id,
                session.status,
                [SettlementSessionStatus.OPEN],
            )

        pending_qs = LedgerEntry.objects.filter(
            zone=session.zone,
            settled=False,
            session__isnull=True,
        )

        # Agregacja przed UPDATE — potrzebujemy total_volume
        aggregate = pending_qs.aggregate(
            count=models_count(),
            total=Sum('amount'),
        )
        count = aggregate['count'] or 0
        total = aggregate['total'] or Decimal('0')

        # UPDATE w jednej query
        updated = pending_qs.update(session=session)

        # Aktualizacja sesji
        session.total_entries_count = count
        session.total_volume = total
        session.status = SettlementSessionStatus.NETTING
        session.save(update_fields=['total_entries_count', 'total_volume', 'status', 'updated_at'])

        logger.info(
            'assign_pending_entries_to_session: sesja %s, %d entries, total %s',
            session.id,
            updated,
            total,
        )
        return updated

    @staticmethod
    @db_transaction.atomic
    def mark_settled(
        session: SettlementSession,
        transfer_results: dict,
    ) -> SettlementSession:
        """
        Oznacza entries sesji jako settled na podstawie wyniku dispatcher RTGS.

        Args:
            session: sesja w statusie SETTLING
            transfer_results: dict {transfer_id: 'COMPLETED' | 'FAILED'}

        Returns:
            Zaktualizowana sesja (status COMPLETED lub FAILED).
        """
        if session.status != SettlementSessionStatus.SETTLING:
            raise InvalidSessionStateError(
                session.id,
                session.status,
                [SettlementSessionStatus.SETTLING],
            )

        any_failed = False
        now = timezone.now()

        for transfer_id, result in transfer_results.items():
            try:
                transfer = SettlementTransfer.objects.get(
                    id=transfer_id,
                    session=session,
                )
            except SettlementTransfer.DoesNotExist:
                logger.error(
                    'mark_settled: transfer %s nie istnieje w sesji %s',
                    transfer_id,
                    session.id,
                )
                any_failed = True
                continue

            if result == SettlementTransferStatus.COMPLETED:
                transfer.status = SettlementTransferStatus.COMPLETED
                transfer.completed_at = now
                transfer.save(update_fields=['status', 'completed_at'])

                # Settlement entries dla pary (from_bank, to_bank) w tej sesji
                LedgerEntry.objects.filter(
                    session=session,
                    from_bank=transfer.from_bank,
                    to_bank=transfer.to_bank,
                    settled=False,
                ).update(settled=True, settled_at=now)
            else:
                transfer.status = SettlementTransferStatus.FAILED
                transfer.save(update_fields=['status'])
                any_failed = True

        # Aktualizacja statusu sesji
        if any_failed:
            session.status = SettlementSessionStatus.FAILED
        else:
            session.status = SettlementSessionStatus.COMPLETED
        session.ended_at = now
        session.save(update_fields=['status', 'ended_at', 'updated_at'])

        logger.info(
            'mark_settled: sesja %s -> %s (failed=%s)',
            session.id,
            session.status,
            any_failed,
        )
        return session


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def models_count():
    """Helper for aggregate count."""
    from django.db.models import Count

    return Count('id')
