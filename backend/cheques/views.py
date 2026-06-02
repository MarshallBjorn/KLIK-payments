"""Endpointy REST dla modułu cheques.

Endpointy:
  POST /cheques/issue    — bank wystawia czek (X-KLIK-Bank-Api-Key)
  POST /cheques/redeem   — agent realizuje czek (X-KLIK-Agent-Api-Key)
  POST /cheques/cancel   — bank anuluje czek (X-KLIK-Bank-Api-Key)
  GET  /cheques/status/{cheque_id} — status czeku (bank lub agent)
"""

import logging
import random
import string

from django.db import transaction as db_transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from agents.authentication import XKlikAgentApiKeyAuthentication
from agents.exceptions import NoActiveMSCAgreementError
from agents.services import AgentService
from banks.authentication import XKlikBankApiKeyAuthentication
from cheques.exceptions import (
    ChequeAlreadyCancelledError,
    ChequeAlreadyRedeemedError,
    ChequeExpiredError,
    ChequeNotActiveError,
    ChequeNotFoundError,
    ChequesNotEnabledError,
)
from cheques.models import Cheque, ChequeStatus
from cheques.serializers import (
    ChequeCancelRequestSerializer,
    ChequeCancelResponseSerializer,
    ChequeIssueRequestSerializer,
    ChequeIssueResponseSerializer,
    ChequeRedeemRequestSerializer,
    ChequeRedeemResponseSerializer,
    ChequeStatusResponseSerializer,
)
from cheques.tasks import notify_cheque_redeemed, notify_cheque_released
from codes.enums import TransactionStatus
from codes.models import Transaction
from common.enums import ZONE_CURRENCY
from common.exceptions import (
    BankInactiveError,
    CurrencyMismatchError,
    IdempotencyConflictError,
    InsufficientPermissionsError,
    MerchantNotFoundError,
    NoActiveMSCError,
    ZoneMismatchError,
)
from common.idempotency import idempotent_endpoint
from ledger.services import LedgerService
from merchants.models import Merchant

logger = logging.getLogger('klik')

# ---------------------------------------------------------------------------
# POST /cheques/issue
# ---------------------------------------------------------------------------


class ChequeIssueView(APIView):
    """
    POST /api/v1/cheques/issue

    Bank wywołuje PO zablokowaniu środków klienta. KLIK rejestruje czek
    i zwraca 9-cyfrowy kod jednorazowo.
    Auth: X-KLIK-Bank-Api-Key
    """

    authentication_classes = [XKlikBankApiKeyAuthentication]
    permission_classes = []

    def _check_bank(self, bank):
        if not bank.active:
            raise BankInactiveError()
        if not bank.cheques_enabled:
            raise ChequesNotEnabledError()

    @idempotent_endpoint
    def post(self, request):
        bank = request.user
        self._check_bank(bank)

        serializer = ChequeIssueRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        d = serializer.validated_data

        # Zone musi zgadzać się ze strefą banku
        if d['zone'] != bank.zone:
            raise ZoneMismatchError(
                detail=f'Strefa {d["zone"]} ≠ strefa banku {bank.zone}.'
            )

        # Currency musi pasować do strefy
        expected_currency = ZONE_CURRENCY.get(bank.zone)
        if d['currency'] != expected_currency:
            raise CurrencyMismatchError(
                detail=f'Waluta {d["currency"]} nie pasuje do strefy {bank.zone} (oczekiwano {expected_currency}).'
            )

        now = timezone.now()
        expires_at = now + timezone.timedelta(seconds=d['ttl_seconds'])

        # Idempotency — sprawdź czy ten key był już użyty przez ten bank
        idempotency_key = request.headers.get('Idempotency-Key', '')
        existing = Cheque.objects.filter(
            idempotency_key=idempotency_key,
            issuer_bank=bank,
        ).first()
        if existing:
            # Replay — zwróć ten sam czek
            response_data = {
                'cheque_id': existing.id,
                'code': existing.code,
                'amount': existing.amount,
                'currency': existing.currency,
                'expires_at': existing.expires_at,
                'issued_at': existing.issued_at,
            }
            return Response(
                ChequeIssueResponseSerializer(response_data).data,
                status=status.HTTP_201_CREATED,
            )

        # Generuj unikalny 9-cyfrowy kod (unikaj kolizji z ACTIVE)
        for _ in range(10):
            code = ''.join(random.choices(string.digits, k=9))
            if not Cheque.objects.filter(code=code, status=ChequeStatus.ACTIVE).exists():
                break

        cheque = Cheque.objects.create(
            code=code,
            issuer_bank=bank,
            issuer_user_id=d['user_id'],
            amount=d['amount'],
            currency=d['currency'],
            zone=d['zone'],
            issued_at=now,
            expires_at=expires_at,
            idempotency_key=idempotency_key,
        )

        logger.info(
            'cheque issued: id=%s code=%s bank=%s amount=%s %s ttl=%ds',
            cheque.id, code, bank.id, d['amount'], d['currency'], d['ttl_seconds'],
        )

        response_data = {
            'cheque_id': cheque.id,
            'code': cheque.code,
            'amount': cheque.amount,
            'currency': cheque.currency,
            'expires_at': cheque.expires_at,
            'issued_at': cheque.issued_at,
        }
        return Response(
            ChequeIssueResponseSerializer(response_data).data,
            status=status.HTTP_201_CREATED,
        )


