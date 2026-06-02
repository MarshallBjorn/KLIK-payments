"""Django app config dla modułu cheques."""

from django.apps import AppConfig


class ChequesConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'cheques'
    verbose_name = 'Czeki (Cheques)'
