"""Konfiguracja dla development."""

from .base import *  # noqa: F401,F403

DEBUG = True

# W dev pozwalamy na wszystko żeby nie męczyć się z hostami
ALLOWED_HOSTS = ['*']

# Verbose logging dla naszej apki
LOGGING['loggers']['klik']['level'] = 'DEBUG'  # noqa: F405

# CORS_ALLOW_ALL_ORIGINS = True

print('>>> Running with DEV settings (DEBUG=True)')
