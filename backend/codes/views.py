"""Endpointy REST dla apki codes."""

import logging

from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from agents.authentication import XKlikAgentApiKeyAuthentication
from banks.authentication import XKlikBankApiKeyAuthentication
from banks.models import Bank
from codes.enums import TransactionStatus
from codes.permissions import BankHasC2BEnabled
from codes.serializers import (
    CodeGenerateRequestSerializer,
    CodeGenerateResponseSerializer,
    PaymentInitiateRequestSerializer,
    PaymentInitiateResponseSerializer,
    PaymentStatusResponseSerializer,
)
from codes.services import CodeService
from codes.services.exceptions import (
    CodeAlreadyUsedError as ServiceCodeAlreadyUsedError,
)
from codes.services.exceptions import CodeNotFoundError as ServiceCodeNotFoundError
from common.enums import ZONE_CURRENCY
from common.exceptions import (
    CodeAlreadyUsedError,
    CodeExpiredError,
    CurrencyMismatchError,
    MerchantNotFoundError,
    TransactionNotFoundError,
    ZoneMismatchError,
)
from common.idempotency import idempotent_endpoint
from merchants.models import Merchant

logger = logging.getLogger('klik')

# ---------------------------------------------------------------------------
# POST /codes/generate
# ---------------------------------------------------------------------------


class CodeGenerateView(APIView):
    """
    POST /api/v1/codes/generate

    Bank wywołuje aby uzyskać 6-cyfrowy kod dla swojego klienta.
    Auth: X-KLIK-Bank-Api-Key
    """

    authentication_classes = [XKlikBankApiKeyAuthentication]
    permission_classes = [BankHasC2BEnabled]

    @idempotent_endpoint
    def post(self, request):
        serializer = CodeGenerateRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        validated = serializer.validated_data

        bank = request.user

        # Walidacja strefy: zone w request musi == zone banku
        if validated['zone'] != bank.zone:
            raise ZoneMismatchError(
                detail=f'Strefa {validated["zone"]} ≠ strefa banku {bank.zone}.'
            )

        service = CodeService()
        result = service.generate_code(
            bank_id=str(bank.id),
            user_id=validated['user_id'],
            zone=validated['zone'],
        )

        response_serializer = CodeGenerateResponseSerializer(result)
        return Response(response_serializer.data, status=status.HTTP_200_OK)


# ---------------------------------------------------------------------------
# POST /payments/initiate
# ---------------------------------------------------------------------------


