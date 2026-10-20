import asyncio, time
from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer
from rest_framework.authtoken.models import Token
from .chat import ChatError, send_message

AUTH_TIMEOUT = 10; RATE = (30, 10)  # max 30 sends per 10 s per connection

class ChatConsumer(AsyncJsonWebsocketConsumer):
    """One socket per browser tab. Protocol: {type:"auth",token} first (tokens never travel in the URL), then
    {type:"send",conversation,text?,video?,client_id?}. Server pushes {type:"message",message}, {type:"ack"}, {type:"error"}."""
    async def connect(self):
        self.user = None; self.group = None; self.sent = []
        await self.accept(); self.timer = asyncio.create_task(self._timeout())

    async def _timeout(self):
        await asyncio.sleep(AUTH_TIMEOUT)
        if not self.user: await self.close(code=4401)

    @database_sync_to_async
    def _user(self, key):
        t = Token.objects.select_related("user").filter(key=str(key or "")).first()
        return t.user if t and t.user.is_active else None

    @database_sync_to_async
    def _send(self, c): return send_message(self.user, c.get("conversation"), c.get("text"), c.get("video"), c.get("client_id"))

    async def receive_json(self, c, **kw):
        if not isinstance(c, dict): return await self.close(code=4400)
        t = c.get("type")
        if self.user is None:
            u = await self._user(c.get("token")) if t == "auth" else None
            if not u: return await self.close(code=4401)
            self.user = u; self.group = f"user_{u.id}"; await self.channel_layer.group_add(self.group, self.channel_name)
            return await self.send_json({"type": "ready"})
        if t == "ping": return await self.send_json({"type": "pong"})
        if t != "send": return await self.send_json({"type": "error", "detail": "Unknown message type."})
        now = time.monotonic(); self.sent = [x for x in self.sent if now - x < RATE[1]]
        if len(self.sent) >= RATE[0]: return await self.send_json({"type": "error", "client_id": c.get("client_id"), "detail": "You're sending too fast."})
        self.sent.append(now)
        try:
            m, created = await self._send(c)
            await self.send_json({"type": "ack", "client_id": c.get("client_id"), "message_id": m.id, "duplicate": not created})
        except ChatError as e: await self.send_json({"type": "error", "client_id": c.get("client_id"), "detail": e.msg, "status": e.status})

    async def chat_message(self, event): await self.send_json({"type": "message", "message": event["message"]})

    async def disconnect(self, code):
        if getattr(self, "timer", None): self.timer.cancel()
        if self.group: await self.channel_layer.group_discard(self.group, self.channel_name)
