import sys
from pathlib import Path

from decouple import config, Csv

# Идёт ли прогон тестов. Нужен, чтобы тестовые данные не уезжали наружу:
# 2026-08-29 тесты регистрации отправили десяток «новых регистраций»
# (ivan@example.com, ООО Тест) в рабочий Telegram-канал владельца — задача
# ушла в общий с продом Redis, и её выполнил боевой воркер.
TESTING = 'test' in sys.argv

BASE_DIR = Path(__file__).resolve().parent.parent.parent

SECRET_KEY = config('SECRET_KEY')
DEBUG = config('DEBUG', default=False, cast=bool)
ALLOWED_HOSTS = config('ALLOWED_HOSTS', cast=Csv())

DJANGO_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'django.contrib.sitemaps',
]

THIRD_PARTY_APPS = [
    'django_htmx',
    'django_filters',
    'django_celery_beat',
]

LOCAL_APPS = [
    'apps.catalog',
    'apps.stock',
    'apps.accounts',
    'apps.orders',
    'apps.export',
    'apps.sync',
    'apps.leads',
    'apps.notifications',
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
    'django_htmx.middleware.HtmxMiddleware',
]

ROOT_URLCONF = 'splithome.urls'

TEMPLATES = [{
    'BACKEND': 'django.template.backends.django.DjangoTemplates',
    'DIRS': [BASE_DIR / 'templates'],
    'APP_DIRS': True,
    'OPTIONS': {
        'context_processors': [
            'django.template.context_processors.debug',
            'django.template.context_processors.request',
            'django.contrib.auth.context_processors.auth',
            'django.contrib.messages.context_processors.messages',
            'apps.orders.context_processors.cart_count',
            'apps.catalog.context_processors.yandex_metrika',
            'apps.catalog.context_processors.seo_verification',
            'apps.catalog.context_processors.static_version',
        ],
    },
}]

WSGI_APPLICATION = 'splithome.wsgi.application'

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': config('DB_NAME'),
        'USER': config('DB_USER'),
        'PASSWORD': config('DB_PASSWORD'),
        'HOST': config('DB_HOST', default='localhost'),
        'PORT': config('DB_PORT', default='5432'),
    }
}

AUTH_USER_MODEL = 'accounts.CustomUser'
LOGIN_URL = '/auth/login/'
LOGIN_REDIRECT_URL = '/account/'
LOGOUT_REDIRECT_URL = '/'

CACHES = {
    'default': {
        'BACKEND': 'django_redis.cache.RedisCache',
        'LOCATION': config('REDIS_URL'),
        'OPTIONS': {'CLIENT_CLASS': 'django_redis.client.DefaultClient'},
    }
}

CELERY_BROKER_URL = config('REDIS_URL')
CELERY_RESULT_BACKEND = config('REDIS_URL')
CELERY_ACCEPT_CONTENT = ['json']
CELERY_TASK_SERIALIZER = 'json'
CELERY_TIMEZONE = 'Europe/Moscow'

from celery.schedules import crontab
CELERY_BEAT_SCHEDULE = {
    'sync-stock-hourly': {
        'task': 'sync.sync_stock',
        'schedule': crontab(minute=5),
    },
    'sync-catalog-4h': {
        'task': 'sync.sync_catalog',
        'schedule': crontab(minute=0, hour='*/4'),
    },
    'sync-daichi-hourly': {
        'task': 'sync.sync_daichi',
        'schedule': crontab(minute=25),
    },
    # Rusklimat: JWT сбрасывается строго в 00:00 МСК (не +24ч). Обновляем
    # за 10 минут до сброса. CELERY_TIMEZONE=Europe/Moscow — crontab уже МСК.
    'refresh-rusklimat-jwt': {
        'task': 'sync.refresh_rusklimat_jwt',
        'schedule': crontab(hour=23, minute=50),
    },
    # Rusklimat REST sync пока ручной (python manage.py sync_rusklimat_rest).
}

