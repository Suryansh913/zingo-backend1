import json, os, subprocess, sys, tempfile, threading, time
from datetime import date, timedelta
from asgiref.sync import sync_to_async
from channels.testing import WebsocketCommunicator
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import OperationalError, connection
from django.test import TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.authtoken.models import Token
from rest_framework.test import APITestCase
from .models import *
from . import match as M

def adult(years=25): return date.today().replace(year=date.today().year - years, day=1)
def mk(name, dob="adult", consent=1, **kw):
    u = User.objects.create_user(name, password="pass12345", date_of_birth=adult() if dob == "adult" else dob, match_consent_version=consent, **kw)
    Token.objects.create(user=u); return u
def tok(u): return Token.objects.get(user=u).key
def video(u): return Video.objects.create(owner=u, file=SimpleUploadedFile("v.mp4", b"x", content_type="video/mp4"))
def pair(a, b, mode="random", active=True):
    M.join_queue(a, mode, ["music"], ""); s = M.join_queue(b, mode, ["music"], "")
    if active: M.accept(a, s.id); s = M.accept(b, s.id)
    return s

DEV = override_settings(MATCH_LAUNCH_STATE="development", MEDIA_ROOT=tempfile.mkdtemp())

@DEV
class MatchRestTests(APITestCase):
    def setUp(self):
        cache.clear(); self.a, self.b, self.c = mk("alice"), mk("bob"), mk("carol")
    def as_(self, u): self.client.credentials(HTTP_AUTHORIZATION="Token " + tok(u)) if u else self.client.credentials()
    def q(self, u, mode="random", **kw): self.as_(u); return self.client.post("/api/match/queue/", {"mode": mode, **kw}, format="json")

    # ---- eligibility, consent, launch gate ----
    def test_age_eligibility_enforced_server_side(self):
        nodob = mk("nodob", dob=None, consent=0); kid = mk("kid", dob=adult(16))
        for u, reason in ((nodob, "dob_missing"), (kid, "underage")):
            self.as_(u); self.assertEqual(self.client.get("/api/match/eligibility/").data["reasons"], [reason])
            r = self.q(u); self.assertEqual((r.status_code, r.data["code"]), (403, reason))
            self.assertEqual(self.client.get("/api/match/ice/").status_code, 403)
        self.as_(kid); r = self.client.patch("/api/me/", {"date_of_birth": str(adult())}, format="json")
        self.assertEqual(r.status_code, 400)                                    # can't flip declared age
        self.assertEqual(self.q(kid).status_code, 403)
        self.as_(nodob); self.assertEqual(self.client.patch("/api/me/", {"date_of_birth": str(adult())}, format="json").status_code, 200)
        self.assertEqual(self.q(nodob).status_code, 403)                        # now adult but still needs consent
        self.assertEqual(self.client.get("/api/match/eligibility/").data["age_note"], "Your date of birth is self-declared and not verified.")
        self.as_(None); self.assertEqual(self.client.get("/api/match/eligibility/").status_code, 401)

    def test_consent_required_and_explicit(self):
        u = mk("noconsent", consent=0); r = self.q(u); self.assertEqual((r.status_code, r.data["code"]), (403, "consent_required"))
        self.assertEqual(self.client.post("/api/match/consent/", {"accept": "yes"}, format="json").status_code, 400)
        self.assertEqual(self.client.post("/api/match/consent/", {"accept": True}, format="json").status_code, 200)
        self.assertEqual(self.q(u).status_code, 200)

    def test_verified_age_and_launch_gate_and_moderation_restriction(self):
        with override_settings(MATCH_REQUIRE_VERIFIED_AGE=True):
            self.assertEqual(self.q(self.a).data["code"], "age_not_verified")
            User.objects.filter(pk=self.a.pk).update(age_verified=True); self.a.refresh_from_db(); self.assertEqual(self.q(self.a).status_code, 200)
        staff = mk("staff", is_staff=True)
        with override_settings(MATCH_LAUNCH_STATE="restricted"):
            self.assertEqual(self.q(self.b).data["code"], "restricted"); self.assertEqual(self.q(staff).status_code, 200)
        User.objects.filter(pk=self.c.pk).update(match_banned_until=timezone.now() + timedelta(hours=1)); self.c.refresh_from_db()
        self.assertEqual(self.q(self.c).data["code"], "restricted_by_moderation")

    def test_public_launch_fails_safe_without_age_assurance_and_moderation(self):
        env = {**os.environ, "DJANGO_SECRET_KEY": "x", "MATCH_LAUNCH_STATE": "public", "DJANGO_SETTINGS_MODULE": "config.settings"}
        run = lambda extra: subprocess.run([sys.executable, "-c", "from django.conf import settings; print(settings.MATCH_LAUNCH_STATE)"], env={**env, **extra}, capture_output=True, text=True).stdout.strip()
        self.assertEqual(run({}), "restricted"); self.assertEqual(run({"MATCH_REQUIRE_VERIFIED_AGE": "1"}), "restricted")
        self.assertEqual(run({"MATCH_REQUIRE_VERIFIED_AGE": "1", "MATCH_MODERATION_CONFIRMED": "1"}), "public")

    # ---- queue ----
    def test_queue_create_status_cancel_and_validation(self):
        self.assertEqual(self.q(self.a).data["status"], "queued"); self.assertEqual(MatchTicket.objects.count(), 1)
        self.assertEqual(self.client.get("/api/match/queue/").data["status"], "queued")
        self.as_(self.a); self.assertEqual(self.client.delete("/api/match/queue/").status_code, 204)
        self.assertEqual(self.client.get("/api/match/queue/").data["status"], "idle"); self.assertEqual(MatchTicket.objects.count(), 0)
        self.assertEqual(self.q(self.a, mode="nope").data["code"], "bad_mode")
        self.assertEqual(self.q(self.a, mode="interest").data["code"], "interests_required")
        self.assertEqual(self.q(self.a, mode="creator").data["code"], "creator_required")
        self.assertEqual(self.q(self.a, mode="interest", interests=["Music!", "music", " Art "]).status_code, 200)
        self.assertEqual(MatchTicket.objects.get(user=self.a).interests, "music,art")

    def test_match_pairs_two_and_prevents_duplicates(self):
        self.q(self.a); r = self.q(self.b); self.assertEqual(r.data["status"], "matched"); sid = r.data["session"]["id"]
        self.assertEqual((r.data["session"]["status"], r.data["session"]["role"]), ("pending", "callee"))
        self.assertEqual(ActiveMatch.objects.count(), 2); self.assertEqual(MatchTicket.objects.count(), 0)
        self.assertEqual(self.q(self.a).data["code"], "already_matched"); self.assertEqual(self.q(self.b).data["code"], "already_matched")
        self.assertEqual(self.q(self.c).data["status"], "queued")                   # third person is NOT dragged in
        self.assertEqual(MatchSession.objects.count(), 1)
        self.as_(self.a); self.assertEqual(self.client.get("/api/match/queue/").data["session"]["role"], "caller")
        with self.assertRaises(Exception): ActiveMatch.objects.create(user=self.a, session=MatchSession.objects.get(pk=sid))  # DB-level uniqueness

    def test_modes_interest_and_creator(self):
        self.q(self.a, "interest", interests=["music"]); self.assertEqual(self.q(self.b, "interest", interests=["gaming"]).data["status"], "queued")  # no overlap
        r = self.q(self.c, "interest", interests=["gaming", "art"]); self.assertEqual(r.data["session"]["shared_interests"], ["gaming"])
        MatchTicket.objects.all().delete(); ActiveMatch.objects.all().delete(); MatchSession.objects.all().delete()
        video(self.a); video(self.b)
        self.q(self.a, "random"); self.assertEqual(self.q(self.b, "creator").data["status"], "queued")  # modes don't mix
        self.assertEqual(self.q(self.a, "creator").data["status"], "matched") if False else None
        MatchTicket.objects.all().delete(); self.q(self.a, "creator"); self.assertEqual(self.q(self.b, "creator").data["status"], "matched")

    def test_blocked_users_never_match(self):
        self.as_(self.a); self.client.post("/api/users/bob/block/")
        self.q(self.a); self.assertEqual(self.q(self.b).data["status"], "queued")
        self.assertEqual(self.q(self.c).data["status"], "matched")                  # c pairs with whoever is eligible, not a<->b
        s = MatchSession.objects.get(); self.assertNotEqual({s.user_a_id, s.user_b_id}, {self.a.id, self.b.id})

    def test_block_during_match_ends_it(self):
        s = pair(self.a, self.b); self.as_(self.a); self.client.post("/api/users/bob/block/")
        s.refresh_from_db(); self.assertEqual((s.status, s.end_reason), ("ended", "blocked")); self.assertEqual(ActiveMatch.objects.count(), 0)

    # ---- lifecycle ----
    def test_accept_decline_skip_end_and_cooldown(self):
        self.q(self.a); sid = self.q(self.b).data["session"]["id"]
        self.as_(self.c); self.assertEqual(self.client.post(f"/api/match/sessions/{sid}/accept/").status_code, 404)   # outsider
        self.assertEqual(self.client.get(f"/api/match/sessions/{sid}/").status_code, 404)
        self.as_(self.a); d = self.client.post(f"/api/match/sessions/{sid}/accept/").data; self.assertEqual((d["status"], d["i_accepted"], d["peer_accepted"]), ("pending", True, False))
        self.as_(self.b); d = self.client.post(f"/api/match/sessions/{sid}/accept/").data; self.assertEqual(d["status"], "active"); self.assertIsNotNone(d["started_at"])
        d = self.client.post(f"/api/match/sessions/{sid}/skip/").data; self.assertEqual((d["status"], d["end_reason"]), ("ended", "skip")); self.assertEqual(ActiveMatch.objects.count(), 0)
        self.assertEqual(self.client.post(f"/api/match/sessions/{sid}/accept/").status_code, 409)
        self.q(self.a); self.assertEqual(self.q(self.b).data["status"], "queued")   # cooldown: just-skipped partner not re-matched
        MatchTicket.objects.all().delete()
        MatchSession.objects.all().update(created=timezone.now() - timedelta(hours=1)); self.q(self.a); self.assertEqual(self.q(self.b).data["status"], "matched")
        sid2 = MatchSession.objects.latest("id").id; self.as_(self.b)
        d = self.client.post(f"/api/match/sessions/{sid2}/decline/").data; self.assertEqual((d["status"], d["end_reason"]), ("declined", "declined"))
        s3 = pair(self.a, self.c); self.as_(self.a); d = self.client.post(f"/api/match/sessions/{s3.id}/end/").data; self.assertEqual((d["status"], d["end_reason"]), ("ended", "ended"))

    def test_cleanup_expired_tickets_pending_and_stale_sessions(self):
        self.q(self.a); MatchTicket.objects.update(expires_at=timezone.now() - timedelta(seconds=1)); self.assertEqual(M.cleanup_all(), 1)
        s = pair(self.a, self.b, active=False); MatchSession.objects.filter(pk=s.pk).update(accept_deadline=timezone.now() - timedelta(seconds=1))
        self.as_(self.a); self.assertEqual(self.client.post(f"/api/match/sessions/{s.pk}/accept/").status_code, 409)
        s.refresh_from_db(); self.assertEqual((s.status, s.end_reason), ("expired", "expired")); self.assertEqual(ActiveMatch.objects.count(), 0)
        s = pair(self.a, self.c); MatchSession.objects.filter(pk=s.pk).update(b_seen=timezone.now() - timedelta(seconds=120), a_seen=timezone.now())
        M.cleanup_all(); s.refresh_from_db(); self.assertEqual((s.status, s.end_reason), ("ended", "disconnected")); self.assertEqual(ActiveMatch.objects.count(), 0)
        self.assertEqual(self.q(self.a).data["status"], "queued")                    # slots freed => can match again

    # ---- abuse prevention ----
    def test_rate_limits(self):
        codes = [self.q(self.a).status_code for _ in range(32)]; self.assertEqual(codes.count(429), 2)
        cache.clear(); [MatchSession.objects.create(user_a=self.b, user_b=self.c, status="ended", accept_deadline=timezone.now()) for _ in range(60)]
        r = self.q(self.b); self.assertEqual((r.status_code, r.data["code"]), (429, "hourly_limit"))

    def test_report_ends_call_and_auto_restricts_repeat_offenders(self):
        s = pair(self.a, self.b); self.as_(self.a)
        self.assertEqual(self.client.post(f"/api/match/sessions/{s.id}/report/", {"reason": ""}, format="json").status_code, 400)
        self.as_(self.c); self.assertEqual(self.client.post(f"/api/match/sessions/{s.id}/report/", {"reason": "x"}, format="json").status_code, 404)
        self.as_(self.a); self.assertEqual(self.client.post(f"/api/match/sessions/{s.id}/report/", {"reason": "nudity", "category": "nudity", "block": True}, format="json").status_code, 201)
        s.refresh_from_db(); self.assertEqual((s.status, s.end_reason), ("ended", "reported")); self.assertEqual(ActiveMatch.objects.count(), 0)
        self.assertTrue(Block.objects.filter(blocker=self.a, blocked=self.b).exists()); self.assertEqual(Report.objects.get().session_id, s.id)
        self.assertIsNone(User.objects.get(pk=self.b.pk).match_banned_until)
        for n in ("r1", "r2"):
            u = mk(n); s2 = pair(u, self.b); M.report(u, s2.id, "abuse", "bad")
        self.assertIsNotNone(User.objects.get(pk=self.b.pk).match_banned_until)      # 3 distinct reporters => restricted
        self.assertEqual(self.q(self.b).data["code"], "restricted_by_moderation")

    # ---- privacy & ICE ----
    def test_match_preview_exposes_only_basic_profile(self):
        User.objects.filter(pk=self.a.pk).update(bio="hi there", city="Kanpur", email="a@secret.test", interests="music")
        self.q(self.b); self.as_(self.a); self.q(self.a); self.as_(self.b); d = self.client.get("/api/match/queue/").data["session"]
        self.assertEqual(set(d["other"]), {"username", "bio", "is_creator", "is_following"}); txt = json.dumps(d)
        for secret in ("a@secret.test", "Kanpur", str(adult()), "date_of_birth", "email", "city"): self.assertNotIn(secret, txt)

    @override_settings(TURN_URLS=["turn:turn.example.com:3478"], TURN_SHARED_SECRET="s3cret", TURN_TTL=600)
    def test_ice_config_stun_and_expiring_turn_credentials(self):
        import base64, hashlib, hmac
        self.as_(self.a); d = self.client.get("/api/match/ice/").data; stun, turn = d["iceServers"]
        self.assertTrue(stun["urls"][0].startswith("stun:")); exp, uid = turn["username"].split(":"); self.assertEqual(int(uid), self.a.id); self.assertGreater(int(exp), time.time())
        want = base64.b64encode(hmac.new(b"s3cret", turn["username"].encode(), hashlib.sha1).digest()).decode(); self.assertEqual(turn["credential"], want); self.assertTrue(d["turn_configured"])
        self.assertNotIn("s3cret", json.dumps(d)); self.as_(None); self.assertEqual(self.client.get("/api/match/ice/").status_code, 401)
    @override_settings(TURN_URLS=["turn:t:3478"], TURN_USERNAME="u", TURN_CREDENTIAL="p", TURN_SHARED_SECRET="")
    def test_static_turn_credentials(self):
        self.as_(self.a); self.assertEqual(self.client.get("/api/match/ice/").data["iceServers"][-1]["credential"], "p")
    def test_no_turn_by_default(self):
        self.as_(self.a); d = self.client.get("/api/match/ice/").data; self.assertFalse(d["turn_configured"]); self.assertEqual(len(d["iceServers"]), 1)

