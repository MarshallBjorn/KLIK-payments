"""Django Admin dla apki agents."""

from django.contrib import admin, messages
from django.utils.html import format_html

from agents.authentication import generate_api_key
from agents.models import Agent, MSCAgreement


class MSCAgreementInline(admin.TabularInline):
    model = MSCAgreement
    extra = 0
    fields = ('klik_fee_perc', 'agent_fee_perc', 'valid_from', 'valid_to', 'created_at')
    readonly_fields = ('created_at',)


@admin.register(Agent)
class AgentAdmin(admin.ModelAdmin):
    list_display = ('name', 'zone', 'settlement_bank', 'account_display', 'active', 'created_at')
    list_filter = ('zone', 'active')
    search_fields = ('name',)
    readonly_fields = ('id', 'api_key_hash', 'created_at', 'updated_at')
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
                'fields': ('settlement_bank', 'iban'),
            },
        ),
        (
            'Bezpieczeństwo',
            {
                'fields': ('api_key_hash',),
                'description': 'Klucze API generuje się przez akcję "Generate new API key".',
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
    inlines = [MSCAgreementInline]
    actions = ['generate_new_api_key']

    @admin.display(description='Account')
    def account_display(self, obj):
        from common.account import format_account_identifier

        return format_account_identifier(obj.account_identifier)

    @admin.action(description='Wygeneruj nowy klucz API (unieważnia stary)')
    def generate_new_api_key(self, request, queryset):
        if queryset.count() != 1:
            self.message_user(
                request,
                'Wybierz dokładnie jednego agenta.',
                level=messages.ERROR,
            )
            return

        agent = queryset.first()
        plaintext, hash_value = generate_api_key()
        agent.api_key_hash = hash_value
        agent.save(update_fields=['api_key_hash', 'updated_at'])

        self.message_user(
            request,
            format_html(
                'Nowy klucz API dla <b>{}</b>: <code>{}</code><br>'
                '<b>Skopiuj go teraz — nie będzie widoczny ponownie.</b>',
                agent.name,
                plaintext,
            ),
            level=messages.WARNING,
        )


@admin.register(MSCAgreement)
class MSCAgreementAdmin(admin.ModelAdmin):
    list_display = ('agent', 'klik_fee_perc', 'agent_fee_perc', 'valid_from', 'valid_to')
    list_filter = ('agent__zone',)
    search_fields = ('agent__name',)
    readonly_fields = ('id', 'created_at', 'updated_at')
    autocomplete_fields = ('agent',)
