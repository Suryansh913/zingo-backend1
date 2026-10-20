import os
from django.core.asgi import get_asgi_application
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django_asgi = get_asgi_application()
from django.conf import settings
from django.urls import path
from channels.routing import ProtocolTypeRouter, URLRouter
from channels.security.websocket import OriginValidator
from core.consumers import ChatConsumer
from core.match_consumer import MatchConsumer

# WebSocket origins: same allow-list as CORS (+ ALLOWED_HOSTS). Browsers always send Origin; missing/foreign origins are rejected.
application = ProtocolTypeRouter({
    "http": django_asgi,
    "websocket": OriginValidator(URLRouter([path("ws/chat/", ChatConsumer.as_asgi()), path("ws/match/", MatchConsumer.as_asgi())]), list(settings.CORS_ALLOWED_ORIGINS) + list(settings.ALLOWED_HOSTS)),
})
