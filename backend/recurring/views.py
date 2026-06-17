"""Endpointy REST dla modułu recurring (Regularne transfery).

Endpointy (docs/reccuring/integration/INFO.md, sekcja "API reference"):
  POST /recurring/create            — bank rejestruje mandate (R1)
  GET  /recurring                   — listing mandatów klienta (?payer_user_id=)
  GET  /recurring/{id}              — szczegóły mandate
  POST /recurring/{id}/pause        — wstrzymanie (R3)
  POST /recurring/{id}/resume       — wznowienie (R3)
  POST /recurring/{id}/cancel       — anulowanie (R4)
  GET  /recurring/{id}/executions   — historia runów

Tylko bank ma dostęp do API recurring (X-KLIK-Bank-Api-Key) — agenci nie
operują na zleceniach stałych. Wymagane flagi: recurring_enabled ORAZ
p2p_enabled (lookup aliasu jest mechanizmem P2P).
"""

import logging

from dateutil.relativedelta import relativedelta
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction as db_transaction
from django.db.models import Count, Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from aliases.models import Alias
from banks.authentication import XKlikBankApiKeyAuthentication
from common.enums import ZONE_CURRENCY, Zone
from common.exceptions import (
    BadRequestError,
    BankInactiveError,
    CurrencyMismatchError,
    InsufficientPermissionsError,
    InvalidAmountError,
    P2PNotEnabledError,
    ZoneMismatchError,
)
from common.idempotency import idempotent_endpoint
from common.phone import validate_e164
from recurring import schedule
from recurring.exceptions import (
    InvalidCycleError,
    InvalidDateRangeError,
    InvalidPhoneFormatError,
    RecipientAliasNotFoundError,
    RecurringNotActiveError,
    RecurringNotEnabledError,
    RecurringNotFoundError,
    RecurringNotPausedError,
    RecurringTerminatedError,
)
from recurring.models import (
    RECURRING_TERMINAL_STATUSES,
    RecurringCycle,
    RecurringExecutionStatus,
    RecurringTransfer,
    RecurringTransferStatus,
)
from recurring.serializers import (
    RecurringCancelResponseSerializer,
    RecurringCreateRequestSerializer,
    RecurringCreateResponseSerializer,
    RecurringDetailResponseSerializer,
    RecurringExecutionItemSerializer,
    RecurringListItemSerializer,
    RecurringPauseResponseSerializer,
    RecurringResumeResponseSerializer,
)
from recurring.tasks import notify_recurring_cancelled

logger = logging.getLogger('klik')

# Limity walidacji dat przy create (INFO.md "Walidacja dat przy create").
MAX_MANDATE_SPAN_YEARS = 10
MAX_START_AHEAD_YEARS = 1

EXECUTIONS_DEFAULT_LIMIT = 20
EXECUTIONS_MAX_LIMIT = 100


class BaseRecurringView(APIView):
    """Wspólna baza: auth bankowy + weryfikacja flag modułu.

    Kolejność checków zgodna z R1: najpierw active (już w auth), potem
    recurring_enabled, potem p2p_enabled.
    """

    authentication_classes = [XKlikBankApiKeyAuthentication]
    permission_classes = [IsAuthenticated]

    def check_bank(self, bank):
        if not bank.active:
            raise BankInactiveError()
        if not bank.recurring_enabled:
            raise RecurringNotEnabledError()
        if not bank.p2p_enabled:
            raise P2PNotEnabledError()

    def get_owned_mandate_for_update(self, bank, recurring_transfer_id):
        """SELECT FOR UPDATE + kontrola własności (operacje mutujące).

        Cudzy mandate → 403_INSUFFICIENT_PERMISSIONS (bank zna UUID, więc
        404 nic by nie ukrywało — spójnie z aliases.unregister).
        """
        try:
            mandate = RecurringTransfer.objects.select_for_update().get(id=recurring_transfer_id)
        except RecurringTransfer.DoesNotExist as e:
            raise RecurringNotFoundError() from e
        if mandate.payer_bank_id != bank.id:
            raise InsufficientPermissionsError()
        return mandate


# ---------------------------------------------------------------------------
# POST /recurring/create
# ---------------------------------------------------------------------------


