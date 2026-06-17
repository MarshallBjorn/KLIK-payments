"""Django app config dla modułu recurring."""

from django.apps import AppConfig


class RecurringConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'recurring'
    verbose_name = 'Regularne transfery (Recurring)'
