"""Serializery dla apki cheques."""

from decimal import Decimal

from rest_framework import serializers

from common.enums import Zone

# ---------------------------------------------------------------------------
# POST /cheques/issue
# ---------------------------------------------------------------------------

CHEQUE_TTL_MIN_SECONDS = 3600  # 1h
CHEQUE_TTL_MAX_SECONDS = 259200  # 72h
CHEQUE_TTL_DEFAULT_SECONDS = 86400  # 24h


class ChequeIssueRequestSerializer(serializers.Serializer):
    user_id = serializers.CharField(max_length=200)
    amount = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=Decimal('0.01'))
    currency = serializers.RegexField(regex=r'^[A-Z]{3}$', max_length=3)
    zone = serializers.ChoiceField(choices=Zone.choices)
    ttl_seconds = serializers.IntegerField(required=False, default=CHEQUE_TTL_DEFAULT_SECONDS)

    def validate_ttl_seconds(self, value):
        if not (CHEQUE_TTL_MIN_SECONDS <= value <= CHEQUE_TTL_MAX_SECONDS):
            raise serializers.ValidationError(
                f'ttl_seconds musi być między {CHEQUE_TTL_MIN_SECONDS} a {CHEQUE_TTL_MAX_SECONDS}.'
            )
        return value


class ChequeIssueResponseSerializer(serializers.Serializer):
    cheque_id = serializers.UUIDField()
    code = serializers.CharField()
    amount = serializers.DecimalField(max_digits=12, decimal_places=2)
    currency = serializers.CharField()
    expires_at = serializers.DateTimeField()
    issued_at = serializers.DateTimeField()


# ---------------------------------------------------------------------------
# POST /cheques/redeem
# ---------------------------------------------------------------------------


class ChequeRedeemRequestSerializer(serializers.Serializer):
    cheque_code = serializers.RegexField(regex=r'^\d{9}$', max_length=9)
    merchant_id = serializers.UUIDField()


class ChequeRedeemResponseSerializer(serializers.Serializer):
    cheque_id = serializers.UUIDField()
    transaction_id = serializers.UUIDField()
    amount_gross = serializers.DecimalField(max_digits=12, decimal_places=2)
    merchant_net = serializers.DecimalField(max_digits=12, decimal_places=2)
    klik_fee = serializers.DecimalField(max_digits=12, decimal_places=2)
    agent_fee = serializers.DecimalField(max_digits=12, decimal_places=2)
    currency = serializers.CharField()
    redeemed_at = serializers.DateTimeField()


# ---------------------------------------------------------------------------
# POST /cheques/cancel
# ---------------------------------------------------------------------------


class ChequeCancelRequestSerializer(serializers.Serializer):
    cheque_id = serializers.UUIDField()


class ChequeCancelResponseSerializer(serializers.Serializer):
    cheque_id = serializers.UUIDField()
    status = serializers.CharField()
    cancelled_at = serializers.DateTimeField()


# ---------------------------------------------------------------------------
# GET /cheques/status/{cheque_id}
# ---------------------------------------------------------------------------


class ChequeStatusResponseSerializer(serializers.Serializer):
    cheque_id = serializers.UUIDField()
    status = serializers.CharField()
    amount = serializers.DecimalField(max_digits=12, decimal_places=2)
    currency = serializers.CharField()
    zone = serializers.CharField()
    issued_at = serializers.DateTimeField()
    expires_at = serializers.DateTimeField()
    redeemed_at = serializers.DateTimeField(allow_null=True)
    transaction_id = serializers.UUIDField(allow_null=True)
