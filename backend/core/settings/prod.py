"""Konfiguracja dla production."""

from .base import *  # noqa: F401,F403

DEBUG = False

# Security
SECURE_SSL_REDIRECT = True
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_HSTS_SECONDS = 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_BROWSER_XSS_FILTER = True
X_FRAME_OPTIONS = 'DENY'

# Dla prod używamy file logging, nie tylko konsoli
LOGGING['handlers']['file'] = {
    'class': 'logging.handlers.RotatingFileHandler',
    'filename': '/var/log/klik/django.log',
    'maxBytes': 10 * 1024 * 1024,  # 10 MB
    'backupCount': 5,
    'formatter': 'verbose',
}
LOGGING['root']['handlers'] = ['console', 'file']  # noqa: F405
