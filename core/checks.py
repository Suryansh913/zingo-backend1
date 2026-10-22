from django.conf import settings
from django.core.checks import Warning, register
from django.db import connection

@register()
def zingo_production_checks(app_configs, **kwargs):
    """Surfaced by `manage.py check`, `migrate`, `runserver` and `check --deploy`."""
    if settings.LENIENT_KEY: return []   # local dev (manage.py off Render, DEBUG, tests) shouldn't be nagged
    out = []
    if not settings.USES_S3: out.append(Warning("Uploaded media uses local disk storage.", hint="On Render the disk is ephemeral. Set CLOUDINARY_URL (Cloudinary), or AWS_STORAGE_BUCKET_NAME (+ keys/endpoint) for S3-compatible storage, or mount a persistent disk and set MEDIA_ROOT.", id="core.W001"))
    if connection.vendor == "sqlite": out.append(Warning("The database is SQLite.", hint="Set DATABASE_URL to a persistent Postgres database for production.", id="core.W002"))
    if not settings.CORS_ALLOWED_ORIGINS: out.append(Warning("CORS_ALLOWED_ORIGINS is empty.", hint="Set it to your frontend origin, e.g. https://zingo-frontend.onrender.com", id="core.W003"))
    if settings.ALLOWED_HOSTS in (["localhost", "127.0.0.1"], []): out.append(Warning("ALLOWED_HOSTS only contains localhost.", hint="Set DJANGO_ALLOWED_HOSTS (Render also adds RENDER_EXTERNAL_HOSTNAME automatically).", id="core.W004"))
    return out