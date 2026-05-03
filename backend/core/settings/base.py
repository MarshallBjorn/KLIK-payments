"""
Wspólna konfiguracja dla wszystkich środowisk.
Środowiskowe nadpisania w dev.py i prod.py.
"""

from pathlib import Path

import environ

# BASE_DIR wskazuje na backend/ (gdzie jest manage.py)
# settings/base.py → settings/ → core/ → backend/
BASE_DIR = Path(__file__).resolve().parent.parent.parent

env = environ.Env()
environ.Env.read_env(BASE_DIR.parent / '.env')

# ============================================================
# Security
# ============================================================
SECRET_KEY = env('SECRET_KEY')
ALLOWED_HOSTS = env.list('ALLOWED_HOSTS', default=['localhost', '127.0.0.1'])

# ============================================================
# Application definition
# ============================================================
DJANGO_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
]

THIRD_PARTY_APPS = [
    'rest_framework',
    'django_celery_beat',
]

# Apki projektowe
LOCAL_APPS = [
    'common',
    'banks',
    'agents',
    'merchants',
    'aliases',
    'codes',
    'ledger',
]

INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'core.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'core.wsgi.application'
ASGI_APPLICATION = 'core.asgi.application'

# ============================================================
# Database
# ============================================================
DATABASES = {
    'default': env.db('DATABASE_URL'),
}

# ============================================================
# Cache (Redis)
# ============================================================
CACHES = {
    'default': {
        'BACKEND': 'django_redis.cache.RedisCache',
        'LOCATION': env('REDIS_URL'),
        'OPTIONS': {
            'CLIENT_CLASS': 'django_redis.client.DefaultClient',
        },
    }
}

# ============================================================
# Celery
# ============================================================
CELERY_BROKER_URL = env('CELERY_BROKER_URL')
CELERY_RESULT_BACKEND = env('CELERY_RESULT_BACKEND')
CELERY_ACCEPT_CONTENT = ['json']
CELERY_TASK_SERIALIZER = 'json'
CELERY_RESULT_SERIALIZER = 'json'
CELERY_TIMEZONE = 'UTC'
CELERY_BEAT_SCHEDULER = 'django_celery_beat.schedulers:DatabaseScheduler'
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True

# ============================================================
# DRF
# ============================================================
REST_FRAMEWORK = {
    'DEFAULT_RENDERER_CLASSES': [
        'rest_framework.renderers.JSONRenderer',
    ],
    'DEFAULT_PARSER_CLASSES': [
        'rest_framework.parsers.JSONParser',
    ],
    # Auth domyślny — w MVP używamy custom X-KLIK-Api-Key (do dopisania)
    'DEFAULT_AUTHENTICATION_CLASSES': [],
    'DEFAULT_PERMISSION_CLASSES': [
        'rest_framework.permissions.AllowAny',
    ],
    # NOWE: globalny handler błędów ujednolicający format JSON do
    # {"error": {"code": ..., "message": ..., "timestamp": ...}}
    # zgodnie z docs/c2b/integration/INFO.md.
    'EXCEPTION_HANDLER': 'common.exceptions.klik_exception_handler',
}
# ============================================================
# Password validation
# ============================================================
AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

# ============================================================
# i18n
# ============================================================
LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True

# ============================================================
# Static / media
# ============================================================
STATIC_URL = 'static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# ============================================================
# KLIK — konfiguracja domenowa
# ============================================================
KLIK_CODE_TTL_SECONDS = env.int('KLIK_CODE_TTL_SECONDS', default=120)

# Interwały sesji rozliczeniowych per strefa (w minutach).
# Default produkcyjny: 1440 (24h). Dla demo można obniżyć do 2-15.
SESSION_INTERVAL_MINUTES_PL = env.int('SESSION_INTERVAL_MINUTES_PL', default=1440)
SESSION_INTERVAL_MINUTES_EU = env.int('SESSION_INTERVAL_MINUTES_EU', default=1440)
SESSION_INTERVAL_MINUTES_UK = env.int('SESSION_INTERVAL_MINUTES_UK', default=1440)
SESSION_INTERVAL_MINUTES_US = env.int('SESSION_INTERVAL_MINUTES_US', default=1440)

# Timeout HTTP dispatcher → RTGS w sekundach
RTGS_TIMEOUT_SECONDS = env.int('RTGS_TIMEOUT_SECONDS', default=30)

# URLe RTGS mocków (w docker-compose: rtgs-mock:9000)
SORBNET3_URL = env('SORBNET3_URL', default='http://rtgs-mock:9000/sorbnet3')
TARGET2_URL = env('TARGET2_URL', default='http://rtgs-mock:9000/target2')
CHAPS_URL = env('CHAPS_URL', default='http://rtgs-mock:9000/chaps')
FEDNOW_URL = env('FEDNOW_URL', default='http://rtgs-mock:9000/fednow')

from core.celery_beat_schedule import build_beat_schedule  # noqa: E402

CELERY_BEAT_SCHEDULE = build_beat_schedule(
    interval_pl=SESSION_INTERVAL_MINUTES_PL,
    interval_eu=SESSION_INTERVAL_MINUTES_EU,
    interval_uk=SESSION_INTERVAL_MINUTES_UK,
    interval_us=SESSION_INTERVAL_MINUTES_US,
)

KLIK_SESSION_INTERVALS = {
    'PL': env.int('SESSION_INTERVAL_MINUTES_PL', default=1440),
    'EU': env.int('SESSION_INTERVAL_MINUTES_EU', default=1440),
    'UK': env.int('SESSION_INTERVAL_MINUTES_UK', default=1440),
    'US': env.int('SESSION_INTERVAL_MINUTES_US', default=1440),
}

KLIK_RTGS_TIMEOUT_SECONDS = env.int('RTGS_TIMEOUT_SECONDS', default=30)

KLIK_RTGS_URLS = {
    'PL': env('SORBNET3_URL'),
    'EU': env('TARGET2_URL'),
    'UK': env('CHAPS_URL'),
    'US': env('FEDNOW_URL'),
}

# ============================================================
# Logging
# ============================================================
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'verbose': {
            'format': '{levelname} {asctime} {module} {process:d} {thread:d} {message}',
            'style': '{',
        },
        'simple': {
            'format': '{levelname} {message}',
            'style': '{',
        },
    },
    'filters': {
        'skip_healthz': {
            '()': 'django.utils.log.CallbackFilter',
            'callback': lambda record: '/healthz/' not in record.getMessage(),
        },
    },
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
            'formatter': 'simple',
            'filters': ['skip_healthz'],
        },
    },
    'root': {
        'handlers': ['console'],
        'level': 'INFO',
    },
    'loggers': {
        'django': {
            'handlers': ['console'],
            'level': 'INFO',
            'propagate': False,
        },
        'klik': {
            'handlers': ['console'],
            'level': 'DEBUG',
            'propagate': False,
        },
    },
}


