from django.core.files.storage import default_storage
from django.core.management.base import BaseCommand
from core.models import DatingPhoto, Video

class Command(BaseCommand):
    help = "Report which stored media files (videos, thumbnails, dating photos) are missing from the CURRENT storage. Read-only unless --hide-missing --yes."
    def add_arguments(self, p):
        p.add_argument("--list", action="store_true", help="print every missing file"); p.add_argument("--hide-missing", action="store_true", help="mark videos whose file is missing as status=removed (hidden from feeds); needs --yes"); p.add_argument("--yes", action="store_true")
    def handle(self, *a, **o):
        missing = []; total = 0
        for v in Video.objects.all().iterator():
            for f, kind in ((v.file, "video"), (v.thumbnail, "thumbnail")):
                if f and f.name:
                    total += 1
                    if not default_storage.exists(f.name): missing.append((kind, v.pk, f.name))
        for ph in DatingPhoto.objects.all().iterator():
            total += 1
            if not default_storage.exists(ph.image.name): missing.append(("date_photo", ph.pk, ph.image.name))
        self.stdout.write(f"storage backend: {default_storage.__class__.__name__} | files referenced in the database: {total} | missing in storage: {len(missing)}")
        if o["list"]:
            for kind, pk, name in missing: self.stdout.write(f"  MISSING {kind} #{pk}: {name}")
        if missing: self.stdout.write("Missing files cannot be recovered by this command. If they existed on an old local disk, copy them back and run: python manage.py sync_media_to_storage --source <folder>")
        if o["hide_missing"]:
            if not o["yes"]: self.stderr.write("Refusing: add --yes to confirm hiding videos with missing files."); return
            ids = [pk for k, pk, _ in missing if k == "video"]; n = Video.objects.filter(pk__in=ids).exclude(status="removed").update(status="removed"); self.stdout.write(f"hid {n} video(s) whose file is missing (status=removed; reversible by setting status back to 'active').")