class RecurringCreateView(BaseRecurringView):
    """
    POST /api/v1/recurring/create

    Bank wywołuje PO uzyskaniu PIN-a od klienta i lokalnym zarejestrowaniu
    mandate. KLIK robi walidacyjny lookup aliasu (NIE naliczany — jest częścią
    mandate setup, nie execution) i zapisuje RecurringTransfer ACTIVE.
    """

    @idempotent_endpoint
    def post(self, request):
        bank = request.user
        self.check_bank(bank)

        serializer = RecurringCreateRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        d = serializer.validated_data

        self._validate_domain(bank, d)

        # Idempotency na poziomie DB — replay tego samego klucza przez ten sam
        # bank zwraca istniejący mandate (dekorator pokrywa okno 24h w Redisie,
        # DB jest trwałą drugą linią — spójnie z ChequeIssueView).
        idempotency_key = request.headers.get('Idempotency-Key', '')
        existing = RecurringTransfer.objects.filter(
            idempotency_key=idempotency_key,
            payer_bank=bank,
        ).first()
        if existing:
            return self._created_response(existing)

        # Walidacyjny lookup aliasu odbiorcy — synchroniczny, darmowy
        # (bez inkrementu countera P2P; płatne są dopiero lookupy w runach).
        try:
            alias = Alias.objects.select_related('bank').get(phone=d['recipient_phone'])
        except Alias.DoesNotExist as e:
            raise RecipientAliasNotFoundError() from e
        if alias.zone != d['zone']:
            raise ZoneMismatchError(
                detail=f'Strefa aliasu odbiorcy ({alias.zone}) ≠ strefa zlecenia ({d["zone"]}).'
            )

        mandate = RecurringTransfer.objects.create(
            payer_bank=bank,
            payer_user_id=d['payer_user_id'],
            recipient_phone=d['recipient_phone'],
            amount=d['amount'],
            currency=d['currency'],
            zone=d['zone'],
            cycle=d['cycle'],
            start_date=d['start_date'],
            end_date=d['end_date'],
            next_run_at=schedule.first_run_at(d['start_date']),
            mandate_signed_at=d['mandate_signed_at'],
            idempotency_key=idempotency_key,
        )

        logger.info(
            'recurring created: id=%s bank=%s amount=%s %s cycle=%s start=%s end=%s',
            mandate.id,
            bank.id,
            mandate.amount,
            mandate.currency,
            mandate.cycle,
            mandate.start_date,
            mandate.end_date,
        )
        return self._created_response(mandate)

    def _validate_domain(self, bank, d):
        """Walidacja domenowa z dedykowanymi kodami błędów z INFO.md."""
        # 400_INVALID_PHONE_FORMAT
        try:
            validate_e164(d['recipient_phone'])
        except DjangoValidationError as e:
            raise InvalidPhoneFormatError() from e

        # 400_INVALID_CYCLE
        if d['cycle'] not in RecurringCycle.values:
            raise InvalidCycleError()

        # 400_INVALID_AMOUNT
        if d['amount'] <= 0:
            raise InvalidAmountError()

        # 400_INVALID_DATE_RANGE
        today = timezone.now().date()
        start, end = d['start_date'], d['end_date']
        if start < today:
            raise InvalidDateRangeError(detail='start_date nie może być w przeszłości.')
        if start > today + relativedelta(years=MAX_START_AHEAD_YEARS):
            raise InvalidDateRangeError(
                detail=f'start_date maksymalnie {MAX_START_AHEAD_YEARS} rok w przyszłość.'
            )
        if end is not None:
            if end <= start:
                raise InvalidDateRangeError(detail='end_date musi być późniejsze niż start_date.')
            if end > start + relativedelta(years=MAX_MANDATE_SPAN_YEARS):
                raise InvalidDateRangeError(
                    detail=f'Zlecenie może trwać maksymalnie {MAX_MANDATE_SPAN_YEARS} lat.'
                )

        # 422_ZONE_MISMATCH / 422_CURRENCY_MISMATCH — strefa banku, strefa
        # w requeście i waluta muszą być spójne. Strefę aliasu sprawdzamy
        # po lookupie.
        if d['zone'] not in Zone.values:
            raise ZoneMismatchError(detail=f'Nieznana strefa: {d["zone"]}.')
        if d['zone'] != bank.zone:
            raise ZoneMismatchError(detail=f'Strefa {d["zone"]} ≠ strefa banku {bank.zone}.')
        expected_currency = ZONE_CURRENCY.get(Zone(d['zone']))
        if d['currency'] != expected_currency:
            raise CurrencyMismatchError(
                detail=(
                    f'Waluta {d["currency"]} nie pasuje do strefy {d["zone"]} '
                    f'(oczekiwano {expected_currency}).'
                )
            )

    @staticmethod
    def _created_response(mandate):
        response_data = {
            'recurring_transfer_id': mandate.id,
            'status': mandate.status,
            'next_run_at': mandate.next_run_at,
            'created_at': mandate.created_at,
        }
        return Response(
            RecurringCreateResponseSerializer(response_data).data,
            status=status.HTTP_201_CREATED,
        )


# ---------------------------------------------------------------------------
# GET /recurring?payer_user_id={id}  +  GET /recurring/{id}
# ---------------------------------------------------------------------------


