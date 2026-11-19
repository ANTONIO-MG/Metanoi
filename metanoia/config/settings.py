"""Django settings for the Metanoia Group backend.

Configuration is environment-driven: values are read from ``metanoia/.env``
(see ``.env.example``) with development-friendly defaults so the project runs
out of the box. Every setting and environment variable — its purpose, default,
allowed values, warnings and production notes — is documented in
``docs/reference/configuration.md``; read that before changing a value here.

How the environment is read
---------------------------
``PROCESS_ENV`` snapshots what the *host* set before ``load_dotenv`` layers the
committed ``.env`` underneath it (``load_dotenv`` never overwrites an existing
variable). :func:`platform_decides` uses the snapshot so that, on Render or
Railway (``ON_PLATFORM``), the handful of settings whose safe production value is
knowable — ``DEBUG``, ``SESSION_COOKIE_SECURE``, ``SITE_URL`` — follow the
platform rather than the developer values the committed ``.env`` carries. On the
AWS host, secrets come from Secrets Manager via ``/run/metanoia/secrets.env``,
which systemd loads before this module runs. The helpers :func:`env_bool`,
:func:`env_int`, :func:`env_list` and :func:`env_or` parse variables.

Layout of this module, in order
-------------------------------
1. Environment loading and hosting-platform detection (``ON_RENDER``,
   ``ON_RAILWAY``, ``RENDER_EXTERNAL_HOSTNAME``/``_NETLOC``).
2. Security core: ``DEBUG``, ``TESTING``, ``SECRET_KEY`` (refuses to start with
   the published development key when ``DEBUG`` is off), ``ALLOWED_HOSTS``.
3. ``INSTALLED_APPS`` and ``MIDDLEWARE``, then URL/template configuration.
4. ASGI / WebSockets (Channels layer), Web Push (VAPID) and CORS.
5. LAN access and ``CSRF_TRUSTED_ORIGINS``, then the session cookie.
6. Databases: ``DATABASE_PUBLIC_URL`` / ``DATABASE_URL`` or ``POSTGRES_*``, TLS,
   timeouts, connection lifetime and the fail-fast guards.
7. Django REST Framework and SimpleJWT.
8. HTTPS hardening (TLS redirect, HSTS, proxy header, cookie flags).
9. Authentication backends, django-axes lockout, password validation.
10. Internationalisation and time zone.
11. Static and media files, upload limits, the two-tier file server and
    ``STORAGES``.
12. django-allauth (e-mail-only accounts), Google and Apple sign-in, MFA.
13. Google Places reviews and the sign-up math CAPTCHA.
14. Cache (Redis or local memory) and e-mail (default connection, sending
    mailboxes, Metanoia Mail, developer accounts, ``SITE_URL``).
15. Payments (PayFast, ``PAYMENTS_ENABLED``, VAT, courier delivery), enrolment.
16. Microsoft Teams / Graph and OneDrive backups, live sessions and YouTube,
    WhatsApp, the AI assistants.
17. Guardian, message tags, ``LOGGING``, optional Sentry, warning filters.

Ordering rules a maintainer must keep
-------------------------------------
* ``daphne`` is the first entry in ``INSTALLED_APPS`` so its ASGI-aware
  ``runserver`` replaces Django's WSGI one.
* In ``MIDDLEWARE``, ``ErrorCaptureMiddleware`` is first (its
  ``process_exception`` runs last and sees everything), WhiteNoise sits directly
  after ``SecurityMiddleware``, the access gates follow ``StudentViewMiddleware``,
  and ``AxesMiddleware`` is last.
* ``AxesStandaloneBackend`` is the first authentication backend, so a locked-out
  account or IP is refused before any password is checked.
* ``CSRF_TRUSTED_ORIGINS`` is assembled once, in the LAN block (environment +
  LAN addresses + the platform's public origin). Never reassign it later from the
  environment alone: that silently discards the other two and every hosted login
  POST fails the Origin check.

Docs: https://docs.djangoproject.com/en/6.0/ref/settings/
"""

import os
import sys
import warnings
from datetime import timedelta
from pathlib import Path

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent.parent

ENV_FILE = BASE_DIR / '.env'
ENV_FILE_FOUND = ENV_FILE.is_file()

PROCESS_ENV = dict(os.environ)

load_dotenv(ENV_FILE)

ON_RENDER = bool(PROCESS_ENV.get('RENDER'))
ON_RAILWAY = bool(
    PROCESS_ENV.get('RAILWAY_ENVIRONMENT')
    or PROCESS_ENV.get('RAILWAY_ENVIRONMENT_NAME')
    or PROCESS_ENV.get('RAILWAY_PROJECT_ID')
    or PROCESS_ENV.get('RAILWAY_SERVICE_ID')
)
ON_PLATFORM = ON_RENDER or ON_RAILWAY
RENDER_EXTERNAL_HOSTNAME = (
    (PROCESS_ENV.get('RENDER_EXTERNAL_HOSTNAME') or '').strip().split('://')[-1].split('/')[0]
)
_RENDER_HOST_PORT = ''
if ':' in RENDER_EXTERNAL_HOSTNAME:
    RENDER_EXTERNAL_HOSTNAME, _, _RENDER_HOST_PORT = RENDER_EXTERNAL_HOSTNAME.partition(':')
RENDER_EXTERNAL_NETLOC = (
    f'{RENDER_EXTERNAL_HOSTNAME}:{_RENDER_HOST_PORT}' if _RENDER_HOST_PORT
    else RENDER_EXTERNAL_HOSTNAME
)

if not ENV_FILE_FOUND:
    warnings.warn(
        f"No .env file found at {ENV_FILE}; using process environment only. "
        f"For a local checkout run `cp .env.example .env` and fill it in, or "
        f"restore a previous one from git.",
        stacklevel=2,
    )


def env_bool(name, default=False):
    """Read environment variable *name* as a boolean (``1``, ``true``, ``yes``, ``on``; case-insensitive)."""
    return os.getenv(name, str(default)).strip().lower() in ('1', 'true', 'yes', 'on')


