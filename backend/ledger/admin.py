"""Django Admin dla apki ledger."""

from django.contrib import admin

from ledger.models import LedgerEntry, SettlementSession, SettlementTransfer


class SettlementTransferInline(admin.TabularInline):
    model = SettlementTransfer
    extra = 0
    readonly_fields = (
        'id',
        'from_bank',
        'to_bank',
        'amount',
        'currency',
        'status',
        'rtgs_reference',
        'created_at',
        'dispatched_at',
        'completed_at',
    )
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(SettlementSession)
class SettlementSessionAdmin(admin.ModelAdmin):
    list_display = (
        'id',
        'zone',
        'status',
        'started_at',
        'ended_at',
        'total_entries_count',
        'total_volume',
    )
    list_filter = ('zone', 'status')
    search_fields = ('id',)
    readonly_fields = (
        'id',
        'zone',
        'started_at',
        'ended_at',
        'status',
        'total_entries_count',
        'total_volume',
        'created_at',
        'updated_at',
    )
    ordering = ('-started_at',)
    inlines = [SettlementTransferInline]

    def has_add_permission(self, request):
        return False  # Sesje tworzone przez LedgerService

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(LedgerEntry)
class LedgerEntryAdmin(admin.ModelAdmin):
    list_display = (
        'id',
        'entry_type',
        'amount',
        'currency',
        'zone',
        'from_bank',
        'to_bank',
        'beneficiary_type',
        'settled',
        'created_at',
    )
    list_filter = ('entry_type', 'beneficiary_type', 'zone', 'settled', 'currency')
    search_fields = ('id', 'source_ref')
    readonly_fields = (
        'id',
        'from_bank',
        'to_bank',
        'beneficiary_type',
        'beneficiary_ref',
        'amount',
        'currency',
        'zone',
        'entry_type',
        'source_ref',
        'session',
        'settled',
        'settled_at',
        'created_at',
    )
    ordering = ('-created_at',)

    def has_add_permission(self, request):
        return False  # Entries tworzone tylko przez LedgerService

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(SettlementTransfer)
class SettlementTransferAdmin(admin.ModelAdmin):
    list_display = (
        'id',
        'session',
        'from_bank',
        'to_bank',
        'amount',
        'currency',
        'status',
        'created_at',
    )
    list_filter = ('status', 'currency')
    search_fields = ('id', 'rtgs_reference', 'session__id')
    readonly_fields = (
        'id',
        'session',
        'from_bank',
        'to_bank',
        'amount',
        'currency',
        'status',
        'rtgs_reference',
        'created_at',
        'dispatched_at',
        'completed_at',
    )
    ordering = ('-created_at',)

    def has_add_permission(self, request):
        return False