class RecurringListView(BaseRecurringView):
    """
    GET /api/v1/recurring?payer_user_id={id}&status={ACTIVE|...|ALL}

    Listing mandatów klienta. Bank widzi tylko swoich (filtr po payer_bank).
    """

    def get(self, request):
        bank = request.user
        self.check_bank(bank)

        payer_user_id = request.query_params.get('payer_user_id')
        if not payer_user_id:
            raise BadRequestError(detail='Parametr payer_user_id jest wymagany.')

        status_param = request.query_params.get('status', RecurringTransferStatus.ACTIVE)
        if status_param != 'ALL' and status_param not in RecurringTransferStatus.values:
            raise BadRequestError(
                detail=f'Niepoprawny status: {status_param}. '
                f'Dozwolone: {", ".join(RecurringTransferStatus.values)}, ALL.'
            )

        qs = RecurringTransfer.objects.filter(payer_bank=bank, payer_user_id=payer_user_id)
        if status_param != 'ALL':
            qs = qs.filter(status=status_param)

        items = RecurringListItemSerializer(qs, many=True).data
        return Response({'items': items, 'count': len(items)}, status=status.HTTP_200_OK)


class RecurringDetailView(BaseRecurringView):
    """
    GET /api/v1/recurring/{recurring_transfer_id}

    Szczegóły mandate. Cudzy mandate → 404 (zgodnie z INFO.md dla GET).
    """

    def get(self, request, recurring_transfer_id):
        bank = request.user
        self.check_bank(bank)

        try:
            mandate = RecurringTransfer.objects.get(id=recurring_transfer_id)
        except RecurringTransfer.DoesNotExist as e:
            raise RecurringNotFoundError() from e
        if mandate.payer_bank_id != bank.id:
            raise RecurringNotFoundError()

        counts = mandate.executions.aggregate(
            succeeded=Count('id', filter=Q(status=RecurringExecutionStatus.SUCCESS)),
            failed=Count('id', filter=Q(status=RecurringExecutionStatus.FAILED)),
        )
        response_data = {
            'recurring_transfer_id': mandate.id,
            'status': mandate.status,
            'payer_user_id': mandate.payer_user_id,
            'recipient_phone': mandate.recipient_phone,
            'amount': mandate.amount,
            'currency': mandate.currency,
            'zone': mandate.zone,
            'cycle': mandate.cycle,
            'start_date': mandate.start_date,
            'end_date': mandate.end_date,
            'next_run_at': mandate.next_run_at,
            'last_run_at': mandate.last_run_at,
            'failed_runs_count': mandate.failed_runs_count,
            'executions_summary': {
                'scheduled': schedule.estimate_total_runs(
                    mandate.start_date, mandate.end_date, mandate.cycle
                ),
                'succeeded': counts['succeeded'],
                'failed': counts['failed'],
            },
            'created_at': mandate.created_at,
        }
        return Response(
            RecurringDetailResponseSerializer(response_data).data,
            status=status.HTTP_200_OK,
        )


# ---------------------------------------------------------------------------
# POST /recurring/{id}/pause
# ---------------------------------------------------------------------------


class RecurringPauseView(BaseRecurringView):
    """
    POST /api/v1/recurring/{recurring_transfer_id}/pause

    Klient klika "Wstrzymaj" w aplikacji bankowej. Tylko ACTIVE → PAUSED.
    Pending executions SCHEDULED przejdą SKIPPED przy najbliższym dispatch
    (worker sprawdza status mandate przed execution).
    """

    @idempotent_endpoint
    def post(self, request, recurring_transfer_id):
        bank = request.user
        self.check_bank(bank)

        now = timezone.now()
        with db_transaction.atomic():
            mandate = self.get_owned_mandate_for_update(bank, recurring_transfer_id)

            if mandate.status in RECURRING_TERMINAL_STATUSES:
                raise RecurringTerminatedError()
            if mandate.status != RecurringTransferStatus.ACTIVE:
                raise RecurringNotActiveError()

            mandate.status = RecurringTransferStatus.PAUSED
            mandate.paused_at = now
            mandate.save(update_fields=['status', 'paused_at', 'updated_at'])

        logger.info('recurring paused: id=%s bank=%s', mandate.id, bank.id)

        response_data = {
            'recurring_transfer_id': mandate.id,
            'status': mandate.status,
            'paused_at': mandate.paused_at,
        }
        return Response(
            RecurringPauseResponseSerializer(response_data).data,
            status=status.HTTP_200_OK,
        )


# ---------------------------------------------------------------------------
# POST /recurring/{id}/resume
# ---------------------------------------------------------------------------