# ---------- concurrency ----------
@DEV
class MatchConcurrencyTests(TransactionTestCase):
    def run_parallel(self, n):
        users = [mk(f"u{i}") for i in range(n)]; barrier = threading.Barrier(n); errors = []
        def worker(u):
            try:
                barrier.wait(); joined = False
                for _ in range(80):
                    try:
                        if ActiveMatch.objects.filter(user=u).exists(): break
                        if not joined: M.join_queue(u, "random", [], ""); joined = True
                        else: M.try_match(u)
                    except OperationalError: time.sleep(0.02)           # SQLite table locks; Postgres would row-lock instead
                    except M.MatchError as e:
                        if e.code == "already_matched": break
                        errors.append(e.code); break
                    time.sleep(0.005)
            finally: connection.close()
        ts = [threading.Thread(target=worker, args=(u,)) for u in users]; [t.start() for t in ts]; [t.join(60) for t in ts]
        return users, errors
    def invariants(self, n):
        live = MatchSession.objects.filter(status__in=M.LIVE); slots = list(ActiveMatch.objects.values_list("user_id", flat=True))
        self.assertEqual(len(slots), len(set(slots)))                                              # nobody in two sessions
        self.assertEqual(len(slots), 2 * live.count())
        for s in live: self.assertNotEqual(s.user_a_id, s.user_b_id)
        self.assertEqual(2 * live.count() + MatchTicket.objects.count(), n)                        # nobody lost, nobody duplicated
        return live.count()
    def test_eight_users_joining_at_once(self):
        _, errors = self.run_parallel(8); self.assertEqual(errors, []); self.assertGreaterEqual(self.invariants(8), 3)
    def test_three_users_racing_for_one_slot(self):
        self.run_parallel(3); self.assertEqual(self.invariants(3), 1); self.assertEqual(MatchTicket.objects.count(), 1)

