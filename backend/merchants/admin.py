"""Django Admin dla apki merchants."""

from django.contrib import admin

from common.account import format_account_identifier
from merchants.models import Merchant


@admin.register(Merchant)
class MerchantAdmin(admin.ModelAdmin):
    list_display = ('name', 'zone', 'settlement_bank', 'account_display', 'active', 'created_at')
    list_filter = ('zone', 'active')
    search_fields = ('name',)
    readonly_fields = ('id', 'created_at', 'updated_at')
    fieldsets = (
        (
            'Podstawowe',
            {
                'fields': ('id', 'name', 'zone', 'active'),
            },
        ),
        (
            'Rozliczenia',
            {
                'fields': ('settlement_bank', 'account_identifier'),
            },
        ),
        (
            'Audyt',
            {
                'fields': ('created_at', 'updated_at'),
                'classes': ('collapse',),
            },
        ),
    )

    @admin.display(description='Account')
    def account_display(self, obj):
        return format_account_identifier(obj.account_identifier)
