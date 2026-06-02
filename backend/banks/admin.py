"""Django Admin dla modelu Bank.

Ulepszenia względem poprzedniej wersji:
- webhook_url i cheques_webhook_url jako CharField w formularzu (omija
  rygorystyczny URLField validator — wartość jest walidowana na poziomie modelu,
  ale puste pole lub http://localhost jest dopuszczalne w dev)
- Podgląd API key w trybie dev: plaintext jest zapisywany do cache Redis
  z TTL 120s tuż po rotacji; widoczny jako readonly field w adminie
- Sekcja Cheques w fieldsets
"""

from django import forms
from django.contrib import admin, messages
from django.core.cache import cache
from django.db import transaction
from django.http import HttpResponseRedirect
from django.urls import path, reverse
from django.utils.html import format_html

from banks.models import Bank


# ---------------------------------------------------------------------------
# Formularz — nadpisuje URLField → CharField dla pól webhook
# ---------------------------------------------------------------------------

class BankAdminForm(forms.ModelForm):
    """Własny form który przyjmuje dowolny string (lub pusty) jako URL webhooka.

    Django URLField jest zbyt restrykcyjny dla devu (odrzuca localhost,
    puste wartości, adresy docker-compose). Walidacja URL-a odbywa się
    na poziomie modelu (model.clean()) — tu tylko zbieramy dane.
    """

    webhook_url = forms.CharField(
        required=False,
        label='Webhook URL',
        help_text=(
            'URL endpointu webhooka autoryzacyjnego. '
            'Dopuszczalne: puste, http://localhost:..., http://bank-mock-backend:8100/webhook'
        ),
        widget=forms.TextInput(attrs={'size': 80}),
    )
    cheques_webhook_url = forms.CharField(
        required=False,
        label='Cheques Webhook URL',
        help_text=(
            'URL bazowy dla webhooków Cheques (/redeemed, /released). '
            'Np. http://bank-mock-backend:8100/webhook/cheques. Puste = fallback do webhook_url + "/cheques".'
        ),
        widget=forms.TextInput(attrs={'size': 80}),
    )

    class Meta:
        model = Bank
        fields = '__all__'

    def clean_webhook_url(self):
        return self.cleaned_data.get('webhook_url', '').strip()

    def clean_cheques_webhook_url(self):
        return self.cleaned_data.get('cheques_webhook_url', '').strip()


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------

_API_KEY_CACHE_PREFIX = 'dev:bank_api_key_preview:'
_API_KEY_CACHE_TTL = 300  # 5 minut