class PaymentInitiateView(APIView):
    """
    POST /api/v1/payments/initiate

    Agent (sklep/bramka) wywołuje gdy klient wpisał kod i potwierdza płatność.
    Auth: X-KLIK-Agent-Api-Key
    """

    authentication_classes = [XKlikAgentApiKeyAuthentication]
    permission_classes = []  # auth class waliduje że agent jest aktywny

    @idempotent_endpoint
    def post(self, request):
        serializer = PaymentInitiateRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        validated = serializer.validated_data

        agent = request.user

        # Walidacja currency vs zone agenta
        expected_currency = ZONE_CURRENCY.get(agent.zone)
        if validated['currency'] != expected_currency:
            raise CurrencyMismatchError(
                detail=(
                    f'Strefa agenta ({agent.zone}) wymaga waluty {expected_currency}, '
                    f'otrzymano {validated["currency"]}.'
                )
            )

        # Pobierz merchanta
        try:
            merchant = Merchant.objects.select_related('settlement_bank').get(
                id=validated['merchant_id'],
                active=True,
            )
        except Merchant.DoesNotExist as e:
            raise MerchantNotFoundError() from e

        # Walidacja stref agent ↔ merchant
        if merchant.zone != agent.zone:
            raise ZoneMismatchError(
                detail=f'Strefa merchanta ({merchant.zone}) ≠ strefa agenta ({agent.zone}).'
            )

        # Atomowo oznacz kod jako USED i pobierz payload
        service = CodeService()
        try:
            code_payload = service.mark_used(validated['code'])
        except ServiceCodeNotFoundError as e:
            raise CodeExpiredError() from e
        except ServiceCodeAlreadyUsedError as e:
            raise CodeAlreadyUsedError() from e

        # Walidacja zone kodu
        if code_payload['zone'] != agent.zone:
            raise ZoneMismatchError(
                detail=f'Strefa kodu ({code_payload["zone"]}) ≠ strefa agenta ({agent.zone}).'
            )

        # Pobierz Bank nadawcy
        try:
            sender_bank = Bank.objects.get(id=code_payload['bank_id'])
        except Bank.DoesNotExist as e:
            # To nie powinno się zdarzyć — kod był wystawiony przez ten bank
            logger.error('Bank z code payload nie istnieje: %s', code_payload['bank_id'])
            raise TransactionNotFoundError() from e

        # Wylicz is_on_us
        is_on_us = sender_bank.id == merchant.settlement_bank_id

        from codes.models import Transaction

        # Stwórz Transaction
        idempotency_key = request.headers.get('Idempotency-Key')
        transaction = Transaction.objects.create(
            sender_bank=sender_bank,
            agent=agent,
            merchant=merchant,
            code_snapshot=validated['code'],
            amount_gross=validated['amount'],
            currency=validated['currency'],
            zone=agent.zone,
            is_on_us=is_on_us,
            idempotency_key=idempotency_key,
            status=TransactionStatus.PENDING,
        )

        # Cache statusu w Redisie
        service.cache_transaction_status(
            str(transaction.id),
            TransactionStatus.PENDING,
        )

        # Zleć Celery task: webhook autoryzacyjny do banku
        from codes.tasks import authorize_webhook_task

        authorize_webhook_task.delay(str(transaction.id))

        response_data = {
            'transaction_id': transaction.id,
            'status': transaction.status,
            'status_url': f'/api/v1/payments/status/{transaction.id}',
            'expires_at': _calculate_expires_at(transaction),
        }
        response_serializer = PaymentInitiateResponseSerializer(response_data)
        return Response(response_serializer.data, status=status.HTTP_202_ACCEPTED)


def _calculate_expires_at(transaction):
    """Transakcja wygasa 60s po authorized_at (lub 120s od created_at)."""
    from datetime import timedelta

    expires = transaction.created_at + timedelta(seconds=120)
    return expires.isoformat()


# ---------------------------------------------------------------------------
# GET /payments/status/{transaction_id}
# ---------------------------------------------------------------------------


class PaymentStatusView(APIView):
    """
    GET /api/v1/payments/status/{transaction_id}

    Bank lub Agent może sprawdzać status. Cache Redis najpierw, fallback Postgres.
    Niewłaściwa strona transakcji → 404 (nie ujawniamy że istnieje).
    """

    authentication_classes = [
        XKlikBankApiKeyAuthentication,
        XKlikAgentApiKeyAuthentication,
    ]
    permission_classes = []

    def get(self, request, transaction_id):
        # Najpierw cache
        service = CodeService()
        _ = service.get_transaction_status(transaction_id)

        from codes.models import Transaction

        # Pobierz z Postgres (i tak potrzebne do uprawnień + pełnych danych)
        try:
            transaction = Transaction.objects.select_related(
                'sender_bank',
                'agent',
                'merchant',
            ).get(id=transaction_id)
        except Transaction.DoesNotExist as e:
            raise TransactionNotFoundError() from e

        # Weryfikacja że request.user jest stroną transakcji
        if not _is_party_to_transaction(request.user, transaction):
            # 404 zamiast 403 — security
            raise TransactionNotFoundError()

        # Jeśli cache był aktualny — można by się zatrzymać na statusie z cache,
        # ale i tak zwracamy pełne dane z Postgres
        response_data = {
            'transaction_id': transaction.id,
            'status': transaction.status,
            'amount_gross': transaction.amount_gross,
            'merchant_net': transaction.merchant_net,
            'currency': transaction.currency,
            'created_at': transaction.created_at,
            'completed_at': transaction.completed_at,
        }
        response_serializer = PaymentStatusResponseSerializer(response_data)
        return Response(response_serializer.data, status=status.HTTP_200_OK)


def _is_party_to_transaction(user, transaction) -> bool:
    """Sprawdza czy user jest stroną transakcji."""
    class_name = user.__class__.__name__
    if class_name == 'Bank':
        return transaction.sender_bank_id == user.id
    if class_name == 'Agent':
        return transaction.agent_id == user.id
    return False
