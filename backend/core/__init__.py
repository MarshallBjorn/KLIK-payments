# Importujemy Celery żeby @shared_task działało wszędzie
from .celery import app as celery_app

__all__ = ('celery_app',)
