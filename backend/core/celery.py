"""
Celery configuration for KLIK project.
"""

import os

from celery import Celery

# Default settings module dla Celery (nadpisany przez ENV jeśli jest)
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings.dev')

app = Celery('core')

# Bierze konfigurację z Django settings, prefix CELERY_
# Czyli CELERY_BROKER_URL → broker_url, CELERY_TIMEZONE → timezone itd.
app.config_from_object('django.conf:settings', namespace='CELERY')

# Auto-discovery tasków we wszystkich INSTALLED_APPS (szuka pliku tasks.py)
app.autodiscover_tasks()


@app.task(bind=True)
def debug_task(self):
    """Test task — sprawdza czy worker działa."""
    print(f'Request: {self.request!r}')