STATIC_URL = '/static/'
STATICFILES_DIRS = [BASE_DIR / 'static']
STATIC_ROOT = BASE_DIR / 'staticfiles'
MEDIA_URL = '/media/'
MEDIA_ROOT = BASE_DIR / 'media'

LANGUAGE_CODE = 'ru-ru'
TIME_ZONE = 'Europe/Moscow'
USE_I18N = True
USE_TZ = True
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

BREEZ_AUTH_HEADER = config('BREEZ_AUTH_HEADER')
BREEZ_BASE_URL = config('BREEZ_BASE_URL', default='https://api.breez.ru/v1/')

RUSKLIMAT_JWT_TOKEN = config('RUSKLIMAT_JWT_TOKEN', default='')
RUSKLIMAT_CONTRACTOR_GUID = config('RUSKLIMAT_CONTRACTOR_GUID', default='')
# Auto-refresh JWT (см. apps/sync/rusklimat_auth.py). Если LOGIN/PASSWORD заданы —
# используем их для получения свежего токена через POST b2b.rusklimat.com/api/v1/auth/jwt/.
# Если не заданы — fallback на статичный RUSKLIMAT_JWT_TOKEN.
RUSKLIMAT_LOGIN = config('RUSKLIMAT_LOGIN', default='')
RUSKLIMAT_PASSWORD = config('RUSKLIMAT_PASSWORD', default='')
RUSKLIMAT_PARTNER_ID = config(
    'RUSKLIMAT_PARTNER_ID',
    default='e51a9046-47ff-4d7e-977d-7dba40c0a979',
)

DAICHI_ACCESS_TOKEN = config('DAICHI_ACCESS_TOKEN', default='')
DAICHI_BASE_URL = config('DAICHI_BASE_URL', default='https://api.daichi.ru/b2b/v1/')
DAICHI_STORE_ID = config('DAICHI_STORE_ID', default='default')

EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'
EMAIL_HOST = config('EMAIL_HOST', default='')
EMAIL_PORT = config('EMAIL_PORT', default=587, cast=int)
EMAIL_HOST_USER = config('EMAIL_HOST_USER', default='')
EMAIL_HOST_PASSWORD = config('EMAIL_HOST_PASSWORD', default='')
EMAIL_USE_TLS = config('EMAIL_USE_TLS', default=True, cast=bool)
EMAIL_TIMEOUT = config('EMAIL_TIMEOUT', default=5, cast=int)
DEFAULT_FROM_EMAIL = config('DEFAULT_FROM_EMAIL', default='noreply@splithome.ru')
MANAGER_EMAIL = config('MANAGER_EMAIL', default='')
TELEGRAM_BOT_TOKEN = config('TELEGRAM_BOT_TOKEN', default='')
TELEGRAM_CHAT_ID = config('TELEGRAM_CHAT_ID', default='')
TELEGRAM_API_URL = config('TELEGRAM_API_URL', default='https://api.telegram.org')
YANDEX_METRIKA_ID = config('YANDEX_METRIKA_ID', default='')

# Верификация прав в панелях вебмастеров (мета-тег в <head>). Токен выдаётся
# при добавлении сайта; пустое значение = мета-тег не выводится.
GOOGLE_SITE_VERIFICATION = config('GOOGLE_SITE_VERIFICATION', default='')
YANDEX_VERIFICATION = config('YANDEX_VERIFICATION', default='')
BING_SITE_VERIFICATION = config('BING_SITE_VERIFICATION', default='')

# Скидка физлицу при регистрации (%). Реализует обещание «Скидка до 15% при
# регистрации» из шаблонов home.html / product_card.html / product_detail.html.
# Применяется в IndividualRegistrationForm.save() и в data migration
# accounts/0003_set_individual_discount для уже зарегистрированных физлиц.
DISCOUNT_PERCENT_INDIVIDUAL = config('DISCOUNT_PERCENT_INDIVIDUAL', default=15, cast=int)