def env_int(name, default=0):
    """Read environment variable *name* as an integer; *default* when unset, blank or not a number."""
    try:
        return int(os.getenv(name, '').strip() or default)
    except ValueError:
        return default


def env_list(name, default=()):
    """Comma-separated env var → list of non-empty stripped strings."""
    raw = os.getenv(name)
    if raw is None:
        return list(default)
    return [item.strip() for item in raw.split(',') if item.strip()]


def platform_decides(name):
    """True when *name*'s value must not be taken from the committed ``.env``.

    The .env in this repository is tracked (see the note in .gitignore), so it
    is deployed along with the code, carrying a developer's values for things
    that are dangerous to get wrong in production — an insecure cookie flag, a
    localhost site URL. ``load_dotenv`` does not overwrite variables the host
    already set, so anything configured in the platform's own dashboard still
    wins; what this catches is the *absence* of such a variable, where the .env
    value would otherwise become the production value by default.

    Only used for the handful of settings where the safe production value is
    knowable without being told (``SESSION_COOKIE_SECURE`` and ``SITE_URL``; see
    ``docs/reference/configuration.md``).
    """
    return ON_PLATFORM and name not in PROCESS_ENV


try:  # pragma: no cover
    import MySQLdb  # noqa: F401
except Exception:  # pragma: no cover
    try:
        import pymysql
        pymysql.install_as_MySQLdb()
    except Exception:
        pass


if ON_PLATFORM and 'DEBUG' not in PROCESS_ENV:
    DEBUG = False
else:
    DEBUG = env_bool('DEBUG', True)

TESTING = 'test' in sys.argv or bool(os.getenv('PYTEST_CURRENT_TEST'))

_DEV_SECRET_KEY = 'django-insecure-bq5ce)%1j-iqh@snbr6=3ddq-p(s!_n@ue8hm$lu1-cy(88amz'
SECRET_KEY = os.getenv('SECRET_KEY') or ''
if not SECRET_KEY.strip():
    if DEBUG or TESTING:
        SECRET_KEY = _DEV_SECRET_KEY
    else:
        from django.core.exceptions import ImproperlyConfigured
        raise ImproperlyConfigured(
            'SECRET_KEY is not set and DEBUG is off. Refusing to start with the '
            'development key that is published in config/settings.py. Set '
            'SECRET_KEY in the environment (on the AWS host: the Secrets Manager '
            'secret, loaded via /run/metanoia/secrets.env).')

ALLOWED_HOSTS = env_list('ALLOWED_HOSTS', ['*'] if DEBUG else [])

if RENDER_EXTERNAL_HOSTNAME and RENDER_EXTERNAL_HOSTNAME not in ALLOWED_HOSTS:
    ALLOWED_HOSTS.append(RENDER_EXTERNAL_HOSTNAME)
if ON_PLATFORM and not DEBUG and len(ALLOWED_HOSTS) > 1:
    ALLOWED_HOSTS = [h for h in ALLOWED_HOSTS if h != '*']


INSTALLED_APPS = [

    'daphne',
    'channels',


    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'django.contrib.sites',
     'django.contrib.humanize',

    'apps.accounts',
    'apps.myhub',
    'apps.communication',
    'apps.mailbox',
    'apps.msteams',
    'apps.livesessions',
    'apps.ai_assistant',
    'apps.shop',
    'apps.finance',
    'apps.tasks',

    'apps.learning',
    'apps.assessments',
    'apps.reports',
    'apps.analytics',
    'apps.scheduler',
    'apps.diagnostics',
    'apps.staffdesk',
    'apps.revision',

    'rest_framework',
    'rest_framework.authtoken',
    'rest_framework_simplejwt.token_blacklist',

    'dj_rest_auth',
    'dj_rest_auth.registration',

    'allauth',
    'allauth.account',
    'allauth.mfa',
    'allauth.socialaccount',
    'allauth.socialaccount.providers.google',
    'allauth.socialaccount.providers.apple',
    'allauth.socialaccount.providers.facebook',

    'corsheaders',
    'guardian',
    'django_filters',

    'captcha',

    'axes',

    'import_export',
]


MIDDLEWARE = [
    'apps.diagnostics.middleware.ErrorCaptureMiddleware',
    'apps.diagnostics.monitor.TrafficMiddleware',
    'core.upload_progress.UploadRedirectMiddleware',
    'corsheaders.middleware.CorsMiddleware',
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'core.media_headers.MediaSecurityHeadersMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'core.counters.CounterInvalidationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    'allauth.account.middleware.AccountMiddleware',
    'apps.accounts.middleware.CurrentUserMiddleware',
    'apps.accounts.middleware.UserPreferenceMiddleware',
    'core.i18n_runtime.RuntimeTranslationMiddleware',
    'core.student_view.StudentViewMiddleware',
    'apps.accounts.middleware.LoginRequiredMiddleware',
    'apps.accounts.middleware.OnboardingMiddleware',
    'apps.accounts.middleware.ManagementAccessMiddleware',
    'apps.accounts.middleware.ParentAccessMiddleware',
    'apps.accounts.middleware.CommunityAccessMiddleware',
    'apps.accounts.middleware.ModuleAccessMiddleware',
    'apps.accounts.middleware.MfaRequiredMiddleware',
    'apps.accounts.middleware.SocialAppGuardMiddleware',
    'axes.middleware.AxesMiddleware',
]


ROOT_URLCONF = 'config.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'core.context_processors.branding',
                'core.context_processors.ui_chrome',
                'django.template.context_processors.i18n',
                'core.roles.user_roles',
                'core.student_view.context',
                'apps.myhub.context_processors.get_dashboard_data',
                'apps.accounts.context_processors.account_stats',
                'apps.accounts.context_processors.terms_update',
                'apps.communication.context_processors.comm_stats',
                'apps.mailbox.context_processors.mail_stats',
                'apps.shop.context_processors.shop_stats',
                'apps.finance.context_processors.finance_stats',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'

