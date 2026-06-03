"""Django Admin dla apki agents."""

from django.contrib import admin, messages
from django.db import transaction
from django.http import HttpResponseRedirect
from django.urls import path, reverse
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
                'fields': ('settlement_bank', 'account_identifier'),
            },
        ),
        (
            'Bezpieczeństwo',
            {
                'fields': ('api_key_hash',),
                'description': (
                    'Klucz API generuje się przyciskiem "Wygeneruj nowy klucz API" '
                    'na tej stronie lub akcją masową na liście agentów.'
                ),
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

    change_form_template = 'admin/agents/agent/change_form.html'

    @admin.display(description='Account')
    def account_display(self, obj):
        from common.account import format_account_identifier

        return format_account_identifier(obj.account_identifier)

    # ------------------------------------------------------------------
    # Custom URL — przycisk "Wygeneruj nowy klucz API" w widoku edycji
    # ------------------------------------------------------------------

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path(
                '<uuid:agent_id>/rotate-api-key/',
                self.admin_site.admin_view(self.rotate_api_key_view),
                name='agents_agent_rotate_api_key',
            ),
        ]
        # Custom przed defaultami, żeby nie zostały złapane przez `<path:object_id>/`.
        return custom + urls

    def rotate_api_key_view(self, request, agent_id):
        """Endpoint admin uruchamiany przyciskiem na stronie edycji agenta.

        Tylko POST (CSRF chroni przed CSRF-based abuse). Rotuje klucz,
        pokazuje plaintext jeden raz przez messages, redirect do widoku edycji.
        """
        agent = self.get_object(request, agent_id)
        if agent is None:
            messages.error(request, 'Agent nie istnieje.')
            return HttpResponseRedirect(reverse('admin:agents_agent_changelist'))

        if request.method != 'POST':
            # Strażnik na wypadek GET — bez tego dałoby się zrotować klucz przez
            # samo wejście linkiem/odświeżenie (CSRF chroni tylko POST).
            messages.error(request, 'Generowanie klucza wymaga akcji POST.')
            return HttpResponseRedirect(
                reverse('admin:agents_agent_change', args=[agent.pk]),
            )

        with transaction.atomic():
            plaintext, hash_value = generate_api_key()
            agent.api_key_hash = hash_value
            agent.save(update_fields=['api_key_hash', 'updated_at'])

        messages.warning(
            request,
            format_html(
                'Wygenerowano nowy klucz API dla <b>{}</b>. '
                'Skopiuj go teraz — nie zostanie pokazany ponownie:<br>'
                '<code style="font-size:1.1em">{}</code>',
                agent.name,
                plaintext,
            ),
        )
        return HttpResponseRedirect(
            reverse('admin:agents_agent_change', args=[agent.pk]),
        )

    # ------------------------------------------------------------------
    # Akcja masowa (bez zmian)
    # ------------------------------------------------------------------

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

    def save_model(self, request, obj, form, change):
        if not change and not obj.api_key_hash:
            plaintext, hash_value = generate_api_key()
            obj.api_key_hash = hash_value
            super().save_model(request, obj, form, change)
            self.message_user(
                request,
                format_html(
                    'Klucz API dla <b>{}</b>: <code>{}</code><br>'
                    '<b>Skopiuj go teraz — nie będzie widoczny ponownie.</b>',
                    obj.name,
                    plaintext,
                ),
                level=messages.WARNING,
            )
        else:
            super().save_model(request, obj, form, change)


@admin.register(MSCAgreement)
class MSCAgreementAdmin(admin.ModelAdmin):
    list_display = ('agent', 'klik_fee_perc', 'agent_fee_perc', 'valid_from', 'valid_to')
    list_filter = ('agent__zone',)
    search_fields = ('agent__name',)
    readonly_fields = ('id', 'created_at', 'updated_at')
    autocomplete_fields = ('agent',)
