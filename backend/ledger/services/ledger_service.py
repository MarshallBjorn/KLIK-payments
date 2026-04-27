"""LedgerService — operacje na księdze i sesjach rozliczeniowych."""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from decimal import Decimal

from django.db import transaction as db_transaction
from django.db.models import Sum
from django.utils import timezone
from django_redis import get_redis_connection

from banks.models import Bank
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
from ledger.services.netting import Obligation, net_obligations

logger = logging.getLogger('klik')

ACTIVE_SESSION_STATUSES = (
    SettlementSessionStatus.OPEN,
    SettlementSessionStatus.NETTING,
    SettlementSessionStatus.SETTLING,
)

# ----------------------------------------------------------------------
# P2P daily fee accrual — Redis key prefix (zsynchronizowany z AliasService)
# ----------------------------------------------------------------------
# AliasService zapisuje counter pod kluczem `aliases:lookups:{bank_id}:YYYYMMDD`
# (patrz aliases/services/alias_service.py, LOOKUP_COUNTER_PREFIX).
# Tutaj używamy tego samego prefixu — accrual MUSI czytać te same klucze co
# inkrement, inaczej naliczanie nigdy nic nie znajdzie.
LOOKUP_COUNTER_PREFIX = 'aliases:lookups'


class LedgerService:
    """Logika księgowa: rejestracja entries i orkiestracja sesji rozliczeniowych."""

    # ==================================================================
    # C2B — księgowanie transakcji po /confirm
    # ==================================================================

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
            * BANK_TO_BANK merchant_net
            * KLIK_FEE_C2B
            * AGENT_FEE

        Idempotent przez `source_ref=transaction.idempotency_key`.
        """
        if transaction.status != TransactionStatus.COMPLETED:
            raise TransactionNotCompletedError(transaction.id, transaction.status)

        if (
            transaction.merchant_net is None
            or transaction.klik_fee is None
            or transaction.agent_fee is None
        ):
            raise FeesNotCalculatedError(transaction.id)

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

        if transaction.klik_fee > 0:
            entries.append(
                LedgerEntry.objects.create(
                    from_bank=sender_bank,
                    to_bank=sender_bank,  # KLIK collect-at-source w MVP
                    beneficiary_type=BeneficiaryType.KLIK,
                    beneficiary_ref=None,
                    amount=transaction.klik_fee,
                    currency=currency,
                    zone=zone,
                    entry_type=LedgerEntryType.KLIK_FEE_C2B,
                    source_ref=source_ref,
                )
            )

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

    # ==================================================================
    # P2P — daily fee accrual (P4)
    # ==================================================================

    @staticmethod
    @db_transaction.atomic
    def record_p2p_lookup_fees(target_date: date | None = None) -> list[LedgerEntry]:
        """Naliczenie LedgerEntry dla prowizji P2P z agregacji counterów lookup.

        Wywoływane przez Celery Beat (P4) raz dziennie o 23:55 UTC.

        Algorytm (zgodny z docs/p2p/diagrams/WORKFLOW.md, P4):
            1. SCAN po kluczach `aliases:lookups:*:YYYYMMDD` w Redisie.
            2. Dla każdego klucza: GET liczba lookupów, dopasuj bank.
            3. total_fee = count × bank.p2p_lookup_fee
            4. INSERT LedgerEntry (P2P_LOOKUP_FEE, beneficiary=KLIK).
            5. DEL klucz w Redisie (counter zużyty).

        Idempotency: `source_ref` = `p2p:{bank_id}:{date}` — drugi run dla
        tej samej daty nie utworzy duplikatów (GET LedgerEntry first).

        Edge cases:
            - Bank nie istnieje → pomijamy + log error (raczej niemożliwe).
            - Bank.p2p_lookup_fee == 0 → tworzymy entry z amount=0? NIE — pomijamy
              (constraint amount__gt=0 i tak by zablokował).
            - count == 0 → pomijamy (pusty klucz teoretycznie nie powstaje).

        Args:
            target_date: dzień do agregacji (default: wczorajszy dzień UTC,
                bo o 23:55 agregujemy DZIEŃ BIEŻĄCY który zaraz się skończy).
                W realu wystarczy passować `timezone.now().date()`, ale
                parametryzacja ułatwia backfill / testy.

        Returns:
            Lista utworzonych LedgerEntry (może być pusta).
        """
        if target_date is None:
            target_date = timezone.now().date()

        date_str = target_date.strftime('%Y%m%d')
        redis = get_redis_connection('default')

        # SCAN zamiast KEYS — KEYS blokuje cały Redis (single-thread). Dla
        # rejestru aliasów to akceptowalne (małe N), ale standard jest taki
        # że SCAN-em się chodzi po kluczach produkcyjnych.
        pattern = f'{LOOKUP_COUNTER_PREFIX}:*:{date_str}'
        cursor = 0
        keys: list[bytes] = []
        while True:
            cursor, batch = redis.scan(cursor=cursor, match=pattern, count=200)
            keys.extend(batch)
            if cursor == 0:
                break

        if not keys:
            logger.info(
                'record_p2p_lookup_fees: brak counterów dla %s — nic do naliczenia',
                target_date,
            )
            return []

        created: list[LedgerEntry] = []
        skipped_zero_fee = 0
        skipped_existing = 0
        skipped_unknown_bank = 0

        for raw_key in keys:
            key = raw_key.decode('utf-8') if isinstance(raw_key, bytes) else raw_key
            # Format: aliases:lookups:{bank_id}:YYYYMMDD
            parts = key.split(':')
            if len(parts) != 4:
                logger.warning('record_p2p_lookup_fees: nieprawidłowy klucz %s', key)
                continue
            bank_id_str = parts[2]

            count_raw = redis.get(key)
            if count_raw is None:
                continue
            try:
                count = int(count_raw)
            except (TypeError, ValueError):
                logger.warning(
                    'record_p2p_lookup_fees: niepoprawna wartość counter dla %s: %r',
                    key,
                    count_raw,
                )
                continue

            if count <= 0:
                continue

            try:
                bank = Bank.objects.get(id=bank_id_str)
            except Bank.DoesNotExist:
                logger.error(
                    'record_p2p_lookup_fees: counter dla nieistniejącego banku %s',
                    bank_id_str,
                )
                skipped_unknown_bank += 1
                continue

            if bank.p2p_lookup_fee <= Decimal('0'):
                logger.debug(
                    'record_p2p_lookup_fees: bank %s ma p2p_lookup_fee=0 — pomijam',
                    bank.id,
                )
                skipped_zero_fee += 1
                # Wciąż usuwamy klucz — został przeczytany, nie chcemy go
                # akumulować w nieskończoność jeśli stawka jest 0.
                redis.delete(key)
                continue

            total_fee = (Decimal(count) * bank.p2p_lookup_fee).quantize(Decimal('0.01'))
            if total_fee <= Decimal('0'):
                # Po quantyzacji do groszy może wyjść 0 (np. 1 lookup × 0.0001).
                # Wtedy też pomijamy.
                skipped_zero_fee += 1
                redis.delete(key)
                continue

            source_ref = f'p2p:{bank.id}:{date_str}'

            # Idempotency: drugi run nie tworzy duplikatu
            if LedgerEntry.objects.filter(source_ref=source_ref).exists():
                skipped_existing += 1
                redis.delete(key)
                continue

            entry = LedgerEntry.objects.create(
                from_bank=bank,
                # KLIK collect-at-source: do_bank = bank z którego pobieramy
                # (analogicznie do KLIK_FEE_C2B w record_c2b_transaction).
                to_bank=bank,
                beneficiary_type=BeneficiaryType.KLIK,
                beneficiary_ref=None,
                amount=total_fee,
                currency=bank.currency,
                zone=bank.zone,
                entry_type=LedgerEntryType.P2P_LOOKUP_FEE,
                source_ref=source_ref,
            )
            created.append(entry)

            # Counter zużyty — usuwamy żeby nie naliczyć drugi raz przypadkiem
            # (mimo że mamy idempotency po source_ref).
            redis.delete(key)

        logger.info(
            'record_p2p_lookup_fees(%s): utworzono %d entries '
            '(skipped: %d zero fee, %d existing, %d unknown bank)',
            target_date,
            len(created),
            skipped_zero_fee,
            skipped_existing,
            skipped_unknown_bank,
        )
        return created

    # ==================================================================
    # Sesje rozliczeniowe — lifecycle
    # ==================================================================

    @staticmethod
    @db_transaction.atomic
    def create_session(zone: str) -> SettlementSession:
        """Tworzy nową sesję dla strefy w statusie OPEN.

        Odrzuca jeśli już istnieje aktywna sesja (OPEN/NETTING/SETTLING) —
        nie chcemy dwóch sesji równolegle dla tej samej strefy
        (race między Celery Beat a manualnym triggerem operatora).
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
        """Przypisuje wszystkie LedgerEntry strefy `session.zone` z `settled=False`
        i `session=NULL` do tej sesji. Zmienia status sesji na NETTING.
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

        aggregate = pending_qs.aggregate(
            count=models_count(),
            total=Sum('amount'),
        )
        count = aggregate['count'] or 0
        total = aggregate['total'] or Decimal('0')

        updated = pending_qs.update(session=session)

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
    def run_netting(session: SettlementSession) -> list[SettlementTransfer]:
        """Wykonuje multilateralny netting + greedy matching dla sesji.

        Przepływ:
            1. Pobiera wszystkie entries sesji (już przypisane przez
               `assign_pending_entries_to_session`).
            2. Mapuje na `Obligation` (czysty Python).
            3. Woła `net_obligations` (algorytm).
            4. Tworzy `SettlementTransfer` dla każdego wyniku.
            5. Zmienia status sesji NETTING → SETTLING.

        Args:
            session: sesja w statusie NETTING (po assign_pending_entries...).

        Returns:
            Lista utworzonych SettlementTransfer (gotowa do dispatcha).

        Raises:
            InvalidSessionStateError: sesja nie w stanie NETTING.
        """
        if session.status != SettlementSessionStatus.NETTING:
            raise InvalidSessionStateError(
                session.id,
                session.status,
                [SettlementSessionStatus.NETTING],
            )

        entries = list(LedgerEntry.objects.filter(session=session, settled=False))

        # Mapowanie LedgerEntry → Obligation. Algorytm operuje na
        # `from_bank_id`/`to_bank_id` — nie kazde entry ma sens jako "z A do B"
        # (np. KLIK_FEE_C2B ma from==to). Te są neutralne netto, ale algorytm
        # i tak je policzy poprawnie (dodaje i odejmuje od tego samego banku → 0).
        obligations = [
            Obligation(
                from_bank_id=entry.from_bank_id,
                to_bank_id=entry.to_bank_id,
                amount=entry.amount,
            )
            for entry in entries
        ]

        net_results = net_obligations(obligations)

        # Mapujemy z powrotem na encje DB
        currency = _resolve_session_currency(session)
        transfers: list[SettlementTransfer] = []
        for nt in net_results:
            transfer = SettlementTransfer.objects.create(
                session=session,
                from_bank_id=nt.from_bank_id,
                to_bank_id=nt.to_bank_id,
                amount=nt.amount,
                currency=currency,
                status=SettlementTransferStatus.PENDING,
            )
            transfers.append(transfer)

        session.status = SettlementSessionStatus.SETTLING
        session.save(update_fields=['status', 'updated_at'])

        logger.info(
            'run_netting: sesja %s, %d entries → %d transfers (status NETTING→SETTLING)',
            session.id,
            len(entries),
            len(transfers),
        )
        return transfers

    @staticmethod
    @db_transaction.atomic
    def mark_settled(
        session: SettlementSession,
        transfer_results: dict,
    ) -> SettlementSession:
        """Oznacza entries sesji jako settled na podstawie wyniku dispatcher RTGS.

        Args:
            session: sesja w statusie SETTLING.
            transfer_results: dict {transfer_id (str): 'COMPLETED' | 'FAILED'}.

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
        any_succeeded = False
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
                # UWAGA — w obecnym MVP markujemy WSZYSTKIE entries sesji
                # między tą parą banków jako settled. To uproszczenie: po
                # nettingu nie ma 1:1 mapowania entry→transfer, więc settlement
                # transfera A→B (kwota 265) "rozlicza" wszystkie entries
                # między A i B w tej sesji. Jeśli transfer fail, entries
                # zostają niesettled i trafią do następnej sesji.
                LedgerEntry.objects.filter(
                    session=session,
                    from_bank=transfer.from_bank,
                    to_bank=transfer.to_bank,
                    settled=False,
                ).update(settled=True, settled_at=now)
                any_succeeded = True
            else:
                transfer.status = SettlementTransferStatus.FAILED
                transfer.save(update_fields=['status'])
                any_failed = True

        # Status sesji: COMPLETED gdy wszystko OK, FAILED gdy cokolwiek padło.
        # Częściowy success też FAILED — operator musi zobaczyć w admin że
        # są entries do retry (one trafią do następnej sesji automatycznie).
        if any_failed:
            session.status = SettlementSessionStatus.FAILED
        else:
            session.status = SettlementSessionStatus.COMPLETED
        session.ended_at = now
        session.save(update_fields=['status', 'ended_at', 'updated_at'])

        logger.info(
            'mark_settled: sesja %s -> %s (succeeded=%s, failed=%s)',
            session.id,
            session.status,
            any_succeeded,
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


def _resolve_session_currency(session: SettlementSession) -> str:
    """Wyciąga walutę dla sesji ze strefy.

    Walidacja na poziomie LedgerEntry gwarantuje że wszystkie entries
    w sesji mają tę samą walutę (zone-isolated). Bierzemy ją z mapowania
    Zone → Currency — to jest single source of truth.
    """
    from common.enums import ZONE_CURRENCY, Zone

    return ZONE_CURRENCY[Zone(session.zone)].value
