"""
Django Admin dla modelu Alias.

Operator KLIK używa tego do:
- Audytu i przeglądu rejestru P2P (ile aliasów per bank, per strefa).
- Awaryjnego usunięcia aliasu (np. fraud, zgłoszenie organów ścigania).
- Manualnego zarejestrowania (testy, demo).

Bezpieczeństwo: nie wystawiamy account_identifier jako pełnego pola edycji
żeby zminimalizować ryzyko literówki przy ręcznym wpisywaniu IBAN-u — ale
JSONField w Django 5 ma już sensowny widget z walidacją.
"""

from django.contrib import admin

from aliases.models import Alias


@admin.register(Alias)
class AliasAdmin(admin.ModelAdmin):
    list_display = ("phone", "bank", "zone", "created_at")
    list_filter = ("zone", "bank")
    search_fields = ("phone", "bank__name", "id")
    readonly_fields = ("id", "created_at", "updated_at")
    ordering = ("-created_at",)

    fieldsets = (
        (
            "Identyfikacja",
            {"fields": ("id", "phone", "zone")},
        ),
        (
            "Powiązanie bankowe",
            {"fields": ("bank", "account_identifier")},
        ),
        (
            "Audyt",
            {"fields": ("created_at", "updated_at")},
        ),
    )
