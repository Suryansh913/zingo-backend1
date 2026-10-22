
import os

from django.conf import settings
from django.db import connection
from django.http import JsonResponse


def root(request):
    """Service banner so opening the API URL isn't a bare 404."""
    return JsonResponse({
        "service": "zingo-api",
        "status": "ok",
        "health": "/healthz/",
    })


def healthz(request):
    """Health check for Render and the frontend status page."""

    # Check database connection
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        db = "ok"
    except Exception:
        db = "error"

    warnings = []

    uses_s3 = getattr(settings, "USES_S3", False)
    uses_cloudinary = getattr(settings, "USES_CLOUDINARY", False)
    media_root = os.environ.get("MEDIA_ROOT")

    # Production configuration warnings
    if not settings.DEV_DEFAULTS:

        if not uses_s3 and not uses_cloudinary and not media_root:
            warnings.append(
                "Uploads are stored on local disk. Configure Cloudinary "
                "or a persistent disk (MEDIA_ROOT)."
            )

        if connection.vendor == "sqlite":
            warnings.append(
                "The database is SQLite. For persistent production data "
                "on Render, configure DATABASE_URL with PostgreSQL."
            )

        if not settings.CORS_ALLOWED_ORIGINS:
            warnings.append(
                "CORS_ALLOWED_ORIGINS is empty. The React frontend "
                "on another domain may not be able to call this API."
            )

    # Identify configured storage
    if uses_cloudinary:
        storage = "cloudinary"
    elif uses_s3:
        storage = "s3"
    else:
        storage = "local"

    response_data = {
        "status": "ok" if db == "ok" else "degraded",
        "database": db,
        "database_engine": connection.vendor,
        "storage": storage,
        "signed_media_urls": bool(
            uses_s3 and not settings.MEDIA_PUBLIC_READ
        ),
        "cors_configured": bool(settings.CORS_ALLOWED_ORIGINS),
        "debug": settings.DEBUG,
        "release": os.environ.get("RENDER_GIT_COMMIT", "")[:7],
        "warnings": warnings,
    }

    return JsonResponse(
        response_data,
        status=200 if db == "ok" else 503,
    )