ASGI_APPLICATION = 'config.asgi.application'

WEBSOCKETS_ENABLED = env_bool('WEBSOCKETS_ENABLED', True)

VAPID_PUBLIC_KEY = os.getenv('VAPID_PUBLIC_KEY', '').strip()
VAPID_PRIVATE_KEY = os.getenv('VAPID_PRIVATE_KEY', '').strip()
VAPID_SUBJECT = os.getenv('VAPID_SUBJECT', 'mailto:info@metanoiagroup.co.za').strip()

if env_bool('USE_REDIS', False):
    CHANNEL_LAYERS = {
        'default': {
            'BACKEND': 'channels_redis.core.RedisChannelLayer',
            'CONFIG': {
                'hosts': [os.getenv('CHANNEL_REDIS_URL',
                                    os.getenv('REDIS_URL', 'redis://127.0.0.1:6379/2'))],
            },
        },
    }
else:
    CHANNEL_LAYERS = {
        'default': {'BACKEND': 'channels.layers.InMemoryChannelLayer'},
    }


CORS_ALLOW_ALL_ORIGINS = env_bool('CORS_ALLOW_ALL_ORIGINS', DEBUG)
CORS_ALLOWED_ORIGINS = env_list('CORS_ALLOWED_ORIGINS')
CORS_ALLOW_CREDENTIALS = env_bool('CORS_ALLOW_CREDENTIALS', False)


def _detect_lan_ips():
    """Return this machine's non-loopback IPv4 addresses, for trusting LAN origins in development.

    The UDP ``connect`` to 8.8.8.8 sends nothing; it only makes the OS pick the
    outward interface so its address can be read.
    """
    import socket
    ips = set()
    try:
        ips.add(socket.gethostbyname(socket.gethostname()))
    except Exception:
        pass
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(('8.8.8.8', 80))
        ips.add(probe.getsockname()[0])
        probe.close()
    except Exception:
        pass
    return {ip for ip in ips if ip and not ip.startswith('127.')}

LAN_IPS = [] if ON_PLATFORM else sorted(_detect_lan_ips())
_LAN_PORTS = [p.strip() for p in os.getenv('LAN_PORTS', '8000,80,8080').split(',') if p.strip()]
CSRF_TRUSTED_ORIGINS = env_list('CSRF_TRUSTED_ORIGINS')
for _ip in LAN_IPS:
    CSRF_TRUSTED_ORIGINS.append(f'http://{_ip}')
    CSRF_TRUSTED_ORIGINS += [f'http://{_ip}:{_port}' for _port in _LAN_PORTS]
if RENDER_EXTERNAL_NETLOC:
    CSRF_TRUSTED_ORIGINS.append(f'https://{RENDER_EXTERNAL_NETLOC}')
CSRF_TRUSTED_ORIGINS = sorted(set(CSRF_TRUSTED_ORIGINS))


SESSION_COOKIE_SAMESITE = os.getenv('SESSION_COOKIE_SAMESITE', 'Lax') or None
SESSION_COOKIE_SECURE = (
    not DEBUG if platform_decides('SESSION_COOKIE_SECURE')
    else env_bool('SESSION_COOKIE_SECURE', not DEBUG)
)

if DEBUG and SESSION_COOKIE_SECURE and not env_bool('DEV_HTTPS', False):
    import warnings
    warnings.warn(
        'SESSION_COOKIE_SECURE=True with DEBUG=True: the dev server serves plain '
        'HTTP, so the browser would discard the session cookie and every login '
        'would loop back to the login page. Forcing it off. (Set DEV_HTTPS=true '
        'if you really are serving the dev site over TLS.)',
        RuntimeWarning,
    )
    SESSION_COOKIE_SECURE = False
    if SESSION_COOKIE_SAMESITE is None:
        SESSION_COOKIE_SAMESITE = 'Lax'


from urllib.parse import parse_qsl, unquote, urlsplit

_POSTGRES_SCHEMES = {
    'postgres', 'postgresql', 'pgsql',
    'postgres+psycopg2', 'postgresql+psycopg2',
    'postgres+psycopg', 'postgresql+psycopg',
}


def _parse_database_url(url, source='DATABASE_URL'):
    """Parse ``postgres://user:pass@host:port/dbname?sslmode=require``.

    Returns a Django ``DATABASES['default']``-shaped dict. Only PostgreSQL
    schemes are accepted: this project's primary database is PostgreSQL, and a
    silently-ignored ``mysql://`` would be far worse than a loud error.
    """
    from django.core.exceptions import ImproperlyConfigured

    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in _POSTGRES_SCHEMES:
        raise ImproperlyConfigured(
            f"{source} has scheme '{parts.scheme or '(none)'}'; expected a "
            f"PostgreSQL URL such as "
            f"postgresql://user:password@host:5432/dbname"
        )
    if not parts.hostname:
        raise ImproperlyConfigured(
            f"{source} has no host: {url!r}. Expected "
            f"postgresql://user:password@host:5432/dbname"
        )

    options = {}
    query = dict(parse_qsl(parts.query, keep_blank_values=False))
    for _key in ('sslmode', 'sslrootcert', 'sslcert', 'sslkey', 'options',
                 'connect_timeout', 'application_name', 'target_session_attrs'):
        if query.get(_key):
            options[_key] = query[_key]
    if 'connect_timeout' in options:
        options['connect_timeout'] = int(options['connect_timeout'])

    return {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': unquote(parts.path.lstrip('/')) or 'postgres',
        'USER': unquote(parts.username or ''),
        'PASSWORD': unquote(parts.password or ''),
        'HOST': parts.hostname,
        'PORT': str(parts.port or ''),
        'OPTIONS': options,
    }


_URL_VARS = (
    ('DATABASE_URL', 'DATABASE_PUBLIC_URL') if ON_PLATFORM
    else ('DATABASE_PUBLIC_URL', 'DATABASE_URL')
)
_URL_CANDIDATES = [
    (name, value) for name in _URL_VARS
    if (value := (os.getenv(name) or '').strip())
]


