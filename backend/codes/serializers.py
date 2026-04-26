"""Serializery dla apki codes."""

from decimal import Decimal

from rest_framework import serializers

from common.enums import Zone

# ---------------------------------------------------------------------------
# /codes/generate
# ---------------------------------------------------------------------------


class CodeGenerateRequestSerializer(serializers.Serializer):
    user_id = serializers.CharField(max_length=200)
    zone = serializers.ChoiceField(choices=Zone.choices)


class CodeGenerateResponseSerializer(serializers.Serializer):
    code = serializers.CharField()
    expires_in = serializers.IntegerField()
    expires_at = serializers.CharField()


# ---------------------------------------------------------------------------
# /payments/initiate
# ---------------------------------------------------------------------------


class PaymentInitiateRequestSerializer(serializers.Serializer):
    code = serializers.RegexField(regex=r'^\d{6}$', max_length=6)
    amount = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=Decimal('0.01'))
    currency = serializers.RegexField(regex=r'^[A-Z]{3}$', max_length=3)
    merchant_id = serializers.UUIDField()


class PaymentInitiateResponseSerializer(serializers.Serializer):
    transaction_id = serializers.UUIDField()
    status = serializers.CharField()
    status_url = serializers.CharField()
    expires_at = serializers.CharField()


# ---------------------------------------------------------------------------
# /payments/status/{transaction_id}
# ---------------------------------------------------------------------------


class PaymentStatusResponseSerializer(serializers.Serializer):
    transaction_id = serializers.UUIDField()
    status = serializers.CharField()
    amount_gross = serializers.DecimalField(max_digits=12, decimal_places=2)
    merchant_net = serializers.DecimalField(
        max_digits=12,
        decimal_places=2,
        allow_null=True,
    )
    currency = serializers.CharField()
    created_at = serializers.DateTimeField()
    completed_at = serializers.DateTimeField(allow_null=True)
