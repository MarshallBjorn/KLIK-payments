"""Konfiguracja dla development."""

from .base import *  # noqa: F401,F403

DEBUG = True

# W dev pozwalamy na wszystko żeby nie męczyć się z hostami
ALLOWED_HOSTS = ['*']

# Verbose logging dla naszej apki
LOGGING['loggers']['klik']['level'] = 'DEBUG'  # noqa: F405

# CORS_ALLOW_ALL_ORIGINS = True

print('>>> Running with DEV settings (DEBUG=True)')

CORS_ALLOWED_ORIGINS = [
    'http://localhost:5173',
    'http://127.0.0.1:5173',
]
CORS_ALLOW_HEADERS = [
    'accept',
    'accept-encoding',
    'authorization',
    'content-type',
    'origin',
    'user-agent',
    'x-klik-agent-api-key',
    'x-klik-bank-api-key',
    'idempotency-key',
]