def _is_private_railway_url(value):
    """True when connection URL *value* points at a ``*.railway.internal`` host, which resolves only inside Railway."""
    try:
        return (urlsplit(value).hostname or '').endswith('.railway.internal')
    except ValueError:
        return False


_usable = [c for c in _URL_CANDIDATES
           if ON_PLATFORM or not _is_private_railway_url(c[1])]
DATABASE_URL_SOURCE = (_usable or _URL_CANDIDATES or [(None, None)])[0][0]

if DATABASE_URL_SOURCE:
    _default_db = _parse_database_url(
        os.getenv(DATABASE_URL_SOURCE).strip(), DATABASE_URL_SOURCE
    )
else:
    _default_db = {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': os.getenv('POSTGRES_DB', 'metanoia_db'),
        'USER': os.getenv('POSTGRES_USER', 'postgres'),
        'PASSWORD': os.getenv('POSTGRES_PASSWORD', ''),
        'HOST': os.getenv('POSTGRES_HOST', '127.0.0.1'),
        'PORT': os.getenv('POSTGRES_PORT', '5432'),
        'OPTIONS': {},
    }

_db_host = _default_db['HOST'] or ''
_db_is_local = _db_host in ('', 'localhost', '127.0.0.1', '::1') or _db_host.startswith('/')

_sslmode = (os.getenv('POSTGRES_SSLMODE') or '').strip()
if _sslmode:
    _default_db['OPTIONS']['sslmode'] = _sslmode
elif not _db_is_local and not _db_host.endswith('.railway.internal'):
    _default_db['OPTIONS'].setdefault('sslmode', 'require')

_default_db['OPTIONS'].setdefault('connect_timeout', env_int('POSTGRES_CONNECT_TIMEOUT', 10))

_default_db['CONN_MAX_AGE'] = env_int('POSTGRES_CONN_MAX_AGE', 0)
_default_db['CONN_HEALTH_CHECKS'] = env_bool('POSTGRES_CONN_HEALTH_CHECKS', True)
_default_db['DISABLE_SERVER_SIDE_CURSORS'] = env_bool('POSTGRES_DISABLE_SERVER_SIDE_CURSORS', True)

DATABASES = {
    'default': _default_db,

}

if _db_host.endswith('.railway.internal') and not ON_RAILWAY:
    warnings.warn(
        f"Database host '{_db_host}' is a Railway *private* address and only "
        f"resolves from inside Railway. Connecting from here fails with "
        f"\"could not translate host name\". Use the public URL instead: "
        f"Railway dashboard → Postgres service → Variables → "
        f"DATABASE_PUBLIC_URL (a *.proxy.rlwy.net host on its own port).",
        RuntimeWarning,
        stacklevel=2,
    )

if ON_PLATFORM and not DATABASE_URL_SOURCE and _db_is_local:
    from django.core.exceptions import ImproperlyConfigured

    raise ImproperlyConfigured(
        f"Running on a hosting platform with the database host set to "
        f"'{_db_host or 'localhost'}' — that is this container's own loopback "
        f"address, where nothing is listening. Attach a managed PostgreSQL "
        f"instance and expose its connection string as DATABASE_URL (on Render: "
        f"the database's Internal Database URL, added to the web service's "
        f"environment)."
    )

if not DATABASES['default'].get('PASSWORD'):
    from django.core.exceptions import ImproperlyConfigured

    if DATABASE_URL_SOURCE:
        _hint = (
            f"{DATABASE_URL_SOURCE} carries no password — copy the full "
            f"connection string, including the ':password@' part."
        )
    elif not ENV_FILE_FOUND:
        _hint = (
            f"No .env file exists at {ENV_FILE}. Restore it "
            "(`git show <commit>:metanoia/.env > .env`) or start from the "
            "template (`cp .env.example .env`) and set DATABASE_URL or "
            "POSTGRES_PASSWORD."
        )
    else:
        _hint = (
            "POSTGRES_PASSWORD is blank in your .env — set it to the database "
            "password, or set DATABASE_URL instead."
        )
    raise ImproperlyConfigured(
        f"PostgreSQL 'default' database has no password. {_hint}"
    )

DATABASE_ROUTERS = ['core.db_router.PrimaryReplicaRouter']


REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': (
        'dj_rest_auth.jwt_auth.JWTCookieAuthentication',
        'rest_framework_simplejwt.authentication.JWTAuthentication',
        'rest_framework.authentication.SessionAuthentication',
    ),
    'DEFAULT_PERMISSION_CLASSES': (
        'rest_framework.permissions.IsAuthenticated',
    ),
    'DEFAULT_FILTER_BACKENDS': [
        'django_filters.rest_framework.DjangoFilterBackend',
        'rest_framework.filters.SearchFilter',
        'rest_framework.filters.OrderingFilter',
    ],
    'DEFAULT_PAGINATION_CLASS': 'core.pagination.StandardResultsSetPagination',
    'PAGE_SIZE': 25,
    'DEFAULT_THROTTLE_CLASSES': (
        'rest_framework.throttling.AnonRateThrottle',
        'rest_framework.throttling.UserRateThrottle',
    ),
    'DEFAULT_THROTTLE_RATES': {
        'anon': os.getenv('API_THROTTLE_ANON') or '60/min',
        'user': os.getenv('API_THROTTLE_USER') or '300/min',
        'auth': os.getenv('API_THROTTLE_AUTH') or '10/min',
    },
}

SIMPLE_JWT = {
    'ACCESS_TOKEN_LIFETIME': timedelta(minutes=60),
    'REFRESH_TOKEN_LIFETIME': timedelta(days=7),
    'ROTATE_REFRESH_TOKENS': True,
    'BLACKLIST_AFTER_ROTATION': True,
    'ALGORITHM': 'HS256',
    'SIGNING_KEY': SECRET_KEY,
    'AUTH_HEADER_TYPES': ('Bearer',),
}

