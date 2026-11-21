import json
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
TESTING = os.environ.get('SMA_TEST_SQLITE') == '1'
if TESTING:
    private = {'secret_key': 'test-only-never-deploy-' * 4, 'internal_token': 'test-internal-token', 'db_password': ''}
else:
    private = json.loads(Path(os.environ.get('SMA_SECRET_FILE', '/run/secrets/app.json')).read_text())
SECRET_KEY = private['secret_key']
INTERNAL_TOKEN = private['internal_token']
DEBUG = False
ALLOWED_HOSTS = os.environ.get('SMA_ALLOWED_HOSTS', '127.0.0.1,localhost,media,backend').split(',')
if TESTING:
    ALLOWED_HOSTS += ['testserver']
SMA_DOMAIN_MANAGEMENT = os.environ.get('SMA_DOMAIN_MANAGEMENT') == '1'
SMA_PUBLIC_IP = os.environ.get('SMA_PUBLIC_IP', '')
SMA_PUBLIC_IPV6 = os.environ.get('SMA_PUBLIC_IPV6', '')
SMA_STATIC_ALLOWED_HOSTS = frozenset(h.strip().lower().rstrip('.') for h in ALLOWED_HOSTS if h.strip())
if SMA_DOMAIN_MANAGEMENT:
    # The first middleware enforces static names and active, verified custom domains.
    ALLOWED_HOSTS = ['*']
INSTALLED_APPS = ['django.contrib.auth', 'django.contrib.contenttypes', 'django.contrib.sessions', 'django.contrib.messages', 'portal']
MIDDLEWARE = [
    'portal.domains.ApprovedHostMiddleware',
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'portal.middleware.AccessContextMiddleware',
]
ROOT_URLCONF = 'sma.urls'
TEMPLATES = [{'BACKEND': 'django.template.backends.django.DjangoTemplates',
              'DIRS': [BASE_DIR / 'templates'], 'APP_DIRS': True,
              'OPTIONS': {'context_processors': [
                  'django.template.context_processors.request',
                  'django.contrib.auth.context_processors.auth',
                  'django.contrib.messages.context_processors.messages',
                  'portal.context.portal_context',
              ]}}]
WSGI_APPLICATION = 'sma.wsgi.application'
DATABASES = {'default': {'ENGINE': 'django.db.backends.postgresql',
    'NAME': 'sma', 'USER': 'sma_app', 'PASSWORD': private['db_password'],
    'HOST': 'db', 'PORT': '5432', 'CONN_MAX_AGE': 60, 'CONN_HEALTH_CHECKS': True,
    'OPTIONS': {'connect_timeout': 5}}}
if TESTING:
    DATABASES = {'default': {'ENGINE': 'django.db.backends.sqlite3', 'NAME': ':memory:'}}
AUTH_USER_MODEL = 'portal.User'
AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator', 'OPTIONS': {'min_length': 12}},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]
LANGUAGE_CODE = 'pt-br'
TIME_ZONE = 'America/Manaus'
USE_I18N = True
USE_TZ = True
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
LOGIN_URL = '/login/'
LOGIN_REDIRECT_URL = '/'
LOGOUT_REDIRECT_URL = '/login/'
SESSION_COOKIE_NAME = 'sma_session'
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = 'Lax'
SESSION_COOKIE_AGE = 8 * 60 * 60
CSRF_COOKIE_NAME = 'sma_csrf'
CSRF_COOKIE_SAMESITE = 'Lax'
SESSION_COOKIE_SECURE = os.environ.get('SMA_SECURE_COOKIES', '1') == '1'
CSRF_COOKIE_SECURE = SESSION_COOKIE_SECURE
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = 'same-origin'
CSRF_FAILURE_VIEW = 'portal.views.csrf_failure'
DATA_UPLOAD_MAX_MEMORY_SIZE = 65536
# Audience forms submit one checkbox per selected account; retain the 64 KiB body cap.
DATA_UPLOAD_MAX_NUMBER_FIELDS = 1000
HLS_DIR = Path(os.environ.get('HLS_DIR', '/var/www/hls'))
HLS_FRESH_SECONDS = 15
RTMP_PUBLIC_URL = os.environ.get('RTMP_PUBLIC_URL', 'rtmp://127.0.0.1:19350/live')
SMA_ENVIRONMENT_LABEL = os.environ.get('SMA_ENVIRONMENT_LABEL', 'Ambiente de testes')
SMA_TENANT_HOSTS = json.loads(os.environ.get('SMA_TENANT_HOSTS', '{}'))
SMA_RTMP_URLS = json.loads(os.environ.get('SMA_RTMP_URLS', '{}'))
SMA_OWNER_HOST = os.environ.get('SMA_OWNER_HOST', '').strip().lower().rstrip('.')
SMA_SINGLE_HOST = os.environ.get('SMA_SINGLE_HOST') == '1'
SMA_OWNER_ALIASES = {
    alias.strip().lower().rstrip('.') for alias in os.environ.get('SMA_OWNER_ALIASES', '').split(',')
    if alias.strip()
}
if SMA_SINGLE_HOST and not SMA_OWNER_HOST:
    from django.core.exceptions import ImproperlyConfigured
    raise ImproperlyConfigured('Configure SMA_OWNER_HOST para o acesso em um único endereço.')
SMA_REALTIME_ENABLED = os.environ.get('SMA_REALTIME_ENABLED') == '1'
SMA_REALTIME_RELAY = os.environ.get('SMA_REALTIME_RELAY', 'rtmp://realtime:1935/live')
SMA_CANONICAL_RTMP = 'rtmp://127.0.0.1:1936/live'
CACHES = {
    'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'},
    'rtmp_auth': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
                  'LOCATION': 'sma-rtmp-auth', 'TIMEOUT': 60, 'OPTIONS': {'MAX_ENTRIES': 4096}},
}

SMA_RECORDINGS_DIR = Path(os.environ.get('SMA_RECORDINGS_DIR', '/var/lib/sma-recordings'))
