import os
from django.conf import settings
from django.db import connection
from django.http import JsonResponse

def root(request):
    """Service banner so opening the API's own URL isn't a bare 404."""
    return JsonResponse({"service": "zingo-api", "status": "ok", "health": "/healthz/"})

def healthz(request):
    """Used by Render's health check and the frontend /status page. Never exposes secrets."""
    try:
        with connection.cursor() as c: c.execute("SELECT 1")
        db = "ok"
    except Exception: db = "error"
    warnings = []
    if not settings.DEV_DEFAULTS:
        if not settings.USES_S3 and not os.environ.get("MEDIA_ROOT"): warnings.append("Uploads are stored on this server's local disk. On Render that disk is wiped on every deploy: configure S3-compatible storage (AWS_STORAGE_BUCKET_NAME ...) or a persistent disk (MEDIA_ROOT).")
        if connection.vendor == "sqlite": warnings.append("The database is SQLite. On Render it is wiped on every deploy: set DATABASE_URL to a Postgres database.")
        if not settings.CORS_ALLOWED_ORIGINS: warnings.append("CORS_ALLOWED_ORIGINS is empty: the React site on another domain cannot call this API.")
    return JsonResponse({"status": "ok" if db == "ok" else "degraded", "database": db, "database_engine": connection.vendor, "storage": "s3" if settings.USES_S3 else "local",
                         "signed_media_urls": bool(settings.USES_S3 and not settings.MEDIA_PUBLIC_READ), "cors_configured": bool(settings.CORS_ALLOWED_ORIGINS),
                         "debug": settings.DEBUG, "release": os.environ.get("RENDER_GIT_COMMIT", "")[:7], "warnings": warnings}, status=200 if db == "ok" else 503)
