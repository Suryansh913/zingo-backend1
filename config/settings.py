"""
ZINGO Django settings.

Secure by default. Configure with environment variables (see .env.example / DEPLOY.md); for local development you may put
them in a `.env` file next to manage.py (real environment variables always win). Nothing secret is hard-coded here.
"""
import os, re, sys
from pathlib import Path
import dj_database_url
from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------- environment helpers
def _load_dotenv(path):
    """Minimal .env reader (KEY=VALUE, '#' comments). Existing environment variables are never overridden."""
    if not path.exists(): return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1); v = v.strip()
            if v[:1] in ("'", '"') and v.find(v[0], 1) > 0: v = v[1:v.find(v[0], 1)]   # quoted value: take what is inside the quotes
            else: v = re.split(r"\s+#", v, maxsplit=1)[0].strip()                        # unquoted: drop a trailing "  # comment"
            os.environ.setdefault(k.strip(), v)
_load_dotenv(BASE_DIR / ".env")

def env_bool(k, default=False):
    v = os.environ.get(k)
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "on")
_env_bool = env_bool   # kept for the Match/Date blocks and older patches
def env_list(k, default=""): return [x.strip() for x in os.environ.get(k, default).split(",") if x.strip()]
def env_int(k, default): return int(os.environ.get(k, default))

TESTING = "test" in sys.argv
ON_RENDER = bool(os.environ.get("RENDER"))   # Render sets RENDER=true on its services
# DEBUG is OFF unless explicitly enabled. Exception: the local dev server (`manage.py runserver`) defaults to ON when nothing is set,
# but never on Render.
DEBUG = env_bool("DJANGO_DEBUG", default=("runserver" in sys.argv and not ON_RENDER))
DEV_DEFAULTS = DEBUG or TESTING   # permits localhost conveniences; never true in a normal production process
# Local `manage.py <command>` (migrate, createsuperuser...) may use a throw-away dev key, but only off Render and never when DJANGO_ENV=production.
# A real server start (daphne/gunicorn) is always strict, and so is anything running on Render.
LENIENT_KEY = DEV_DEFAULTS or "collectstatic" in sys.argv or (os.path.basename(sys.argv[0]) == "manage.py" and not ON_RENDER and os.environ.get("DJANGO_ENV") != "production")

# ---------------------------------------------------------------- core security
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY") or ""
if not SECRET_KEY:
    if LENIENT_KEY: SECRET_KEY = "dev-only-insecure-key-do-not-use-in-production"
    else: raise ImproperlyConfigured("DJANGO_SECRET_KEY is not set. Generate one: python -c \"from django.core.management.utils import get_random_secret_key as k; print(k())\"")

RENDER_HOST = os.environ.get("RENDER_EXTERNAL_HOSTNAME", "")   # e.g. zingo-backend-o11v.onrender.com (provided by Render)
ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS") + ([RENDER_HOST] if RENDER_HOST else [])
if DEV_DEFAULTS or not ALLOWED_HOSTS: ALLOWED_HOSTS += ["localhost", "127.0.0.1"]
CSRF_TRUSTED_ORIGINS = env_list("CSRF_TRUSTED_ORIGINS") + ([f"https://{RENDER_HOST}"] if RENDER_HOST else [])

# CORS: explicit origins only (never a wildcard). The API uses an Authorization header, so credentials/cookies are not needed.
CORS_ALLOWED_ORIGINS = env_list("CORS_ALLOWED_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173" if DEV_DEFAULTS else "")
CORS_ALLOW_CREDENTIALS = False

if not DEV_DEFAULTS:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")   # Render terminates TLS and forwards the scheme
    SECURE_SSL_REDIRECT = env_bool("SECURE_SSL_REDIRECT", True)
    SECURE_REDIRECT_EXEMPT = [r"^healthz/?$"]
    SESSION_COOKIE_SECURE = CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = env_int("SECURE_HSTS_SECONDS", 3600)      # start small; raise to 31536000 once everything works over HTTPS
    SECURE_CONTENT_TYPE_NOSNIFF = True