REST_USE_JWT = True
JWT_AUTH_COOKIE = 'access'
JWT_AUTH_REFRESH_COOKIE = 'refresh'

REST_AUTH_REGISTER_SERIALIZERS = {
    'REGISTER_SERIALIZER': 'apps.accounts.serializers.CustomRegisterSerializer',
}


ALLOW_PLAIN_HTTP = env_bool('ALLOW_PLAIN_HTTP', False)
SECURE_SSL_REDIRECT = env_bool('SECURE_SSL_REDIRECT', not DEBUG and not TESTING)
CSRF_COOKIE_SECURE = env_bool('CSRF_COOKIE_SECURE', not DEBUG)
SECURE_HSTS_SECONDS = env_int('SECURE_HSTS_SECONDS', 0 if DEBUG else 31536000)
SECURE_HSTS_INCLUDE_SUBDOMAINS = env_bool('SECURE_HSTS_INCLUDE_SUBDOMAINS', not DEBUG)
SECURE_HSTS_PRELOAD = env_bool('SECURE_HSTS_PRELOAD', not DEBUG)

if env_bool('USE_X_FORWARDED_PROTO', ON_PLATFORM):
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')

SESSION_COOKIE_HTTPONLY = True
CSRF_COOKIE_HTTPONLY = env_bool('CSRF_COOKIE_HTTPONLY', False)
PUBLIC_SITE_URL = os.getenv('PUBLIC_SITE_URL', 'https://metanoiagroup.co.za/')
CSRF_FAILURE_VIEW = 'core.csrf.csrf_failure'
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = os.getenv('SECURE_REFERRER_POLICY', 'same-origin')


AUTHENTICATION_BACKENDS = [
    'axes.backends.AxesStandaloneBackend',
    'django.contrib.auth.backends.ModelBackend',
    'guardian.backends.ObjectPermissionBackend',
    'allauth.account.auth_backends.AuthenticationBackend',
]

AXES_FAILURE_LIMIT = int(os.getenv('AXES_FAILURE_LIMIT') or 5)
AXES_COOLOFF_TIME = timedelta(minutes=int(os.getenv('AXES_COOLOFF_MINUTES') or 30))
AXES_LOCKOUT_PARAMETERS = [['username', 'ip_address']]
AXES_RESET_ON_SUCCESS = True
AXES_SENSITIVE_PARAMETERS = ['password', 'password1', 'password2', 'token']
AXES_ENABLE_ADMIN = True
AXES_IPWARE_PROXY_COUNT = int(os.getenv('AXES_PROXY_COUNT') or 0) or None
AXES_LOCKOUT_TEMPLATE = 'account/lockout.html'


AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]


LANGUAGE_CODE = 'en'
LANGUAGES = [
    ('en', 'English'),
    ('fr', 'Français'),
    ('pt', 'Português'),
]
LOCALE_PATHS = [BASE_DIR / 'locale']
TIME_ZONE = os.getenv('TIME_ZONE') or 'Africa/Johannesburg'
USE_I18N = True
USE_TZ = True

RUNTIME_TRANSLATION = True


STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
STATICFILES_DIRS = [d for d in [BASE_DIR / 'static'] if d.exists()]

MEDIA_URL = '/media/'
MEDIA_ROOT = BASE_DIR / 'media'

SERVE_MEDIA_FROM_DJANGO = env_bool('SERVE_MEDIA_FROM_DJANGO', True)

DATA_UPLOAD_MAX_MEMORY_SIZE = env_int('DATA_UPLOAD_MAX_MEMORY_SIZE', 250 * 1024 * 1024)
FILE_UPLOAD_MAX_MEMORY_SIZE = env_int('FILE_UPLOAD_MAX_MEMORY_SIZE', 5 * 1024 * 1024)
DATA_UPLOAD_MAX_NUMBER_FIELDS = env_int('DATA_UPLOAD_MAX_NUMBER_FIELDS', 2000)


def env_or(name, default):
    """Environment value, falling back to ``default`` when unset **or blank**.

    ``os.getenv(name, default)`` only falls back when the variable is *absent*.
    A declared-but-empty ``FILES_URL=`` in .env therefore resolved to ``''``,
    which silently stripped the ``/media/`` prefix off every file-server URL and
    rooted uploads at the project directory instead of ``media/``. Blank is
    never a meaningful value for these, so it is treated as "not set".
    """
    value = os.getenv(name)
    return value if (value or '').strip() else default


FILE_SERVER_BACKEND = env_or('FILE_SERVER_BACKEND', 'local')
FILES_ROOT = env_or('FILES_ROOT', str(MEDIA_ROOT))
FILES_URL = env_or('FILES_URL', MEDIA_URL)


def _files_storage_backend():
    """Return the ``STORAGES['files']`` entry for ``FILE_SERVER_BACKEND``: S3, Azure, or local disk under ``FILES_ROOT``."""
    if FILE_SERVER_BACKEND == 's3':
        return {
            'BACKEND': 'storages.backends.s3.S3Storage',
            'OPTIONS': {
                'bucket_name': os.getenv('AWS_STORAGE_BUCKET_NAME', ''),
                'region_name': os.getenv('AWS_S3_REGION_NAME', ''),
                'endpoint_url': os.getenv('AWS_S3_ENDPOINT_URL', '') or None,
                'access_key': os.getenv('AWS_ACCESS_KEY_ID', ''),
                'secret_key': os.getenv('AWS_SECRET_ACCESS_KEY', ''),
                'location': os.getenv('FILES_PREFIX', 'files'),
                'default_acl': os.getenv('AWS_DEFAULT_ACL', 'private'),
                'querystring_auth': True,
            },
        }
    if FILE_SERVER_BACKEND == 'azure':
        return {
            'BACKEND': 'storages.backends.azure_storage.AzureStorage',
            'OPTIONS': {
                'account_name': os.getenv('AZURE_ACCOUNT_NAME', ''),
                'account_key': os.getenv('AZURE_ACCOUNT_KEY', ''),
                'azure_container': os.getenv('AZURE_CONTAINER', 'files'),
                'expiration_secs': int(os.getenv('AZURE_URL_EXPIRATION_SECS', '3600')),
            },
        }
    return {
        'BACKEND': 'django.core.files.storage.FileSystemStorage',
        'OPTIONS': {'location': FILES_ROOT, 'base_url': FILES_URL},
    }


