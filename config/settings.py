"""
Django settings for the Uptora project.

Configuration that differs between environments is read from environment
variables, which are loaded from a local `.env` file during development.
"""

import json
import os
from datetime import timedelta
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent

load_dotenv(BASE_DIR / '.env')


def env_bool(name, default=False):
    return os.getenv(name, str(default)).strip().lower() in {'1', 'true', 'yes', 'on'}


def env_list(name, default=''):
    return [value.strip() for value in os.getenv(name, default).split(',') if value.strip()]


UPTORA_ENVIRONMENT = os.getenv('UPTORA_ENVIRONMENT', 'development').strip().lower()
IS_PRODUCTION = UPTORA_ENVIRONMENT == 'production'


# SECURITY WARNING: keep the secret key used in production secret!
DEVELOPMENT_SECRET_KEY = 'django-insecure-local-development-only-do-not-use-in-production'
SECRET_KEY = os.getenv('DJANGO_SECRET_KEY', DEVELOPMENT_SECRET_KEY)

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = env_bool('DJANGO_DEBUG', not IS_PRODUCTION)

ALLOWED_HOSTS = env_list('DJANGO_ALLOWED_HOSTS', 'localhost,127.0.0.1')
CSRF_TRUSTED_ORIGINS = env_list('DJANGO_CSRF_TRUSTED_ORIGINS')
APP_BASE_URL = os.getenv('APP_BASE_URL', 'http://localhost:3000').rstrip('/')
REGISTRATION_ENABLED = env_bool('UPTORA_REGISTRATION_ENABLED', not IS_PRODUCTION)
READINESS_TOKEN = os.getenv('UPTORA_READINESS_TOKEN', '')

TRUST_PROXY_HEADERS = env_bool('DJANGO_TRUST_PROXY_HEADERS', False)
if TRUST_PROXY_HEADERS:
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
    USE_X_FORWARDED_HOST = True

SECURE_SSL_REDIRECT = env_bool('DJANGO_SECURE_SSL_REDIRECT', IS_PRODUCTION)
SESSION_COOKIE_SECURE = env_bool('DJANGO_SESSION_COOKIE_SECURE', IS_PRODUCTION)
CSRF_COOKIE_SECURE = env_bool('DJANGO_CSRF_COOKIE_SECURE', IS_PRODUCTION)
SECURE_HSTS_SECONDS = int(os.getenv('DJANGO_SECURE_HSTS_SECONDS', '0'))
SECURE_HSTS_INCLUDE_SUBDOMAINS = env_bool('DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS', False)
SECURE_HSTS_PRELOAD = env_bool('DJANGO_SECURE_HSTS_PRELOAD', False)
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = 'same-origin'
X_FRAME_OPTIONS = 'DENY'


# Application definition

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'rest_framework',
    'rest_framework_simplejwt.token_blacklist',
    'accounts',
    'websites',
    'monitors',
    'incidents',
    'notifications',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'config.urls'

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

WSGI_APPLICATION = 'config.wsgi.application'


# Database
# https://docs.djangoproject.com/en/6.1/ref/settings/#databases

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': os.getenv('DB_NAME', 'uptora'),
        'USER': os.getenv('DB_USER', 'uptora_user'),
        'PASSWORD': os.getenv('DB_PASSWORD', ''),
        'HOST': os.getenv('DB_HOST', 'localhost'),
        'PORT': os.getenv('DB_PORT', '5433'),
    }
}


# Cache
# https://docs.djangoproject.com/en/6.1/topics/cache/
#
# DRF stores throttle counters here. The local-memory backend is per-process, so
# with several workers the effective limit is multiplied by the worker count.
# That is acceptable for now; pointing this at Redis later makes the counters
# shared without any change to the throttle classes or rates.

CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
        'LOCATION': 'uptora-default',
    }
}


# Authentication
# https://docs.djangoproject.com/en/6.1/topics/auth/customizing/

AUTH_USER_MODEL = 'accounts.User'

AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]


# Django REST Framework
# https://www.django-rest-framework.org/api-guide/settings/

REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': [
        'rest_framework_simplejwt.authentication.JWTAuthentication',
    ],
    'DEFAULT_PERMISSION_CLASSES': [
        'rest_framework.permissions.IsAuthenticated',
    ],
    # General ceiling for ordinary API traffic. Views that need a tighter limit
    # opt in to ScopedRateThrottle with a scope from DEFAULT_THROTTLE_RATES.
    'DEFAULT_THROTTLE_CLASSES': [
        'rest_framework.throttling.UserRateThrottle',
    ],
    'DEFAULT_THROTTLE_RATES': {
        'user': '120/minute',
        'register': '5/minute',
        'login': '5/minute',
        'token_refresh': '20/minute',
        # Manually running a monitor triggers an outbound request.
        'monitor_run': '10/minute',
    },
}


# Simple JWT
# https://django-rest-framework-simplejwt.readthedocs.io/en/latest/settings.html

SIMPLE_JWT = {
    'ACCESS_TOKEN_LIFETIME': timedelta(minutes=15),
    'REFRESH_TOKEN_LIFETIME': timedelta(days=7),
    # Each refresh issues a new refresh token and blacklists the one used, so a
    # stolen refresh token is only usable until the legitimate client refreshes.
    'ROTATE_REFRESH_TOKENS': True,
    'BLACKLIST_AFTER_ROTATION': True,
    'UPDATE_LAST_LOGIN': True,
}


# Internationalization
# https://docs.djangoproject.com/en/6.1/topics/i18n/

LANGUAGE_CODE = 'en-us'

TIME_ZONE = 'UTC'

USE_I18N = True

USE_TZ = True


# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/6.1/howto/static-files/

STATIC_URL = 'static/'


# Notifications
# https://resend.com/docs
#
# The provider is chosen from configuration, never hard-wired into business
# logic. Without an API key, development uses the console provider, which logs
# and returns a message id marked `console:` -- deliberately not a silent
# no-op that would look like production delivery in the delivery record.

RESEND_API_KEY = os.getenv('RESEND_API_KEY', '')
DEFAULT_FROM_EMAIL = os.getenv('DEFAULT_FROM_EMAIL', 'Uptora <alerts@uptora.example>')
NOTIFICATIONS_EMAIL_PROVIDER = os.getenv('NOTIFICATIONS_EMAIL_PROVIDER', '') or (
    'notifications.email.factories.resend_provider'
    if RESEND_API_KEY
    else 'notifications.email.factories.console_provider'
)


# Celery
# https://docs.celeryq.dev/en/stable/django/first-steps-with-django.html
#
# Redis is the broker. Results are not stored: nothing in the product reads a
# task return value, and MonitorRun already records what happened in a form
# that outlives any broker.
#
# Beat carries two global dispatchers. Per-monitor timing lives in the monitors
# table, so a monitor being created, disabled or retimed never has to be
# reflected into broker state, and deliveries likewise remain database-driven.

CELERY_BROKER_URL = os.getenv('CELERY_BROKER_URL', 'redis://localhost:6379/0')
CELERY_RESULT_BACKEND = None
CELERY_TASK_IGNORE_RESULT = True

CELERY_TASK_DEFAULT_QUEUE = 'http'
# Browser and flow work is routed to its own queue by the dispatcher, so an
# operator can run that worker at low concurrency and cap how many Chromium
# processes exist at once.
CELERY_TASK_QUEUES_DOCUMENTED = ('http', 'browser', 'notifications')

CELERY_TASK_ACKS_LATE = True
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True
CELERY_TIMEZONE = 'UTC'

DISPATCH_INTERVAL_SECONDS = int(os.getenv('UPTORA_DISPATCH_INTERVAL_SECONDS', '30'))

NOTIFICATION_DISPATCH_INTERVAL_SECONDS = int(
    os.getenv('UPTORA_NOTIFICATION_DISPATCH_INTERVAL_SECONDS', '60')
)