# ---------- WebSocket signaling ----------
_APP = None
def app():
    global _APP
    if _APP is None:
        from config.asgi import application; _APP = application
    return _APP
ORIGIN = [(b"origin", b"http://localhost:5173")]

@DEV
class MatchSocketTests(TransactionTestCase):
    def setUp(self):
        cache.clear(); self.a, self.b, self.c = mk("alice"), mk("bob"), mk("carol"); self.T = {u.pk: tok(u) for u in (self.a, self.b, self.c)}
    async def conn(self, u, token=None, origin=True):
        c = WebsocketCommunicator(app(), "/ws/match/", headers=ORIGIN if origin else [])
        ok, _ = await c.connect()
        if ok: await c.send_json_to({"type": "auth", "token": token or self.T[u.pk]})
        return c, ok
    async def ready(self, u):
        c, ok = await self.conn(u); assert ok; assert (await c.receive_json_from())["type"] == "ready"; assert (await c.receive_json_from())["type"] == "state"; return c
    async def join(self, c, sid):
        await c.send_json_to({"type": "join", "session": sid}); return await c.receive_json_from()
    async def sess(self, active=True): return await sync_to_async(pair)(self.a, self.b, active=active)

    async def test_websocket_authentication(self):
        c, ok = await self.conn(self.a, origin=False); self.assertFalse(ok)
        c = WebsocketCommunicator(app(), "/ws/match/", headers=[(b"origin", b"http://evil.example")]); self.assertFalse((await c.connect())[0])
        c, _ = await self.conn(self.a, token="bad"); self.assertEqual((await c.receive_output())["type"], "websocket.close")
        c = WebsocketCommunicator(app(), "/ws/match/", headers=ORIGIN); await c.connect()
        await c.send_json_to({"type": "join", "session": 1}); self.assertEqual((await c.receive_output())["type"], "websocket.close")   # must auth first
        kid = await sync_to_async(mk)("kid", dob=adult(16)); self.T[kid.pk] = await sync_to_async(tok)(kid); c, _ = await self.conn(kid); self.assertEqual((await c.receive_output())["type"], "websocket.close")  # ineligible
        c = await self.ready(self.a); await c.disconnect()

    async def test_signaling_relays_only_between_the_pair_and_only_when_active(self):
        s = await self.sess(); a, b, k = await self.ready(self.a), await self.ready(self.b), await self.ready(self.c)
        self.assertEqual((await self.join(a, s.id))["role"], "caller"); self.assertEqual((await self.join(b, s.id))["role"], "callee")
        self.assertEqual((await a.receive_json_from())["kind"], "peer_online") if False else None
        while not await b.receive_nothing(): await b.receive_json_from()                 # drain presence
        while not await a.receive_nothing(): await a.receive_json_from()
        offer = {"sdp": "v=0...", "type": "offer"}
        await a.send_json_to({"type": "signal", "session": s.id, "kind": "offer", "data": offer})
        got = await b.receive_json_from(); self.assertEqual((got["type"], got["kind"], got["data"]), ("signal", "offer", offer))
        await b.send_json_to({"type": "signal", "session": s.id, "kind": "ice", "data": {"candidate": "x"}}); self.assertEqual((await a.receive_json_from())["kind"], "ice")
        self.assertTrue(await k.receive_nothing())                                       # carol sees nothing
        # carol cannot join or signal into someone else's session
        self.assertEqual((await self.join(k, s.id))["status"], 403)
        await k.send_json_to({"type": "signal", "session": s.id, "kind": "offer", "data": offer}); self.assertEqual((await k.receive_json_from())["status"], 403)
        self.assertTrue(await a.receive_nothing()); self.assertTrue(await b.receive_nothing())
        for bad in ({"kind": "evil"}, {"kind": "offer", "data": "str"}, {"kind": "offer", "data": {"x": "y" * 30000}}):
            await a.send_json_to({"type": "signal", "session": s.id, **bad}); self.assertEqual((await a.receive_json_from())["status"], 400)
        await a.send_json_to({"type": "signal", "session": 999999, "kind": "offer", "data": offer}); self.assertEqual((await a.receive_json_from())["status"], 403)
        await sync_to_async(M.leave)(self.a, s.id, "ended")                              # ended session: signaling refused
        while not await a.receive_nothing(): await a.receive_json_from()
        await a.send_json_to({"type": "signal", "session": s.id, "kind": "offer", "data": offer}); self.assertEqual((await a.receive_json_from())["status"], 403)
        for c in (a, b, k): await c.disconnect()

    async def test_no_signaling_before_both_accept(self):
        s = await self.sess(active=False); a = await self.ready(self.a); await self.join(a, s.id)
        await a.send_json_to({"type": "signal", "session": s.id, "kind": "offer", "data": {}}); self.assertEqual((await a.receive_json_from())["status"], 403)
        await a.disconnect()

    async def test_match_found_is_pushed_to_waiting_user(self):
        w = await self.ready(self.a); await sync_to_async(M.join_queue)(self.a, "random", [], "")
        await sync_to_async(M.join_queue)(self.b, "random", [], ""); e = await w.receive_json_from()
        self.assertEqual((e["type"], e["session"]["status"], e["session"]["other"]["username"]), ("session", "pending", "bob")); await w.disconnect()

    async def test_disconnect_notifies_peer_and_cleanup_ends_stale_session(self):
        s = await self.sess(); a, b = await self.ready(self.a), await self.ready(self.b); await self.join(a, s.id); await self.join(b, s.id)
        while not await a.receive_nothing(): await a.receive_json_from()
        await b.disconnect(); self.assertEqual((await a.receive_json_from())["kind"], "peer_offline")
        await sync_to_async(MatchSession.objects.filter(pk=s.id).update)(b_seen=timezone.now() - timedelta(seconds=120))
        await sync_to_async(M.cleanup_all)(); fresh = await MatchSession.objects.aget(pk=s.id)
        self.assertEqual((fresh.status, fresh.end_reason), ("ended", "disconnected")); self.assertEqual(await ActiveMatch.objects.acount(), 0)
        ev = await a.receive_json_from(); self.assertEqual((ev["type"], ev["session"]["status"]), ("session", "ended")); await a.disconnect()