_STATICFILES_BACKEND = (
    'whitenoise.storage.CompressedManifestStaticFilesStorage'
    if env_bool('STATIC_MANIFEST', False)
    else 'whitenoise.storage.CompressedStaticFilesStorage'
)

STORAGES = {
    'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
    'staticfiles': {'BACKEND': _STATICFILES_BACKEND},
    'files': _files_storage_backend(),
}

WHITENOISE_AUTOREFRESH = DEBUG
WHITENOISE_USE_FINDERS = DEBUG


DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'


SITE_ID = 1

ACCOUNT_USER_MODEL_USERNAME_FIELD = 'username'
ACCOUNT_SIGNUP_FIELDS = ['email*', 'password1*', 'password2*']
ACCOUNT_UNIQUE_EMAIL = True
ACCOUNT_LOGIN_METHODS = {'email'}

ACCOUNT_ADAPTER = 'apps.accounts.adapters.HubAccountAdapter'
SOCIALACCOUNT_ADAPTER = 'apps.accounts.adapters.HubSocialAccountAdapter'

ACCOUNT_FORMS = {
    'reset_password_from_key': 'apps.accounts.forms.MetanoiaResetPasswordKeyForm',
}

ACCOUNT_EMAIL_VERIFICATION = os.getenv('ACCOUNT_EMAIL_VERIFICATION', 'mandatory')
ACCOUNT_CONFIRM_EMAIL_ON_GET = True
ACCOUNT_LOGIN_ON_EMAIL_CONFIRMATION = False
ACCOUNT_EMAIL_SUBJECT_PREFIX = os.getenv('ACCOUNT_EMAIL_SUBJECT_PREFIX', '')

LOGIN_URL = '/myhub/page-login/'
LOGIN_REDIRECT_URL = '/myhub/'
LOGOUT_REDIRECT_URL = '/myhub/page-login/'

ACCOUNT_EMAIL_CONFIRMATION_ANONYMOUS_REDIRECT_URL = LOGIN_URL
ACCOUNT_EMAIL_CONFIRMATION_AUTHENTICATED_REDIRECT_URL = LOGIN_URL


SOCIALACCOUNT_LOGIN_ON_GET = True
SOCIALACCOUNT_EMAIL_VERIFICATION = 'none'
SOCIALACCOUNT_EMAIL_REQUIRED = True

SOCIALACCOUNT_EMAIL_AUTHENTICATION = True
SOCIALACCOUNT_EMAIL_AUTHENTICATION_AUTO_CONNECT = True


MFA_SUPPORTED_TYPES = ['totp', 'recovery_codes']
MFA_TOTP_ISSUER = os.getenv('MFA_TOTP_ISSUER', '') or 'Metanoia Group'
MFA_RECOVERY_CODE_COUNT = 10
MFA_PASSKEY_LOGIN_ENABLED = False
ACCOUNT_REAUTHENTICATION_REQUIRED = env_bool('ACCOUNT_REAUTHENTICATION_REQUIRED', True)
MFA_REQUIRED_FOR_STAFF = env_bool('MFA_REQUIRED_FOR_STAFF', False)

SOCIALACCOUNT_PROVIDERS = {
    'google': {
        'SCOPE': ['profile', 'email'],
        'AUTH_PARAMS': {'access_type': 'online'},
        'OAUTH_PKCE_ENABLED': True,
        'APP': {
            'client_id': os.getenv('GOOGLE_OAUTH_CLIENT_ID', ''),
            'secret': os.getenv('GOOGLE_OAUTH_SECRET', ''),
            'key': '',
        },
    },
}

_apple_key = ''
if os.getenv('APPLE_PRIVATE_KEY_PATH'):
    try:
        _apple_key = Path(os.getenv('APPLE_PRIVATE_KEY_PATH')).read_text()
    except OSError:
        _apple_key = ''
if os.getenv('APPLE_SERVICES_ID') and os.getenv('APPLE_KEY_ID') and os.getenv('APPLE_TEAM_ID') and _apple_key:
    SOCIALACCOUNT_PROVIDERS['apple'] = {
        'SCOPE': ['name', 'email'],
        'APP': {
            'client_id': os.getenv('APPLE_SERVICES_ID'),
            'secret': os.getenv('APPLE_KEY_ID'),
            'key': os.getenv('APPLE_TEAM_ID'),
            'settings': {'certificate_key': _apple_key},
        },
    }


GOOGLE_PLACES_API_KEY = os.getenv('GOOGLE_PLACES_API_KEY', '')
GOOGLE_PLACE_ID = os.getenv('GOOGLE_PLACE_ID', '')


CAPTCHA_CHALLENGE_FUNCT = 'apps.accounts.captcha_challenges.add_challenge'
CAPTCHA_LETTER_ROTATION = None
CAPTCHA_TIMEOUT = 10


if env_bool('USE_REDIS', False):
    CACHES = {
        'default': {
            'BACKEND': 'django_redis.cache.RedisCache',
            'LOCATION': os.getenv('REDIS_URL', 'redis://127.0.0.1:6379/1'),
            'OPTIONS': {
                'CLIENT_CLASS': 'django_redis.client.DefaultClient',
                'IGNORE_EXCEPTIONS': True,
            },
        }
    }
    DJANGO_REDIS_LOG_IGNORED_EXCEPTIONS = True
    SESSION_ENGINE = 'django.contrib.sessions.backends.cached_db'
else:
    CACHES = {
        'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'},
    }