# ---------------------------------------------------------------------------
# POST /cheques/redeem
# ---------------------------------------------------------------------------


class ChequeRedeemView(APIView):
    """
    POST /api/v1/cheques/redeem

    Agent realizuje czek. Operacja atomowa: ACTIVE→REDEEMED + Transaction COMPLETED.
    Auth: X-KLIK-Agent-Api-Key
    """

    authentication_classes = [XKlikAgentApiKeyAuthentication]
    permission_classes = []

    @idempotent_endpoint
    def post(self, request):
        agent = request.user

        serializer = ChequeRedeemRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        d = serializer.validated_data

        # Pobierz merchanta
        try:
            merchant = Merchant.objects.select_related('settlement_bank').get(
                id=d['merchant_id'],
                active=True,
            )
        except Merchant.DoesNotExist as e:
            raise MerchantNotFoundError() from e

        # Walidacja stref
        if merchant.zone != agent.zone:
            raise ZoneMismatchError(
                detail=f'Strefa merchanta ({merchant.zone}) ≠ strefa agenta ({agent.zone}).'
            )

        now = timezone.now()

        with db_transaction.atomic():
            # SELECT FOR UPDATE — blokada wyścigu
            try:
                cheque = Cheque.objects.select_for_update().get(
                    code=d['cheque_code'],
                    status=ChequeStatus.ACTIVE,
                )
            except Cheque.DoesNotExist:
                # Sprawdź czy czek istnieje w ogóle (dla lepszego error msg)
                historical = Cheque.objects.filter(code=d['cheque_code']).first()
                if historical:
                    if historical.status == ChequeStatus.REDEEMED:
                        raise ChequeAlreadyRedeemedError()
                    if historical.status == ChequeStatus.CANCELLED:
                        raise ChequeAlreadyCancelledError()
                    if historical.status == ChequeStatus.EXPIRED:
                        raise ChequeExpiredError()
                raise ChequeNotFoundError()

            # Sprawdź czy nie wygasł już (może jeszcze ACTIVE ale expires_at minął)
            if cheque.expires_at <= now:
                cheque.status = ChequeStatus.EXPIRED
                cheque.expired_at = now
                cheque.save(update_fields=['status', 'expired_at', 'updated_at'])
                notify_cheque_released.delay(str(cheque.id), 'EXPIRED')
                raise ChequeExpiredError()

            # Walidacja strefy czeku vs agenta
            if cheque.zone != agent.zone:
                raise ZoneMismatchError(
                    detail=f'Strefa czeku ({cheque.zone}) ≠ strefa agenta ({agent.zone}).'
                )

            # Kalkulacja splitu
            try:
                split = AgentService.calculate_split(agent, cheque.amount)
            except NoActiveMSCAgreementError as e:
                raise NoActiveMSCError() from e

            is_on_us = cheque.issuer_bank_id == merchant.settlement_bank_id

            # Utwórz Transaction (status=COMPLETED od razu — bez PENDING/AUTHORIZED)
            idempotency_key = request.headers.get('Idempotency-Key', '')
            transaction = Transaction.objects.create(
                sender_bank=cheque.issuer_bank,
                agent=agent,
                merchant=merchant,
                code_snapshot=cheque.code,  # 9 cyfr zamiast 6
                amount_gross=cheque.amount,
                klik_fee=split['klik_fee'],
                agent_fee=split['agent_fee'],
                merchant_net=split['merchant_net'],
                currency=cheque.currency,
                zone=cheque.zone,
                is_on_us=is_on_us,
                status=TransactionStatus.COMPLETED,
                idempotency_key=idempotency_key,
                authorized_at=now,
                completed_at=now,
            )

            # Zaktualizuj czek → REDEEMED
            cheque.status = ChequeStatus.REDEEMED
            cheque.redeemed_at = now
            cheque.transaction = transaction
            cheque.save(update_fields=['status', 'redeemed_at', 'transaction', 'updated_at'])

        # Ledger entries (poza transakcją DB — LedgerService ma własny atomic)
        try:
            LedgerService.record_c2b_transaction(transaction)
        except Exception:
            logger.exception('cheque redeem: ledger failed for transaction %s', transaction.id)

        # Webhook do banku wystawcy — asynchronicznie
        notify_cheque_redeemed.delay(str(cheque.id))

        logger.info(
            'cheque redeemed: cheque=%s tx=%s amount=%s agent=%s merchant=%s',
            cheque.id, transaction.id, cheque.amount, agent.id, merchant.id,
        )

        response_data = {
            'cheque_id': cheque.id,
            'transaction_id': transaction.id,
            'amount_gross': cheque.amount,
            'merchant_net': split['merchant_net'],
            'klik_fee': split['klik_fee'],
            'agent_fee': split['agent_fee'],
            'currency': cheque.currency,
            'redeemed_at': cheque.redeemed_at,
        }
        return Response(
            ChequeRedeemResponseSerializer(response_data).data,
            status=status.HTTP_200_OK,
        )


