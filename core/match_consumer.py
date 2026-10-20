import asyncio, json, time
from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer
from django.db.models import Q
from rest_framework.authtoken.models import Token
from .match import LIVE, MatchError, cleanup_session, eligibility, ser_session, touch
from .models import ActiveMatch, MatchSession

AUTH_TIMEOUT = 10; BEAT = 10; MAX_SIGNAL_BYTES = 20000; RATE = (300, 10)
KINDS = {"ready", "offer", "answer", "ice"}

class MatchConsumer(AsyncJsonWebsocketConsumer):
    """WebRTC signaling only (no media). Protocol: auth -> (server: ready,state) -> join{session} -> signal{session,kind,data}.
    Every signal is re-authorised against the DB: sender must belong to that session and it must be 'active'."""
    async def connect(self):
        self.user = None; self.group = None; self.sid = None; self.beat = None; self.sent = []
        await self.accept(); self.timer = asyncio.create_task(self._auth_timeout())

    async def _auth_timeout(self):
        await asyncio.sleep(AUTH_TIMEOUT)
        if not self.user: await self.close(code=4401)

    @database_sync_to_async
    def _auth(self, key):
        t = Token.objects.select_related("user").filter(key=str(key or "")).first()
        return t.user if t and t.user.is_active and not eligibility(t.user) else None

    @database_sync_to_async
    def _state(self):
        slot = ActiveMatch.objects.select_related("session__user_a", "session__user_b").filter(user=self.user).first()
        return ser_session(slot.session, self.user) if slot else None

    @database_sync_to_async
    def _member(self, sid, live_only=False):
        try: sid = int(sid)
        except (TypeError, ValueError): return None
        q = MatchSession.objects.filter(pk=sid).filter(Q(user_a=self.user) | Q(user_b=self.user))
        s = q.filter(status="active").first() if live_only else q.filter(status__in=LIVE).first()
        if not s: return None
        return {"id": s.id, "peer": s.user_b_id if s.user_a_id == self.user.id else s.user_a_id, "role": "caller" if s.user_a_id == self.user.id else "callee", "status": s.status}

    @database_sync_to_async
    def _beat(self):
        touch(self.sid, self.user); s = cleanup_session(self.sid); return s.status if s else None

    async def _heartbeat(self):
        while True:
            await asyncio.sleep(BEAT)
            st = await self._beat()
            if st not in ("pending", "active"): return

    async def receive_json(self, c, **kw):
        if not isinstance(c, dict): return await self.close(code=4400)
        t = c.get("type")
        if self.user is None:
            u = await self._auth(c.get("token")) if t == "auth" else None
            if not u: return await self.close(code=4401)
            self.user = u; self.group = f"mu_{u.id}"; await self.channel_layer.group_add(self.group, self.channel_name)
            await self.send_json({"type": "ready"}); return await self.send_json({"type": "state", "session": await self._state()})
        if t == "ping":
            if self.sid: await self._beat()
            return await self.send_json({"type": "pong"})
        if t == "join":
            m = await self._member(c.get("session"))
            if not m: return await self.send_json({"type": "error", "detail": "Not your session.", "status": 403})
            self.sid = m["id"]
            if self.beat: self.beat.cancel()
            self.beat = asyncio.create_task(self._heartbeat()); await self._beat()
            await self.channel_layer.group_send(f"mu_{m['peer']}", {"type": "match.signal", "session": self.sid, "kind": "peer_online", "data": {}})
            return await self.send_json({"type": "joined", "session": self.sid, "role": m["role"], "status": m["status"]})
        if t != "signal": return await self.send_json({"type": "error", "detail": "Unknown message type."})
        now = time.monotonic(); self.sent = [x for x in self.sent if now - x < RATE[1]]
        if len(self.sent) >= RATE[0]: return await self.send_json({"type": "error", "detail": "Too many signaling messages."})
        self.sent.append(now)
        kind, data = c.get("kind"), c.get("data", {})
        if kind not in KINDS or not isinstance(data, dict) or len(json.dumps(data)) > MAX_SIGNAL_BYTES:
            return await self.send_json({"type": "error", "detail": "Invalid signal.", "status": 400})
        m = await self._member(c.get("session"), live_only=True)   # authorise EVERY event against the active session
        if not m: return await self.send_json({"type": "error", "detail": "No active match for this session.", "status": 403})
        await self.channel_layer.group_send(f"mu_{m['peer']}", {"type": "match.signal", "session": m["id"], "kind": kind, "data": data})

    async def match_event(self, e): await self.send_json(e["event"])
    async def match_signal(self, e):
        if self.sid is None or e["session"] != self.sid: return   # only deliver to the socket that joined this session
        await self.send_json({"type": "signal", "session": e["session"], "kind": e["kind"], "data": e["data"]})

    async def disconnect(self, code):
        for t in (getattr(self, "timer", None), self.beat):
            if t: t.cancel()
        if self.group: await self.channel_layer.group_discard(self.group, self.channel_name)
        if self.sid and self.user:
            m = await self._member(self.sid)
            if m: await self.channel_layer.group_send(f"mu_{m['peer']}", {"type": "match.signal", "session": self.sid, "kind": "peer_offline", "data": {}})
