from pathlib import Path
from django.conf import settings
from django.core.files.base import File
from django.core.files.storage import default_storage
from django.core.management.base import BaseCommand
from core.models import DatingPhoto, Video

class Command(BaseCommand):
    help = "Upload files that still exist in a local folder (default MEDIA_ROOT) to the CURRENT storage (e.g. S3/R2) for database records whose file is not there yet. Never deletes anything."
    def add_arguments(self, p): p.add_argument("--source", default=None, help="folder that mirrors the stored names (default: MEDIA_ROOT)"); p.add_argument("--dry-run", action="store_true")
    def handle(self, *a, **o):
        src = Path(o["source"] or settings.MEDIA_ROOT); names = []
        for v in Video.objects.all().iterator(): names += [f.name for f in (v.file, v.thumbnail) if f and f.name]
        names += [p.image.name for p in DatingPhoto.objects.all().iterator()]
        up = skip = gone = 0
        for n in names:
            if default_storage.exists(n): skip += 1; continue
            local = src / n
            if not local.is_file(): gone += 1; continue
            if not o["dry_run"]:
                with open(local, "rb") as fh: saved = default_storage.save(n, File(fh))
                if saved != n: self.stderr.write(f"warning: stored as {saved} (name was taken): {n}")
            up += 1
        self.stdout.write(f"{'would upload' if o['dry_run'] else 'uploaded'}: {up} | already in storage: {skip} | not found locally (lost): {gone}")
