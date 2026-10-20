from django.core.management.base import BaseCommand
from core.match import cleanup_all
class Command(BaseCommand):
    help = "Expire stale match tickets/sessions. Run every minute from cron (also done lazily on API access)."
    def handle(self, *a, **k): self.stdout.write(f"expired tickets: {cleanup_all()}")
