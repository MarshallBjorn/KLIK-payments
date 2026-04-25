"""
Django Admin dla modelu Bank.

Funkcje:
- list/edit standardowy
- akcja masowa "Generuj nowe klucze API" z poziomu listy
- przycisk "Wygeneruj nowy klucz API" na stronie edycji pojedynczego banku
- wyświetlenie plaintext klucza JEDEN RAZ przez `messages` po wygenerowaniu

Plaintext klucza nigdy nie jest zapisywany — operator musi go skopiować
od razu i przekazać bankowi (krok 2 w A0).
"""

from django.contrib import admin, messages
from django.db import transaction
from django.http import HttpResponseRedirect
from django.urls import path, reverse
from django.utils.html import format_html

from banks.models import Bank


@admin.register(Bank)
class BankAdmin(admin.ModelAdmin):
    list_display = ("name", "zone", "currency", "active", "has_webhook", "created_at")
    list_filter = ("zone", "currency", "active")
    search_fields = ("name", "id")
    readonly_fields = ("id", "api_key_hash", "created_at", "updated_at")
    ordering = ("name",)

    fieldsets = (
        (
            "Identyfikacja",
            {"fields": ("id", "name")},
        ),
        (
            "Konfiguracja domenowa",
            {"fields": ("zone", "currency", "debt_limit", "active")},
        ),
        (
            "Integracja",
            {"fields": ("webhook_url", "api_key_hash")},
        ),
        (
            "Audyt",
            {"fields": ("created_at", "updated_at")},
        ),
    )

    actions = ["rotate_api_keys_bulk"]

    # ------------------------------------------------------------------
    # Display helpers
    # ------------------------------------------------------------------

    @admin.display(boolean=True, description="Webhook")
    def has_webhook(self, obj: Bank) -> bool:
        return bool(obj.webhook_url)

    # ------------------------------------------------------------------
    # Custom URL — przycisk "Wygeneruj nowy klucz API" w widoku edycji
    # ------------------------------------------------------------------

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path(
                "<uuid:bank_id>/rotate-api-key/",
                self.admin_site.admin_view(self.rotate_api_key_view),
                name="banks_bank_rotate_api_key",
            ),
        ]
        # Custom przed defaultami, żeby nie zostały złapane przez `<path:object_id>/`.
        return custom + urls

    def rotate_api_key_view(self, request, bank_id):
        """Endpoint admin uruchamiany przyciskiem na stronie edycji banku.

        Tylko POST (CSRF chroni przed CSRF-based abuse). Logika minimalna:
        rotuj klucz, pokaż plaintext jeden raz przez messages, redirect do
        widoku edycji.
        """
        bank = self.get_object(request, bank_id)
        if bank is None:
            messages.error(request, "Bank nie istnieje.")
            return HttpResponseRedirect(reverse("admin:banks_bank_changelist"))

        if request.method != "POST":
            # Strażnik na wypadek GET (np. przejście linkiem). Bez tego dałoby się
            # zrotować klucz przez przeglądarkę przy odświeżeniu strony, co jest
            # niebezpieczne (CSRF chroni tylko POST).
            messages.error(request, "Generowanie klucza wymaga akcji POST.")
            return HttpResponseRedirect(
                reverse("admin:banks_bank_change", args=[bank.pk]),
            )

        with transaction.atomic():
            plaintext = bank.rotate_api_key()
            bank.save(update_fields=["api_key_hash", "updated_at"])

        messages.warning(
            request,
            format_html(
                "Wygenerowano nowy klucz API dla <b>{}</b>. "
                "Skopiuj go teraz — nie zostanie pokazany ponownie:<br>"
                '<code style="font-size:1.1em">{}</code>',
                bank.name,
                plaintext,
            ),
        )
        return HttpResponseRedirect(
            reverse("admin:banks_bank_change", args=[bank.pk]),
        )

    # ------------------------------------------------------------------
    # Renderowanie przycisku w formularzu edycji
    # ------------------------------------------------------------------

    change_form_template = "admin/banks/bank/change_form.html"

    # ------------------------------------------------------------------
    # Akcja masowa
    # ------------------------------------------------------------------

    @admin.action(description="Wygeneruj nowe klucze API dla zaznaczonych banków")
    def rotate_api_keys_bulk(self, request, queryset):
        """Akcja na liście — rotuje klucze dla wielu banków naraz.

        Pokazuje wszystkie plaintexty w jednym message; operator musi skopiować
        zanim zamknie stronę.
        """
        rotated = []
        with transaction.atomic():
            for bank in queryset:
                plaintext = bank.rotate_api_key()
                bank.save(update_fields=["api_key_hash", "updated_at"])
                rotated.append((bank.name, plaintext))

        if not rotated:
            return

        rows = format_html(
            "<br>".join(
                ["<b>{}</b>: <code>{}</code>".format(*r) for r in rotated],
            ),
        )
        messages.warning(
            request,
            format_html(
                "Wygenerowano {} nowych kluczy. Skopiuj je teraz:<br>{}",
                len(rotated),
                rows,
            ),
        )

    # ------------------------------------------------------------------
    # Tworzenie nowego banku — automatycznie generuj klucz przy save
    # ------------------------------------------------------------------

    def save_model(self, request, obj, form, change):
        """Przy tworzeniu nowego banku auto-generujemy pierwszy klucz API.

        Bez tego operator musiałby najpierw zapisać banku (z pustym hashem,
        co psuje constraint unique), potem ręcznie zrotować — niepraktyczne.
        """
        if not change:  # Tworzenie nowego rekordu
            plaintext = obj.rotate_api_key()
            super().save_model(request, obj, form, change)
            messages.warning(
                request,
                format_html(
                    "Utworzono bank <b>{}</b>. Klucz API (zapisz teraz, "
                    "nie zostanie pokazany ponownie):<br><code>{}</code>",
                    obj.name,
                    plaintext,
                ),
            )
        else:
            super().save_model(request, obj, form, change)
