"""Django Admin dla modułu Cheques."""

from django.contrib import admin
from django.utils.html import format_html

from cheques.models import Cheque, ChequeStatus


@admin.register(Cheque)
class ChequeAdmin(admin.ModelAdmin):
    list_display = (
        'code',
        'status_badge',
        'amount',
        'currency',
        'zone',
        'issuer_bank',
        'issuer_user_id',
        'issued_at',
        'expires_at',
    )
    list_filter = ('status', 'zone', 'currency', 'issuer_bank')
    search_fields = ('code', 'issuer_user_id', 'id')
    readonly_fields = (
        'id',
        'code',
        'issuer_bank',
        'issuer_user_id',
        'amount',
        'currency',
        'zone',
        'issued_at',
        'expires_at',
        'redeemed_at',
        'cancelled_at',
        'expired_at',
        'transaction',
        'idempotency_key',
        'created_at',
        'updated_at',
    )
    ordering = ('-created_at',)
    date_hierarchy = 'issued_at'

    fieldsets = (
        ('Identyfikacja', {'fields': ('id', 'code', 'idempotency_key')}),
        ('Wystawca', {'fields': ('issuer_bank', 'issuer_user_id')}),
        ('Kwota', {'fields': ('amount', 'currency', 'zone')}),
        (
            'Stan',
            {
                'fields': (
                    'status',
                    'issued_at',
                    'expires_at',
                    'redeemed_at',
                    'cancelled_at',
                    'expired_at',
                )
            },
        ),
        ('Powiązania', {'fields': ('transaction',)}),
        ('Audyt', {'fields': ('created_at', 'updated_at')}),
    )

    @admin.display(description='Status')
    def status_badge(self, obj):
        colors = {
            ChequeStatus.ACTIVE: '#16a34a',
            ChequeStatus.REDEEMED: '#2563eb',
            ChequeStatus.CANCELLED: '#dc2626',
            ChequeStatus.EXPIRED: '#6b7280',
        }
        color = colors.get(obj.status, '#6b7280')
        return format_html(
            '<span style="color: {}; font-weight: bold;">{}</span>',
            color,
            obj.status,
        )

    def has_add_permission(self, request):
        return False  # Czeki są tworzone tylko przez /cheques/issue

    def has_delete_permission(self, request, obj=None):
        return False  # Audit trail — nie usuwamy