EMAIL_BACKEND = os.getenv('EMAIL_BACKEND', 'django.core.mail.backends.console.EmailBackend')
EMAIL_HOST = os.getenv('EMAIL_HOST', 'localhost')
EMAIL_PORT = int(os.getenv('EMAIL_PORT', '25'))
EMAIL_HOST_USER = os.getenv('EMAIL_HOST_USER', '')
EMAIL_HOST_PASSWORD = os.getenv('EMAIL_HOST_PASSWORD', '').replace(' ', '')
EMAIL_USE_TLS = env_bool('EMAIL_USE_TLS', False)
EMAIL_USE_SSL = env_bool('EMAIL_USE_SSL', False)
EMAIL_TIMEOUT = int(os.getenv('EMAIL_TIMEOUT', '20'))
DEFAULT_FROM_EMAIL = os.getenv('DEFAULT_FROM_EMAIL', 'no-reply@metanoia.local')
SERVER_EMAIL = os.getenv('SERVER_EMAIL', DEFAULT_FROM_EMAIL)

DEVELOPER_EMAILS = [e.lower() for e in env_list(
    'DEVELOPER_EMAILS', env_list('PLATFORM_DEVELOPER_EMAILS', (
        'dev@metanoiagroup.co.za',
        'seeid24@gmail.com',
        'cjmordaunt@gmail.com',
    )))]

MAILBOX_SECRET_KEY = os.getenv('MAILBOX_SECRET_KEY', '')
MAILBOX_EMAIL_BACKEND = ('django.core.mail.backends.locmem.EmailBackend' if TESTING
                         else 'django.core.mail.backends.smtp.EmailBackend')

EMAIL_FROM_NAME = os.getenv('EMAIL_FROM_NAME', 'Metanoia Group')
EMAIL_MAILBOXES = {
    name: {'user': os.getenv(f'EMAIL_{key}_USER', ''),
           'password': os.getenv(f'EMAIL_{key}_PASSWORD', '')}
    for name, key in (('accounts', 'ACCOUNTS'), ('info', 'INFO'), ('learninghub', 'LEARNINGHUB'))
}

if platform_decides('SITE_URL') and RENDER_EXTERNAL_NETLOC:
    SITE_URL = f'https://{RENDER_EXTERNAL_NETLOC}'
else:
    SITE_URL = os.getenv('SITE_URL', 'http://127.0.0.1:8000')


PAYFAST_SANDBOX = env_bool('PAYFAST_SANDBOX', True)
PAYFAST_MERCHANT_ID = os.getenv('PAYFAST_MERCHANT_ID', '')
PAYFAST_MERCHANT_KEY = os.getenv('PAYFAST_MERCHANT_KEY', '')
PAYFAST_PASSPHRASE = os.getenv('PAYFAST_PASSPHRASE', '')

PAYMENTS_ENABLED = env_bool('PAYMENTS_ENABLED', DEBUG or not ON_PLATFORM)

VAT_REGISTERED = env_bool('VAT_REGISTERED', False)
VAT_RATE = os.getenv('VAT_RATE', '15')
VAT_NUMBER = os.getenv('VAT_NUMBER', '').strip()

COURIER_GUY_API_KEY = os.getenv('COURIER_GUY_API_KEY', '').strip()
COURIER_GUY_BASE_URL = os.getenv('COURIER_GUY_BASE_URL',
                                 'https://api.portal.thecourierguy.co.za/v2/').strip()
SHOP_DELIVERY_DOOR_FEE = os.getenv('SHOP_DELIVERY_DOOR_FEE', '120')
SHOP_DELIVERY_LOCKER_FEE = os.getenv('SHOP_DELIVERY_LOCKER_FEE', '70')
SHOP_FREE_DELIVERY_OVER = os.getenv('SHOP_FREE_DELIVERY_OVER', '1500')
COURIER_TRACKING_URL = os.getenv('COURIER_TRACKING_URL',
                                 'https://www.thecourierguy.co.za/tracking?ref={ref}')
SHOP_COLLECTION = {
    'company': os.getenv('SHOP_COLLECTION_COMPANY', 'Metanoia Group'),
    'contact': os.getenv('SHOP_COLLECTION_CONTACT', 'Metanoia Group'),
    'phone': os.getenv('SHOP_COLLECTION_PHONE', '+27110278720'),
    'email': os.getenv('SHOP_COLLECTION_EMAIL', 'info@metanoiagroup.co.za'),
    'street_address': os.getenv('SHOP_COLLECTION_STREET', 'Metanoia House, 86 Fleet Street'),
    'suburb': os.getenv('SHOP_COLLECTION_SUBURB', 'Ferndale'),
    'city': os.getenv('SHOP_COLLECTION_CITY', 'Randburg'),
    'zone': os.getenv('SHOP_COLLECTION_PROVINCE', 'GP'),
    'code': os.getenv('SHOP_COLLECTION_POSTAL_CODE', '2194'),
}


ENROLLMENT_MODE = os.getenv('ENROLLMENT_MODE', 'manual').strip().lower()
if ENROLLMENT_MODE not in ('auto', 'manual'):
    ENROLLMENT_MODE = 'manual'


MEETING_PROVIDER = os.getenv('MEETING_PROVIDER', 'teams')

MS_TEAMS_ENABLED = env_bool('MS_TEAMS_ENABLED', False)
MS_GRAPH_TENANT_ID = os.getenv('MS_GRAPH_TENANT_ID', '')
MS_GRAPH_CLIENT_ID = os.getenv('MS_GRAPH_CLIENT_ID', '')
MS_GRAPH_CLIENT_SECRET = os.getenv('MS_GRAPH_CLIENT_SECRET', '')
MS_GRAPH_DEFAULT_ORGANIZER = os.getenv('MS_GRAPH_DEFAULT_ORGANIZER', '')
MS_GRAPH_AUTHORITY = os.getenv(
    'MS_GRAPH_AUTHORITY', 'https://login.microsoftonline.com')
MS_GRAPH_BASE = os.getenv('MS_GRAPH_BASE', 'https://graph.microsoft.com/v1.0')

ONEDRIVE_BACKUP_ENABLED = (env_bool('ONEDRIVE_BACKUP_ENABLED')
                           if (os.getenv('ONEDRIVE_BACKUP_ENABLED') or '').strip() else None)