# ---------------------------------------------------------------- apps / middleware
INSTALLED_APPS = [
    "daphne",   # ASGI server (HTTP + WebSockets); must come before staticfiles
    "django.contrib.admin", "django.contrib.auth", "django.contrib.contenttypes", "django.contrib.sessions", "django.contrib.messages",
    "django.contrib.staticfiles", "rest_framework", "rest_framework.authtoken", "corsheaders", "channels", "core",
]
MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",                 # must be before anything that can generate responses
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",            # serves collected static files (admin CSS etc.) in production
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
ROOT_URLCONF = "config.urls"
TEMPLATES = [{"BACKEND": "django.template.backends.django.DjangoTemplates", "DIRS": [], "APP_DIRS": True,
              "OPTIONS": {"context_processors": ["django.template.context_processors.request", "django.contrib.auth.context_processors.auth", "django.contrib.messages.context_processors.messages"]}}]
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"
AUTH_USER_MODEL = "core.User"
AUTH_PASSWORD_VALIDATORS = [{"NAME": f"django.contrib.auth.password_validation.{n}"} for n in ("UserAttributeSimilarityValidator", "MinimumLengthValidator", "CommonPasswordValidator", "NumericPasswordValidator")]
LANGUAGE_CODE = "en-us"; TIME_ZONE = "UTC"; USE_I18N = True; USE_TZ = True

# ---------------------------------------------------------------- database
# # DATABASE_URL (Render Postgres / Supabase)
if os.environ.get("DATABASE_URL"):
    DATABASES = {
        "default": dj_database_url.parse(
            os.environ["DATABASE_URL"],
            conn_max_age=env_int("DB_CONN_MAX_AGE", 600),
            conn_health_checks=True,
            ssl_require=env_bool("DATABASE_SSL_REQUIRE", False),
        )
    }

    # Explicitly ensure the database name is present
    if not DATABASES["default"].get("NAME"):
        DATABASES["default"]["NAME"] = "postgres"

else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": os.environ.get(
                "SQLITE_PATH",
                BASE_DIR / "db.sqlite3",
            ),
        }
    }

# ---------------------------------------------------------------- static + media storage
STATIC_URL = "/static/"; STATIC_ROOT = BASE_DIR / "staticfiles"
MEDIA_URL = "/media/"; MEDIA_ROOT = Path(os.environ.get("MEDIA_ROOT", BASE_DIR / "media"))   # only used for local storage (point at a persistent disk in production)
STORAGES = {"default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
            "staticfiles": {"BACKEND": "whitenoise.storage.CompressedStaticFilesStorage"}}
S3_BUCKET = os.environ.get("AWS_STORAGE_BUCKET_NAME", "")
MEDIA_PUBLIC_READ = env_bool("MEDIA_PUBLIC_READ", False)   # False = private bucket + time-limited signed URLs (respects video/photo privacy)
if S3_BUCKET:   # any S3-compatible service: AWS S3, Cloudflare R2, Backblaze B2, Supabase Storage, MinIO...
    STORAGES["default"] = {"BACKEND": "storages.backends.s3.S3Storage", "OPTIONS": {
        "bucket_name": S3_BUCKET, "access_key": os.environ.get("AWS_ACCESS_KEY_ID", ""), "secret_key": os.environ.get("AWS_SECRET_ACCESS_KEY", ""),
        "endpoint_url": os.environ.get("AWS_S3_ENDPOINT_URL") or None, "region_name": os.environ.get("AWS_S3_REGION_NAME") or None,
        "signature_version": "s3v4", "addressing_style": os.environ.get("AWS_S3_ADDRESSING_STYLE", "auto"),
        "querystring_auth": not MEDIA_PUBLIC_READ, "querystring_expire": env_int("MEDIA_URL_EXPIRE_SECONDS", 6 * 3600),
        "custom_domain": (os.environ.get("AWS_S3_CUSTOM_DOMAIN") or None) if MEDIA_PUBLIC_READ else None,
        "file_overwrite": False, "default_acl": None, "object_parameters": {"CacheControl": "max-age=31536000"}}}
USES_S3 = bool(S3_BUCKET)
SERVE_LOCAL_MEDIA = env_bool("SERVE_LOCAL_MEDIA", True)   # serve /media/ (with HTTP Range) when using local storage

# Uploads
MAX_VIDEO_MB = env_int("MAX_VIDEO_MB", 100)
VIDEO_VERIFY_SIGNATURE = env_bool("VIDEO_VERIFY_SIGNATURE", True)   # reject files whose bytes are not really MP4/MOV/WebM
VIDEO_EXTENSIONS = (".mp4", ".m4v", ".mov", ".webm")

# ---------------------------------------------------------------- REST framework
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": ["rest_framework.authentication.TokenAuthentication"],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.CursorPagination", "PAGE_SIZE": 10,
    "DEFAULT_THROTTLE_CLASSES": ["rest_framework.throttling.UserRateThrottle", "rest_framework.throttling.AnonRateThrottle"],
    "DEFAULT_THROTTLE_RATES": {"user": "300/min", "anon": "30/min", "chat": "60/min", "match": "30/min", "date": "120/min"},
}