@admin.register(Bank)
class BankAdmin(admin.ModelAdmin):
    form = BankAdminForm
    change_form_template = 'admin/banks/bank/change_form.html'

    list_display = (
        'name',
        'zone',
        'currency',
        'active',
        'c2b_enabled',
        'p2p_enabled',
        'cheques_enabled',
        'has_webhook',
        'created_at',
    )
    list_filter = ('zone', 'currency', 'active', 'c2b_enabled', 'p2p_enabled', 'cheques_enabled')
    search_fields = ('name', 'id')
    readonly_fields = ('id', 'api_key_hash', 'api_key_dev_preview', 'created_at', 'updated_at')
    ordering = ('name',)

    fieldsets = (
        (
            'Identyfikacja',
            {'fields': ('id', 'name')},
        ),
        (
            'Konfiguracja domenowa',
            {'fields': ('zone', 'currency', 'debt_limit', 'active')},
        ),
        (
            'Moduły',
            {
                'fields': (
                    'c2b_enabled',
                    'p2p_enabled', 'p2p_lookup_fee',
                    'cheques_enabled', 'cheques_webhook_url',
                ),
            },
        ),
        (
            'Integracja',
            {'fields': ('webhook_url', 'api_key_hash', 'api_key_dev_preview')},
        ),
        (
            'Audyt',
            {'fields': ('created_at', 'updated_at')},
        ),
    )

    actions = ['rotate_api_keys_bulk']

    # ------------------------------------------------------------------
    # Display helpers
    # ------------------------------------------------------------------

    @admin.display(boolean=True, description='Webhook')
    def has_webhook(self, obj: Bank) -> bool:
        return bool(obj.webhook_url)

    @admin.display(description='API Key — podgląd dev (5 min po rotacji)')
    def api_key_dev_preview(self, obj: Bank):
        """Pokazuje plaintext klucza przez 5 minut po rotacji.

        Klucz jest zapisywany do Redis cache tylko po wywołaniu rotate.
        Po TTL znika. Przeznaczone wyłącznie dla środowiska dev.
        """
        if obj.pk is None:
            return '—'
        key = cache.get(f'{_API_KEY_CACHE_PREFIX}{obj.pk}')
        if not key:
            return format_html(
                '<span style="color:#6b7280;font-style:italic">'
                'Niedostępny — wygeneruj nowy klucz przyciskiem powyżej'
                '</span>'
            )
        return format_html(
            '<code style="background:#fef3c7;padding:4px 8px;border-radius:4px;'
            'font-size:0.95em;user-select:all">{}</code>'
            '<span style="color:#92400e;font-size:0.8em;margin-left:8px">'
            '⚠ tylko dev — zniknie po 5 min</span>',
            key,
        )

    # ------------------------------------------------------------------
    # URL dla przycisku "Wygeneruj nowy klucz API"
    # ------------------------------------------------------------------

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path(
                '<uuid:bank_id>/rotate-api-key/',
                self.admin_site.admin_view(self.rotate_api_key_view),
                name='banks_bank_rotate_api_key',
            ),
        ]
        return custom + urls

    def rotate_api_key_view(self, request, bank_id):
        bank = self.get_object(request, bank_id)
        if bank is None:
            messages.error(request, 'Bank nie istnieje.')
            return HttpResponseRedirect(reverse('admin:banks_bank_changelist'))

        if request.method != 'POST':
            messages.error(request, 'Generowanie klucza wymaga akcji POST.')
            return HttpResponseRedirect(reverse('admin:banks_bank_change', args=[bank.pk]))

        with transaction.atomic():
            plaintext = bank.rotate_api_key()
            bank.save(update_fields=['api_key_hash', 'updated_at'])

        # Zapisz do cache — dostępny w api_key_dev_preview przez 5 min
        cache.set(f'{_API_KEY_CACHE_PREFIX}{bank.pk}', plaintext, _API_KEY_CACHE_TTL)

        messages.warning(
            request,
            format_html(
                'Nowy klucz API dla <b>{}</b> (skopiuj teraz — zniknie za 5 min):<br>'
                '<code style="font-size:1.1em;background:#fef3c7;padding:4px 8px">{}</code>',
                bank.name,
                plaintext,
            ),
        )
        return HttpResponseRedirect(reverse('admin:banks_bank_change', args=[bank.pk]))

    # ------------------------------------------------------------------
    # Tworzenie nowego banku — auto-generuj klucz
    # ------------------------------------------------------------------

    def save_model(self, request, obj, form, change):
        if not change:
            plaintext = obj.rotate_api_key()
            super().save_model(request, obj, form, change)
            cache.set(f'{_API_KEY_CACHE_PREFIX}{obj.pk}', plaintext, _API_KEY_CACHE_TTL)
            messages.warning(
                request,
                format_html(
                    'Utworzono bank <b>{}</b>. Klucz API (widoczny też w polu "API Key podgląd dev"):<br>'
                    '<code style="font-size:1.1em;background:#fef3c7;padding:4px 8px">{}</code>',
                    obj.name,
                    plaintext,
                ),
            )
        else:
            super().save_model(request, obj, form, change)

    # ------------------------------------------------------------------
    # Akcja masowa
    # ------------------------------------------------------------------

    @admin.action(description='Wygeneruj nowe klucze API dla zaznaczonych banków')
    def rotate_api_keys_bulk(self, request, queryset):
        rotated = []
        with transaction.atomic():
            for bank in queryset:
                plaintext = bank.rotate_api_key()
                bank.save(update_fields=['api_key_hash', 'updated_at'])
                cache.set(f'{_API_KEY_CACHE_PREFIX}{bank.pk}', plaintext, _API_KEY_CACHE_TTL)
                rotated.append((bank.name, plaintext))

        if not rotated:
            return

        rows = '<br>'.join(
            f'<b>{name}</b>: <code>{key}</code>' for name, key in rotated
        )
        messages.warning(
            request,
            format_html(
                'Wygenerowano {} kluczy (widoczne też w polach "API Key podgląd dev" przez 5 min):<br>{}',
                len(rotated),
                format_html(rows),
            ),
        )