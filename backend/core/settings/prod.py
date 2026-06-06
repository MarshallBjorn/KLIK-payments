"""Konfiguracja dla production."""

import os
from pathlib import Path

from .base import *  # noqa: F401,F403

DEBUG = False

SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')

# Security
SECURE_SSL_REDIRECT = True
SECURE_REDIRECT_EXEMPT = [r'^healthz/']
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_HSTS_SECONDS = 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_BROWSER_XSS_FILTER = True
X_FRAME_OPTIONS = 'DENY'

CSRF_TRUSTED_ORIGINS = env.list('CSRF_TRUSTED_ORIGINS', default=[])  # noqa: F405

LOG_DIR = env('DJANGO_LOG_DIR', default='/var/log/klik')  # noqa: F405
try:
    Path(LOG_DIR).mkdir(parents=True, exist_ok=True)
    LOGGING['handlers']['file'] = {  # noqa: F405
        'class': 'logging.handlers.RotatingFileHandler',
        'filename': os.path.join(LOG_DIR, 'django.log'),
        'maxBytes': 10 * 1024 * 1024,  # 10 MB
        'backupCount': 5,
        'formatter': 'verbose',
        'filters': ['skip_healthz'],
    }
except OSError:
    LOGGING['root']['handlers'] = ['console']  # noqa: F405