# ---------------------------------------------------------------------------
# POST /cheques/cancel
# ---------------------------------------------------------------------------


class ChequeCancelView(APIView):
    """
    POST /api/v1/cheques/cancel

    Bank anuluje czek wystawiony przez siebie. KLIK zmienia stan na CANCELLED
    i kolejkuje webhook /released do banku.
    Auth: X-KLIK-Bank-Api-Key
    """

    authentication_classes = [XKlikBankApiKeyAuthentication]
    permission_classes = []

    def _check_bank(self, bank):
        if not bank.active:
            raise BankInactiveError()
        if not bank.cheques_enabled:
            raise ChequesNotEnabledError()

    @idempotent_endpoint
    def post(self, request):
        bank = request.user
        self._check_bank(bank)

        serializer = ChequeCancelRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        d = serializer.validated_data

        now = timezone.now()

        with db_transaction.atomic():
            try:
                cheque = Cheque.objects.select_for_update().get(id=d['cheque_id'])
            except Cheque.DoesNotExist as e:
                raise ChequeNotFoundError() from e

            # Tylko bank wystawcy może anulować
            if cheque.issuer_bank_id != bank.id:
                raise InsufficientPermissionsError()

            if cheque.status != ChequeStatus.ACTIVE:
                if cheque.status == ChequeStatus.REDEEMED:
                    raise ChequeAlreadyRedeemedError()
                if cheque.status == ChequeStatus.CANCELLED:
                    raise ChequeAlreadyCancelledError()
                raise ChequeNotActiveError()

            cheque.status = ChequeStatus.CANCELLED
            cheque.cancelled_at = now
            cheque.save(update_fields=['status', 'cancelled_at', 'updated_at'])

        # Webhook asynchronicznie
        notify_cheque_released.delay(str(cheque.id), 'CANCELLED')

        logger.info('cheque cancelled: cheque=%s bank=%s', cheque.id, bank.id)

        response_data = {
            'cheque_id': cheque.id,
            'status': cheque.status,
            'cancelled_at': cheque.cancelled_at,
        }
        return Response(
            ChequeCancelResponseSerializer(response_data).data,
            status=status.HTTP_200_OK,
        )


# ---------------------------------------------------------------------------
# GET /cheques/status/{cheque_id}
# ---------------------------------------------------------------------------


class ChequeStatusView(APIView):
    """
    GET /api/v1/cheques/status/{cheque_id}

    Sprawdź status czeku. Dostępne dla banku wystawcy i agenta który zrealizował.
    Auth: X-KLIK-Bank-Api-Key lub X-KLIK-Agent-Api-Key
    """

    # Oba typy auth — DRF spróbuje każdy po kolei
    authentication_classes = [
        XKlikBankApiKeyAuthentication,
        XKlikAgentApiKeyAuthentication,
    ]
    permission_classes = []

    def get(self, request, cheque_id):
        user = request.user

        try:
            cheque = Cheque.objects.select_related('transaction').get(id=cheque_id)
        except Cheque.DoesNotExist as e:
            raise ChequeNotFoundError() from e

        # Kontrola dostępu:
        # - bank widzi tylko swoje czeki
        # - agent widzi tylko czeki które zrealizował
        from banks.models import Bank
        from agents.models import Agent

        if isinstance(user, Bank):
            if cheque.issuer_bank_id != user.id:
                raise ChequeNotFoundError()
        elif isinstance(user, Agent):
            if cheque.transaction is None or cheque.transaction.agent_id != user.id:
                raise ChequeNotFoundError()
        else:
            raise ChequeNotFoundError()

        response_data = {
            'cheque_id': cheque.id,
            'status': cheque.status,
            'amount': cheque.amount,
            'currency': cheque.currency,
            'zone': cheque.zone,
            'issued_at': cheque.issued_at,
            'expires_at': cheque.expires_at,
            'redeemed_at': cheque.redeemed_at,
            'transaction_id': cheque.transaction_id if cheque.transaction_id else None,
        }
        return Response(
            ChequeStatusResponseSerializer(response_data).data,
            status=status.HTTP_200_OK,
        )