# ---------------------------------------------------------------- realtime (chat + match signaling)
REDIS_URL = os.environ.get("REDIS_URL")  # required for >1 server process; unset = in-memory layer (single process only)
CHANNEL_LAYERS = {"default": {"BACKEND": "channels_redis.core.RedisChannelLayer", "CONFIG": {"hosts": [REDIS_URL]}} if REDIS_URL else {"BACKEND": "channels.layers.InMemoryChannelLayer"}}
CHAT_MAX_LEN = 1000

# ---------------------------------------------------------------- battles
BATTLE_VOTING_HOURS_DEFAULT = env_int("BATTLE_VOTING_HOURS_DEFAULT", 24); BATTLE_VOTING_HOURS_MIN = 1; BATTLE_VOTING_HOURS_MAX = env_int("BATTLE_VOTING_HOURS_MAX", 168)
BATTLE_CHALLENGE_EXPIRY_HOURS = env_int("BATTLE_CHALLENGE_EXPIRY_HOURS", 72)
BATTLE_LIVE_COUNTS = env_bool("BATTLE_LIVE_COUNTS", False)

# ---------------------------------------------------------------- ZINGO Match (live video)
MATCH_REQUIRE_VERIFIED_AGE = env_bool("MATCH_REQUIRE_VERIFIED_AGE"); MATCH_MODERATION_CONFIRMED = env_bool("MATCH_MODERATION_CONFIRMED")
_launch = os.environ.get("MATCH_LAUNCH_STATE", "development" if DEBUG else "restricted")
LAUNCH_STATES = ("development", "self_declared", "restricted", "public")
if _launch not in LAUNCH_STATES: _launch = "restricted"                      # unknown value => fail safe
if _launch == "public" and not (MATCH_REQUIRE_VERIFIED_AGE and MATCH_MODERATION_CONFIRMED): _launch = "restricted"   # fail safe
MATCH_LAUNCH_STATE = _launch   # development/self_declared: any consenting self-declared adult (UI says age is unverified) | restricted: staff only | public: verified adults
MATCH_QUEUE_TTL = env_int("MATCH_QUEUE_TTL", 60); MATCH_ACCEPT_SECONDS = env_int("MATCH_ACCEPT_SECONDS", 20); MATCH_DISCONNECT_GRACE = env_int("MATCH_DISCONNECT_GRACE", 30)
MATCH_COOLDOWN_MINUTES = env_int("MATCH_COOLDOWN_MINUTES", 10); MATCH_MAX_SESSIONS_PER_HOUR = env_int("MATCH_MAX_SESSIONS_PER_HOUR", 60)
MATCH_CONSENT_VERSION = 1; MATCH_AUTO_RESTRICT_REPORTS = env_int("MATCH_AUTO_RESTRICT_REPORTS", 3)
STUN_URLS = env_list("STUN_URLS", "stun:stun.l.google.com:19302"); TURN_URLS = env_list("TURN_URLS")
TURN_USERNAME = os.environ.get("TURN_USERNAME", ""); TURN_CREDENTIAL = os.environ.get("TURN_CREDENTIAL", "")
TURN_SHARED_SECRET = os.environ.get("TURN_SHARED_SECRET", ""); TURN_TTL = env_int("TURN_TTL", 3600)

# ---------------------------------------------------------------- ZINGO Date (18+)
DATE_REQUIRE_VERIFIED_AGE = env_bool("DATE_REQUIRE_VERIFIED_AGE"); DATE_MODERATION_CONFIRMED = env_bool("DATE_MODERATION_CONFIRMED")
_dl = os.environ.get("DATE_LAUNCH_STATE", "development" if DEBUG else "restricted")
if _dl not in LAUNCH_STATES: _dl = "restricted"
if _dl == "public" and not (DATE_REQUIRE_VERIFIED_AGE and DATE_MODERATION_CONFIRMED): _dl = "restricted"
DATE_LAUNCH_STATE = _dl
DATE_MAX_PHOTOS = 6; DATE_MAX_PHOTO_MB = 5; DATE_BIO_MAX = 500
DATE_MAX_LIKES_PER_DAY = env_int("DATE_MAX_LIKES_PER_DAY", 100); DATE_AUTO_HOLD_REPORTS = env_int("DATE_AUTO_HOLD_REPORTS", 3)

# ---------------------------------------------------------------- logging
LOGGING = {"version": 1, "disable_existing_loggers": False,
           "handlers": {"console": {"class": "logging.StreamHandler"}},
           "root": {"handlers": ["console"], "level": os.environ.get("LOG_LEVEL", "INFO")},
           "loggers": {"django.request": {"handlers": ["console"], "level": "WARNING", "propagate": False}}}
