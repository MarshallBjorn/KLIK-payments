"""Konfiguracja dla development."""

from .base import *  # noqa: F401,F403

DEBUG = True

# W dev pozwalamy na wszystko żeby nie męczyć się z hostami
ALLOWED_HOSTS = ['*']

# Verbose logging dla naszej apki
LOGGING['loggers']['klik']['level'] = 'DEBUG'  # noqa: F405

# CORS_ALLOW_ALL_ORIGINS = True

print('>>> Running with DEV settings (DEBUG=True)')

# Origins UI które wolno bić do KLIK API. W split-deployment musi obejmować
# publiczny URL terminala (agenta) — mock-bank UI nie woła KLIK z przeglądarki,
# więc nie potrzebuje wpisu tutaj.
CORS_ALLOWED_ORIGINS = env.list(  # noqa: F405
    'CORS_ALLOWED_ORIGINS',
    default=[
        'http://localhost:5173',
        'http://127.0.0.1:5173',
        'http://localhost:5174',  # mock-bank UI (lokalne testy)
        'http://127.0.0.1:5174',
        'http://localhost:5175',  # agent / terminal
        'http://127.0.0.1:5175',
    ],
)
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
