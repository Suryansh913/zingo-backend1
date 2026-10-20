#!/usr/bin/env bash
# Production start command (Render: `bash start.sh`). Runs HTTP + WebSockets with Daphne.
set -euo pipefail
if [ "${RUN_MIGRATIONS:-0}" = "1" ]; then
  echo "Applying database migrations (additive only)..."
  python manage.py migrate --noinput
fi
# Optional read-only report of uploads whose files are missing from the CURRENT storage (output appears in the Render logs). Changes nothing.
if [ "${RUN_MEDIA_CHECK:-0}" = "1" ]; then
  python manage.py check_media --list || echo "(media check failed; continuing)"
fi
# Optional first admin account (for /admin moderation). Set all three env vars once, then remove the password.
if [ -n "${DJANGO_SUPERUSER_USERNAME:-}" ] && [ -n "${DJANGO_SUPERUSER_PASSWORD:-}" ] && [ -n "${DJANGO_SUPERUSER_EMAIL:-}" ]; then
  if out=$(python manage.py createsuperuser --noinput 2>&1); then
    echo "Created admin user '${DJANGO_SUPERUSER_USERNAME}'."
  elif echo "$out" | grep -qi "already taken"; then
    echo "Admin user '${DJANGO_SUPERUSER_USERNAME}' already exists (nothing changed)."
  else
    echo "WARNING: could not create the admin user: $out"   # never blocks startup
  fi
fi
exec daphne -b 0.0.0.0 -p "${PORT:-8000}" config.asgi:application