MS_GRAPH_BACKUP_DRIVE_USER = os.getenv('MS_GRAPH_BACKUP_DRIVE_USER', '')
MS_GRAPH_BACKUP_FOLDER = os.getenv('MS_GRAPH_BACKUP_FOLDER', '') or 'metanoia DB BCKP'
BACKUP_RETENTION_DAYS = env_int('BACKUP_RETENTION_DAYS', 3)

YOUTUBE_CLIENT_ID = os.getenv('YOUTUBE_CLIENT_ID', '')
YOUTUBE_CLIENT_SECRET = os.getenv('YOUTUBE_CLIENT_SECRET', '')
YOUTUBE_REFRESH_TOKEN = os.getenv('YOUTUBE_REFRESH_TOKEN', '')

YOUTUBE_CHANNEL_ID = os.getenv('YOUTUBE_CHANNEL_ID', '')
YOUTUBE_API_KEY = os.getenv('YOUTUBE_API_KEY', '')

SESSION_THUMBNAIL_FONT = os.getenv('SESSION_THUMBNAIL_FONT', '')

WHATSAPP_ENABLED = env_bool('WHATSAPP_ENABLED', False)
WHATSAPP_PHONE_NUMBER_ID = os.getenv('WHATSAPP_PHONE_NUMBER_ID', '')
WHATSAPP_BUSINESS_ACCOUNT_ID = os.getenv('WHATSAPP_BUSINESS_ACCOUNT_ID', '')
WHATSAPP_ACCESS_TOKEN = os.getenv('WHATSAPP_ACCESS_TOKEN', '')
WHATSAPP_API_VERSION = os.getenv('WHATSAPP_API_VERSION', 'v23.0')
WHATSAPP_DISPLAY_NUMBER = os.getenv('WHATSAPP_DISPLAY_NUMBER', '')
WHATSAPP_VERIFY_AT_REGISTRATION = env_bool('WHATSAPP_VERIFY_AT_REGISTRATION', True)
WHATSAPP_OTP_TEMPLATE = os.getenv('WHATSAPP_OTP_TEMPLATE', '')
WHATSAPP_TEMPLATE_NAME = os.getenv('WHATSAPP_TEMPLATE_NAME', '')
WHATSAPP_TEMPLATE_LANG = os.getenv('WHATSAPP_TEMPLATE_LANG', 'en')
WHATSAPP_VERIFY_TOKEN = os.getenv('WHATSAPP_VERIFY_TOKEN', '')
WHATSAPP_APP_SECRET = os.getenv('WHATSAPP_APP_SECRET', '')
DEFAULT_DIAL_CODE = os.getenv('DEFAULT_DIAL_CODE', '27')


AI_ASSISTANT_ENABLED = env_bool('AI_ASSISTANT_ENABLED', False)
AI_LLM_MODEL = os.getenv('AI_LLM_MODEL', '')
AI_LLM_API_BASE = os.getenv('AI_LLM_API_BASE', '')
AI_LLM_API_KEY = os.getenv('AI_LLM_API_KEY', '')

WHISPER_MODEL = os.getenv('WHISPER_MODEL', 'base')
WHISPER_DEVICE = os.getenv('WHISPER_DEVICE', 'cpu')
WHISPER_COMPUTE_TYPE = os.getenv('WHISPER_COMPUTE_TYPE', 'int8')

ADMIN_AI_ENABLED = env_bool('ADMIN_AI_ENABLED', False)
ANTHROPIC_API_KEY = os.getenv('ANTHROPIC_API_KEY', '')
ADMIN_AI_MODEL = os.getenv('ADMIN_AI_MODEL', 'claude-opus-5-5')

os.environ.setdefault('CREWAI_TELEMETRY_OPT_OUT',
                      os.getenv('CREWAI_TELEMETRY_OPT_OUT', 'true'))
os.environ.setdefault('OTEL_SDK_DISABLED', os.getenv('OTEL_SDK_DISABLED', 'true'))


ANONYMOUS_USER_NAME = None


from django.contrib.messages import constants as message_constants  # noqa: E402

MESSAGE_TAGS = {message_constants.ERROR: 'danger'}


LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'handlers': {
        'console': {'class': 'logging.StreamHandler'},
        'errorlog': {
            'class': 'apps.diagnostics.handlers.ErrorLogHandler',
            'level': 'WARNING',
        },
    },
    'loggers': {
        'accounts': {
            'handlers': ['console', 'errorlog'],
            'level': 'INFO',
            'propagate': False,
        },
        'apps': {
            'handlers': ['console', 'errorlog'],
            'level': 'INFO',
            'propagate': False,
        },
        'django.request': {
            'handlers': ['console', 'errorlog'],
            'level': 'WARNING',
            'propagate': False,
        },
        'django.security': {
            'handlers': ['console', 'errorlog'],
            'level': 'WARNING',
            'propagate': False,
        },
        'django.security.DisallowedHost': {
            'handlers': ['console'],
            'level': 'ERROR',
            'propagate': False,
        },
    },
}


SENTRY_DSN = os.getenv('SENTRY_DSN', '').strip()
if SENTRY_DSN:  # pragma: no cover - only runs where a DSN is configured
    try:
        import sentry_sdk
        sentry_sdk.init(
            dsn=SENTRY_DSN,
            environment=os.getenv('SENTRY_ENVIRONMENT') or ('development' if DEBUG else 'production'),
            release=os.getenv('SENTRY_RELEASE') or None,
            traces_sample_rate=float(os.getenv('SENTRY_TRACES_SAMPLE_RATE') or 0.05),
            send_default_pii=env_bool('SENTRY_SEND_PII', False),
        )
    except Exception:
        warnings.warn('SENTRY_DSN is set but sentry-sdk could not be initialised.', stacklevel=2)


warnings.filterwarnings('ignore', message='.*NotOpenSSLWarning.*', category=Warning)
warnings.filterwarnings('ignore', category=UserWarning)
