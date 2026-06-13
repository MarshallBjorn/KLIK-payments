"""Serializery dla apki recurring.

Serializery robią tylko walidację FORMATU (typy, wymagane pola). Walidacja
domenowa z dedykowanymi kodami błędów (400_INVALID_CYCLE,
400_INVALID_DATE_RANGE, 400_INVALID_PHONE_FORMAT, 422_*) siedzi w widoku —
dzięki temu zwracamy kody z tabeli błędów INFO.md zamiast generycznego
400_BAD_REQUEST.
"""

from rest_framework import serializers

# ---------------------------------------------------------------------------
# POST /recurring/create
# ---------------------------------------------------------------------------


class RecurringCreateRequestSerializer(serializers.Serializer):
    payer_user_id = serializers.CharField(max_length=200)
    recipient_phone = serializers.CharField(max_length=16)
    amount = serializers.DecimalField(max_digits=12, decimal_places=2)
    currency = serializers.RegexField(regex=r'^[A-Z]{3}$', max_length=3)
    zone = serializers.CharField(max_length=2)
    # CharField zamiast ChoiceField — zły cykl ma zwrócić 400_INVALID_CYCLE
    # z widoku, nie 400_BAD_REQUEST z serializera.
    cycle = serializers.CharField(max_length=10)
    start_date = serializers.DateField()
    end_date = serializers.DateField(required=False, allow_null=True, default=None)
    mandate_signed_at = serializers.DateTimeField()


class RecurringCreateResponseSerializer(serializers.Serializer):
    recurring_transfer_id = serializers.UUIDField()
    status = serializers.CharField()
    next_run_at = serializers.DateTimeField()
    created_at = serializers.DateTimeField()


# ---------------------------------------------------------------------------
# GET /recurring/{id}
# ---------------------------------------------------------------------------


class ExecutionsSummarySerializer(serializers.Serializer):
    scheduled = serializers.IntegerField()
    succeeded = serializers.IntegerField()
    failed = serializers.IntegerField()


class RecurringDetailResponseSerializer(serializers.Serializer):
    recurring_transfer_id = serializers.UUIDField()
    status = serializers.CharField()
    payer_user_id = serializers.CharField()
    recipient_phone = serializers.CharField()
    amount = serializers.DecimalField(max_digits=12, decimal_places=2)
    currency = serializers.CharField()
    zone = serializers.CharField()
    cycle = serializers.CharField()
    start_date = serializers.DateField()
    end_date = serializers.DateField(allow_null=True)
    next_run_at = serializers.DateTimeField()
    last_run_at = serializers.DateTimeField(allow_null=True)
    failed_runs_count = serializers.IntegerField()
    executions_summary = ExecutionsSummarySerializer()
    created_at = serializers.DateTimeField()


# ---------------------------------------------------------------------------
# GET /recurring?payer_user_id={id}
# ---------------------------------------------------------------------------


class RecurringListItemSerializer(serializers.Serializer):
    recurring_transfer_id = serializers.UUIDField(source='id')
    status = serializers.CharField()
    recipient_phone = serializers.CharField()
    amount = serializers.DecimalField(max_digits=12, decimal_places=2)
    currency = serializers.CharField()
    cycle = serializers.CharField()
    next_run_at = serializers.DateTimeField()


# ---------------------------------------------------------------------------
# POST /recurring/{id}/pause | /resume | /cancel
# ---------------------------------------------------------------------------


class RecurringPauseResponseSerializer(serializers.Serializer):
    recurring_transfer_id = serializers.UUIDField()
    status = serializers.CharField()
    paused_at = serializers.DateTimeField()


class RecurringResumeResponseSerializer(serializers.Serializer):
    recurring_transfer_id = serializers.UUIDField()
    status = serializers.CharField()
    next_run_at = serializers.DateTimeField()
    resumed_at = serializers.DateTimeField()


class RecurringCancelResponseSerializer(serializers.Serializer):
    recurring_transfer_id = serializers.UUIDField()
    status = serializers.CharField()
    cancelled_at = serializers.DateTimeField()


# ---------------------------------------------------------------------------
# GET /recurring/{id}/executions
# ---------------------------------------------------------------------------


class RecurringExecutionItemSerializer(serializers.Serializer):
    execution_id = serializers.UUIDField(source='id')
    scheduled_for = serializers.DateTimeField()
    executed_at = serializers.DateTimeField(allow_null=True)
    status = serializers.CharField()
    rtp_reference = serializers.SerializerMethodField()
    failure_reason = serializers.SerializerMethodField()

    def get_rtp_reference(self, obj):
        return obj.rtp_reference or None

    def get_failure_reason(self, obj):
        return obj.failure_reason or None
