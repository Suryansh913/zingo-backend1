
from django.conf import settings
from django.core.checks import Warning, register
from django.db import connection


@register()
def zingo_production_checks(app_configs, **kwargs):
    """Surfaced by manage.py check, migrate, runserver and check --deploy."""
    if settings.LENIENT_KEY:
        return []

    out = []

    # Warn only when neither S3 nor Cloudinary is configured.
    if not getattr(settings, "USES_S3", False) and not getattr(
        settings, "USES_CLOUDINARY", False
    ):
        out.append(
            Warning(
                "Uploaded media uses local disk storage.",
                hint=(
                    "On Render, local disk may be ephemeral. Configure "
                    "CLOUDINARY_CLOUD_NAME, CLOUDINARY_API_KEY and "
                    "CLOUDINARY_API_SECRET, or use a persistent disk "
                    "and set MEDIA_ROOT."
                ),
                id="core.W001",
            )
        )

    if connection.vendor == "sqlite":
        out.append(
            Warning(
                "The database is SQLite.",
                hint=(
                    "Set DATABASE_URL to a persistent PostgreSQL "
                    "database for production."
                ),
                id="core.W002",
            )
        )

    if not settings.CORS_ALLOWED_ORIGINS:
        out.append(
            Warning(
                "CORS_ALLOWED_ORIGINS is empty.",
                hint=(
                    "Set it to your frontend origin, e.g. "
                    "https://zingo-frontend.onrender.com"
                ),
                id="core.W003",
            )
        )

    if settings.ALLOWED_HOSTS in (["localhost", "127.0.0.1"], []):
        out.append(
            Warning(
                "ALLOWED_HOSTS only contains localhost.",
                hint=(
                    "Set DJANGO_ALLOWED_HOSTS (Render also adds "
                    "RENDER_EXTERNAL_HOSTNAME automatically)."
                ),
                id="core.W004",
            )
        )

    return out
