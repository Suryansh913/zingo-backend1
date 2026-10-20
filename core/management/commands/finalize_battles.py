from django.core.management.base import BaseCommand
from core.battles import settle_due
class Command(BaseCommand):
    help = "Finalize ended battles and expire stale challenges (run from cron; also done lazily on API access)."
    def handle(self, *a, **k): settle_due(); self.stdout.write("done")