class RecurringResumeView(BaseRecurringView):
    """
    POST /api/v1/recurring/{recurring_transfer_id}/resume

    Klient wznawia z pauzy. next_run_at = pierwszy NADCHODZĄCY slot zgodnie
    z cyklem (NIE catch-up missed runów). Reset failed_runs_count (fresh start).
    """

    @idempotent_endpoint
    def post(self, request, recurring_transfer_id):
        bank = request.user
        self.check_bank(bank)

        now = timezone.now()
        with db_transaction.atomic():
            mandate = self.get_owned_mandate_for_update(bank, recurring_transfer_id)

            if mandate.status in RECURRING_TERMINAL_STATUSES:
                raise RecurringTerminatedError()
            if mandate.status != RecurringTransferStatus.PAUSED:
                raise RecurringNotPausedError()

            mandate.status = RecurringTransferStatus.ACTIVE
            mandate.next_run_at = schedule.compute_next_run_at(
                start_date=mandate.start_date,
                cycle=mandate.cycle,
                after=now,
            )
            mandate.failed_runs_count = 0
            mandate.paused_at = None
            mandate.save(
                update_fields=[
                    'status',
                    'next_run_at',
                    'failed_runs_count',
                    'paused_at',
                    'updated_at',
                ]
            )

        logger.info(
            'recurring resumed: id=%s bank=%s next_run_at=%s',
            mandate.id,
            bank.id,
            mandate.next_run_at,
        )

        response_data = {
            'recurring_transfer_id': mandate.id,
            'status': mandate.status,
            'next_run_at': mandate.next_run_at,
            'resumed_at': now,
        }
        return Response(
            RecurringResumeResponseSerializer(response_data).data,
            status=status.HTTP_200_OK,
        )


# ---------------------------------------------------------------------------
# POST /recurring/{id}/cancel
# ---------------------------------------------------------------------------


class RecurringCancelView(BaseRecurringView):
    """
    POST /api/v1/recurring/{recurring_transfer_id}/cancel

    Klient kasuje na stałe. Działa na ACTIVE i PAUSED. Stan terminalny —
    brak resume z CANCELLED. Webhook /cancelled wysyłamy ZAWSZE (R4) —
    częściowo redundantny (bank sam zaintencjonował cancel), ale bank ma
    jeden punkt obsługi end-of-life mandate-a.
    """

    @idempotent_endpoint
    def post(self, request, recurring_transfer_id):
        bank = request.user
        self.check_bank(bank)

        now = timezone.now()
        with db_transaction.atomic():
            mandate = self.get_owned_mandate_for_update(bank, recurring_transfer_id)

            if mandate.status in RECURRING_TERMINAL_STATUSES:
                raise RecurringTerminatedError()

            mandate.status = RecurringTransferStatus.CANCELLED
            mandate.cancelled_at = now
            mandate.save(update_fields=['status', 'cancelled_at', 'updated_at'])

        # Webhook asynchronicznie, poza transakcją DB
        notify_recurring_cancelled.delay(str(mandate.id), 'USER_REQUEST')

        logger.info('recurring cancelled: id=%s bank=%s', mandate.id, bank.id)

        response_data = {
            'recurring_transfer_id': mandate.id,
            'status': mandate.status,
            'cancelled_at': mandate.cancelled_at,
        }
        return Response(
            RecurringCancelResponseSerializer(response_data).data,
            status=status.HTTP_200_OK,
        )


# ---------------------------------------------------------------------------
# GET /recurring/{id}/executions
# ---------------------------------------------------------------------------


class RecurringExecutionsView(BaseRecurringView):
    """
    GET /api/v1/recurring/{recurring_transfer_id}/executions?limit=&before=

    Historia runów mandate-a, najnowsze najpierw. Paginacja kursorem `before`
    (datetime — zwracamy runy z scheduled_for < before).
    """

    def get(self, request, recurring_transfer_id):
        bank = request.user
        self.check_bank(bank)

        try:
            mandate = RecurringTransfer.objects.get(id=recurring_transfer_id)
        except RecurringTransfer.DoesNotExist as e:
            raise RecurringNotFoundError() from e
        if mandate.payer_bank_id != bank.id:
            raise RecurringNotFoundError()

        limit = self._parse_limit(request.query_params.get('limit'))
        qs = mandate.executions.order_by('-scheduled_for')

        before_param = request.query_params.get('before')
        if before_param:
            before = parse_datetime(before_param)
            if before is None:
                raise BadRequestError(detail='Parametr before musi być datetime ISO 8601.')
            qs = qs.filter(scheduled_for__lt=before)

        items = RecurringExecutionItemSerializer(qs[:limit], many=True).data
        return Response({'items': items, 'count': len(items)}, status=status.HTTP_200_OK)

    @staticmethod
    def _parse_limit(raw):
        if raw is None:
            return EXECUTIONS_DEFAULT_LIMIT
        try:
            limit = int(raw)
        except (TypeError, ValueError) as e:
            raise BadRequestError(detail='Parametr limit musi być liczbą.') from e
        if limit < 1:
            raise BadRequestError(detail='Parametr limit musi być >= 1.')
        return min(limit, EXECUTIONS_MAX_LIMIT)