CELERY_BEAT_SCHEDULE = {
    'dispatch-due-monitors': {
        'task': 'monitors.dispatch_due_monitors',
        'schedule': DISPATCH_INTERVAL_SECONDS,
    },
    # Safety net: normally a delivery is enqueued the moment its event commits,
    # but a broker that was down at that instant would otherwise lose the email.
    'dispatch-pending-notifications': {
        'task': 'notifications.dispatch_pending_notifications',
        'schedule': NOTIFICATION_DISPATCH_INTERVAL_SECONDS,
    },
}


# Media files
# https://docs.djangoproject.com/en/6.1/topics/files/
#
# Failure screenshots are written through Django's storage API, so switching to
# an S3-compatible backend later is a STORAGES change and nothing more. The
# local filesystem is fine for development; serving these in production needs an
# access-controlled view, since the media root is not behind authentication.

MEDIA_URL = 'media/'

EVIDENCE_STORAGE_BACKEND = os.getenv(
    'EVIDENCE_STORAGE_BACKEND',
    'django.core.files.storage.FileSystemStorage',
)
try:
    EVIDENCE_STORAGE_OPTIONS = json.loads(os.getenv('EVIDENCE_STORAGE_OPTIONS_JSON', '{}'))
except json.JSONDecodeError as exc:
    raise ImproperlyConfigured('EVIDENCE_STORAGE_OPTIONS_JSON must be valid JSON.') from exc
if not isinstance(EVIDENCE_STORAGE_OPTIONS, dict):
    raise ImproperlyConfigured('EVIDENCE_STORAGE_OPTIONS_JSON must contain a JSON object.')

MEDIA_ROOT = Path(os.getenv('EVIDENCE_MEDIA_ROOT', str(BASE_DIR / 'media')))
STORAGES = {
    'default': {
        'BACKEND': EVIDENCE_STORAGE_BACKEND,
        'OPTIONS': EVIDENCE_STORAGE_OPTIONS,
    },
    'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
}

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'


# Logging
LOG_LEVEL = os.getenv('UPTORA_LOG_LEVEL', 'INFO').upper()
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'filters': {
        'redact_capabilities': {'()': 'config.logging.RedactCapabilitiesFilter'},
    },
    'formatters': {
        'operational': {
            'format': '%(asctime)s %(levelname)s %(name)s %(message)s',
        },
    },
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
            'filters': ['redact_capabilities'],
            'formatter': 'operational',
        },
    },
    'root': {'handlers': ['console'], 'level': LOG_LEVEL},
}


def require_production(condition, message):
    if IS_PRODUCTION and not condition:
        raise ImproperlyConfigured(message)


require_production(not DEBUG, 'DJANGO_DEBUG must be false in production.')
require_production(
    bool(SECRET_KEY) and SECRET_KEY != DEVELOPMENT_SECRET_KEY,
    'DJANGO_SECRET_KEY must be set to a production secret.',
)
require_production(bool(os.getenv('DJANGO_ALLOWED_HOSTS')), 'DJANGO_ALLOWED_HOSTS is required.')
require_production(bool(CSRF_TRUSTED_ORIGINS), 'DJANGO_CSRF_TRUSTED_ORIGINS is required.')
require_production(APP_BASE_URL.startswith('https://'), 'APP_BASE_URL must use HTTPS.')
require_production(bool(READINESS_TOKEN), 'UPTORA_READINESS_TOKEN is required.')
require_production(bool(os.getenv('DB_PASSWORD')), 'DB_PASSWORD is required.')
require_production(
    bool(os.getenv('CELERY_BROKER_URL')),
    'CELERY_BROKER_URL must be explicitly configured in production.',
)
require_production(
    bool(RESEND_API_KEY)
    or bool(os.getenv('NOTIFICATIONS_EMAIL_PROVIDER'))
    or env_bool('UPTORA_ALLOW_CONSOLE_EMAIL', False),
    'Configure production email or explicitly allow the console provider.',
)
require_production(
    EVIDENCE_STORAGE_BACKEND != 'django.core.files.storage.FileSystemStorage'
    or env_bool('UPTORA_ALLOW_LOCAL_EVIDENCE_STORAGE', False),
    'Configure durable evidence storage or explicitly acknowledge persistent local storage.',
)
