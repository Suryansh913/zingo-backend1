import json, tempfile
from datetime import timedelta
from asgiref.sync import sync_to_async
from channels.testing import WebsocketCommunicator
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.authtoken.models import Token
from rest_framework.test import APITestCase
from .models import *

def mkvideo(owner, vis="public", status="active"):
    return Video.objects.create(owner=owner, file=SimpleUploadedFile("v.mp4", b"x", content_type="video/mp4"), visibility=vis, status=status)

@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class ChatRestTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.u = {n: User.objects.create_user(n, password="pass12345") for n in ("alice", "bob", "carol")}
        self.tok = {n: Token.objects.create(user=u).key for n, u in self.u.items()}
    def as_(self, n): self.client.credentials(HTTP_AUTHORIZATION="Token " + self.tok[n]) if n else self.client.credentials()
    def conv(self, a="alice", b="bob"):
        self.as_(a); return self.client.post("/api/conversations/", {"username": b}, format="json")
    def say(self, who, cid, text="hi", **kw):
        self.as_(who); return self.client.post(f"/api/conversations/{cid}/messages/", {"text": text, **kw}, format="json")

    def test_no_duplicate_conversations_and_listing(self):
        r1 = self.conv(); r2 = self.conv("bob", "alice")
        self.assertEqual((r1.status_code, r2.status_code, r1.data["id"]), (201, 200, r2.data["id"]))
        self.assertEqual(Conversation.objects.count(), 1)
        self.assertEqual(self.conv("alice", "alice").status_code, 403)
        self.assertEqual(self.client.post("/api/conversations/", {"username": "nobody"}, format="json").status_code, 404)

    def test_send_receive_history_and_pagination(self):
        cid = self.conv().data["id"]; self.assertEqual(self.say("alice", cid, "hello").status_code, 201)
        self.as_("bob"); d = self.client.get(f"/api/conversations/{cid}/messages/").data["results"][0]
        self.assertEqual((d["text"], d["sender"]), ("hello", "alice")); self.assertIn("created", d)
        Message.objects.bulk_create([Message(conversation_id=cid, sender=self.u["bob"], text=str(i)) for i in range(40)])
        pg = self.client.get(f"/api/conversations/{cid}/messages/").data
        self.assertEqual(len(pg["results"]), 30); self.assertIsNotNone(pg["next"])
        older = self.client.get(pg["next"]).data; self.assertEqual(len(older["results"]), 11)
        ids = [m["id"] for m in pg["results"] + older["results"]]; self.assertEqual(len(set(ids)), 41)

    def test_membership_enforced_and_idor(self):
        cid = self.conv().data["id"]; self.say("alice", cid, "secret")
        self.as_("carol")
        for r in (self.client.get(f"/api/conversations/{cid}/"), self.client.get(f"/api/conversations/{cid}/messages/"),
                  self.client.post(f"/api/conversations/{cid}/read/"), self.say("carol", cid, "intrude")):
            self.assertEqual(r.status_code, 404)
        self.assertEqual(self.client.get("/api/conversations/").data["results"], [])
        self.as_(None); self.assertEqual(self.client.get("/api/conversations/").status_code, 401)
        self.assertEqual(Message.objects.count(), 1)

    def test_blocked_users(self):
        cid = self.conv().data["id"]; self.as_("alice"); self.client.post("/api/users/bob/block/")
        self.assertEqual(self.say("bob", cid, "yo").status_code, 403); self.assertEqual(self.say("alice", cid, "yo").status_code, 403)
        self.assertEqual(self.conv("bob", "alice").status_code, 403)
        for n in ("alice", "bob"):
            self.as_(n); self.assertEqual(self.client.get("/api/conversations/").data["results"], []); self.assertEqual(self.client.get(f"/api/conversations/{cid}/messages/").status_code, 404)
        self.as_("alice"); self.client.delete("/api/users/bob/block/"); self.assertEqual(self.say("bob", cid, "back").status_code, 201)

    def test_private_account_rule(self):
        User.objects.filter(username="carol").update(is_private=True)
        self.assertEqual(self.conv("alice", "carol").status_code, 403)
        self.as_("alice"); self.client.post("/api/users/carol/follow/"); self.assertEqual(self.conv("alice", "carol").status_code, 201)

    def test_validation(self):
        cid = self.conv().data["id"]
        for t in ("", "   ", "x" * 1001): self.assertEqual(self.say("alice", cid, t).status_code, 400)
        self.assertEqual(self.say("alice", cid, "x" * 1000).status_code, 201)
        self.assertEqual(self.say("alice", cid, ["a"]).status_code, 400)

    def test_unread_counts_and_read(self):
        cid = self.conv().data["id"]; self.say("bob", cid, "1"); self.say("bob", cid, "2")
        self.as_("alice"); c = self.client.get("/api/conversations/").data["results"][0]
        self.assertEqual((c["unread"], c["other"]["username"], c["last_message"]["text"]), (2, "bob", "2"))
        self.assertEqual(self.client.get("/api/conversations/unread-count/").data["count"], 1)
        self.assertEqual(self.client.get("/api/notifications/unread-count/").data["count"], 1)  # one DM notification per conversation
        self.client.post(f"/api/conversations/{cid}/read/")
        self.assertEqual(self.client.get("/api/conversations/unread-count/").data["count"], 0)
        self.assertEqual(self.client.get("/api/notifications/unread-count/").data["count"], 0)
        self.as_("bob"); self.assertEqual(self.client.get("/api/conversations/").data["results"][0]["unread"], 0)  # own messages never unread
        self.assertEqual(self.client.get(f"/api/conversations/{cid}/").data["other_last_read_id"], Message.objects.filter(conversation_id=cid).order_by("-id")[0].id)

    def test_idempotent_client_id(self):
        cid = self.conv().data["id"]; a = self.say("alice", cid, "once", client_id="abc"); b = self.say("alice", cid, "once", client_id="abc")
        self.assertEqual((a.status_code, b.status_code, a.data["id"] == b.data["id"], Message.objects.count()), (201, 200, True, 1))

    def test_shared_video_permissions(self):
        cid = self.conv().data["id"]; pub = mkvideo(self.u["alice"]); fol = mkvideo(self.u["alice"], "followers"); prv = mkvideo(self.u["alice"], "private")
        r = self.say("alice", cid, "", video=pub.id); self.assertEqual((r.status_code, r.data["kind"], r.data["video"]["id"]), (201, "video", pub.id))
        self.assertEqual(self.say("alice", cid, "", video=fol.id).status_code, 400)   # bob can't see it
        self.assertEqual(self.say("alice", cid, "", video=prv.id).status_code, 400)
        self.assertEqual(self.say("alice", cid, "", video=99999).status_code, 400)
        self.as_("bob"); self.client.post("/api/users/alice/follow/"); self.assertEqual(self.say("alice", cid, "", video=fol.id).status_code, 201)
        Video.objects.filter(id=pub.id).update(status="removed")
        self.as_("bob"); res = self.client.get(f"/api/conversations/{cid}/messages/").data["results"]
        self.assertTrue(any(m["video"] == {"unavailable": True} for m in res))

    def test_report_message(self):
        cid = self.conv().data["id"]; mid = self.say("alice", cid, "rude").data["id"]
        self.as_("bob"); self.assertEqual(self.client.post("/api/report/", {"message": mid, "reason": "abuse"}, format="json").status_code, 201)
        self.as_("carol"); self.assertEqual(self.client.post("/api/report/", {"message": mid, "reason": "x"}, format="json").status_code, 404)
        self.assertEqual(Report.objects.filter(message_id=mid, user__username="alice").count(), 1)

    def test_send_throttle(self):
        cid = self.conv().data["id"]
        codes = [self.say("alice", cid, str(i)).status_code for i in range(62)]
        self.assertEqual(codes.count(429), 2)

@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class NotificationTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.u = {n: User.objects.create_user(n, password="pass12345") for n in ("alice", "bob", "carol")}
        self.tok = {n: Token.objects.create(user=u).key for n, u in self.u.items()}
    def as_(self, n): self.client.credentials(HTTP_AUTHORIZATION="Token " + self.tok[n])
    def kinds(self, n): return list(Notification.objects.filter(user__username=n).order_by("id").values_list("kind", flat=True))

    def test_follow_like_comment_reply_mention(self):
        v = mkvideo(self.u["alice"]); self.as_("bob")
        self.client.post("/api/users/alice/follow/"); self.client.delete("/api/users/alice/follow/"); self.client.post("/api/users/alice/follow/")  # refollow: no 2nd
        self.client.post(f"/api/videos/{v.id}/like/"); self.client.delete(f"/api/videos/{v.id}/like/"); self.client.post(f"/api/videos/{v.id}/like/")
        c = self.client.post(f"/api/videos/{v.id}/comments/", {"text": "great"}, format="json").data["id"]
        self.assertEqual(self.kinds("alice"), ["follow", "like", "comment"])
        self.as_("carol"); self.client.post(f"/api/videos/{v.id}/comments/", {"text": "@bob hi @alice", "parent": c}, format="json")
        self.assertEqual(self.kinds("bob"), ["mention"])                       # mention beats reply for the same person
        self.assertEqual(self.kinds("alice"), ["follow", "like", "comment", "mention"])
        self.as_("alice"); self.client.post(f"/api/videos/{v.id}/comments/", {"text": "thanks", "parent": c}, format="json")
        self.assertEqual(self.kinds("bob")[-1], "reply"); self.assertEqual(self.kinds("carol"), [])  # own actions: none

    def test_no_notification_for_failed_self_or_blocked_actions(self):
        v = mkvideo(self.u["alice"]); self.as_("alice")
        self.client.post("/api/users/alice/follow/"); self.client.post(f"/api/videos/{v.id}/like/")
        self.client.post(f"/api/videos/{v.id}/comments/", {"text": "me"}, format="json")
        self.as_("bob"); self.client.post("/api/videos/9999/like/")
        self.assertEqual(self.client.post(f"/api/videos/{v.id}/comments/", {"text": "x", "parent": 424242}, format="json").status_code, 400)
        self.assertEqual(self.client.post(f"/api/videos/{v.id}/comments/", {"text": ""}, format="json").status_code, 400)
        self.assertEqual(Notification.objects.count(), 0)
        self.as_("alice"); self.client.post("/api/users/bob/block/"); self.as_("bob"); self.client.post(f"/api/videos/{v.id}/like/")
        self.assertEqual(Notification.objects.count(), 0)                       # blocked pair => silent (and video hidden anyway)

    def test_mark_read_unauthorized_and_all(self):
        for i in range(3): Notification.objects.create(user=self.u["alice"], kind="follow", actor=self.u["bob"], dedupe_key=f"follow:{i}")
        n = Notification.objects.filter(user=self.u["alice"]).first()
        self.as_("bob"); self.assertEqual(self.client.post(f"/api/notifications/{n.id}/read/").status_code, 404)
        self.assertEqual(self.client.get("/api/notifications/").data["results"], [])
        self.as_("alice"); self.assertEqual(self.client.get("/api/notifications/unread-count/").data["count"], 3)
        self.assertTrue(self.client.post(f"/api/notifications/{n.id}/read/").data["read"])
        self.assertEqual(self.client.get("/api/notifications/unread-count/").data["count"], 2)
        self.assertEqual(len(self.client.get("/api/notifications/?unread=1").data["results"]), 2)
        self.assertEqual(self.client.post("/api/notifications/read-all/").data["updated"], 2)
        self.assertEqual(self.client.get("/api/notifications/unread-count/").data["count"], 0)
        self.client.credentials(); self.assertEqual(self.client.get("/api/notifications/").status_code, 401)

    def test_pagination(self):
        Notification.objects.bulk_create([Notification(user=self.u["alice"], kind="follow", actor=self.u["bob"]) for _ in range(25)])
        self.as_("alice"); p = self.client.get("/api/notifications/").data
        self.assertEqual((len(p["results"]), bool(p["next"])), (20, True)); self.assertEqual(len(self.client.get(p["next"]).data["results"]), 5)

    def test_whatnext_result_notified_once(self):
        v = mkvideo(self.u["alice"]); self.as_("alice")
        s = self.client.post("/api/stories/", {"video": v.id, "options": ["A", "B"]}, format="json").data
        o = StoryOption.objects.filter(node_id=s["root"]).first().id
        self.as_("bob"); self.client.post(f"/api/nodes/{s['root']}/vote/", {"option": o}, format="json")
        self.assertEqual(self.client.get("/api/notifications/").data["results"], [])        # still open
        StoryNode.objects.filter(id=s["root"]).update(closes_at=timezone.now() - timedelta(minutes=1))
        for _ in range(2): self.client.get("/api/notifications/")
        self.assertEqual(Notification.objects.filter(kind="story_result").count(), 2)       # creator + 1 voter, once
        self.assertEqual(self.client.get("/api/notifications/").data["results"][0]["story_node"], s["root"])
        self.as_("carol"); self.assertEqual(Notification.objects.filter(user__username="carol").count(), 0)

    def test_battle_invite_and_result(self):
        va, vb = mkvideo(self.u["alice"]), mkvideo(self.u["bob"]); self.as_("alice")
        bid = self.client.post("/api/battles/", {"video": va.id, "opponent": "bob"}, format="json").data["id"]
        self.assertEqual(self.kinds("bob"), ["battle_invite"]); self.assertEqual(self.kinds("alice"), [])
        self.as_("bob"); self.client.post(f"/api/battles/{bid}/accept/", {"video": vb.id}, format="json")
        Battle.objects.filter(id=bid).update(ends_at=timezone.now() - timedelta(seconds=1)); self.client.get(f"/api/battles/{bid}/")
        self.client.get(f"/api/battles/{bid}/")
        self.assertEqual((self.kinds("alice"), self.kinds("bob")), (["battle_result"], ["battle_invite", "battle_result"]))
        n = self.client.get("/api/notifications/").data["results"][0]; self.assertEqual(n["battle"], bid)

APP = None
def app():
    global APP
    if APP is None:
        from config.asgi import application; APP = application
    return APP
ORIGIN = [(b"origin", b"http://localhost:5173")]

class ChatSocketTests(TransactionTestCase):
    def setUp(self):
        cache.clear()
        self.u = {n: User.objects.create_user(n, password="pass12345") for n in ("alice", "bob", "carol")}
        self.tok = {n: Token.objects.create(user=u).key for n, u in self.u.items()}
        from .chat import Conversation as C
        self.conv = Conversation.objects.create(pair_key=f"{self.u['alice'].id}:{self.u['bob'].id}")
        ConversationParticipant.objects.bulk_create([ConversationParticipant(conversation=self.conv, user=self.u[n]) for n in ("alice", "bob")])
    async def connect(self, name, token=None, origin=True):
        c = WebsocketCommunicator(app(), "/ws/chat/", headers=ORIGIN if origin else [])
        ok, _ = await c.connect()
        if not ok: return c, False
        await c.send_json_to({"type": "auth", "token": token or self.tok[name]}); return c, True

    async def test_auth_required_and_origin(self):
        c, ok = await self.connect("alice", origin=False); self.assertFalse(ok)                      # no/foreign origin rejected
        c = WebsocketCommunicator(app(), "/ws/chat/", headers=[(b"origin", b"http://evil.example")]); self.assertFalse((await c.connect())[0])
        c, ok = await self.connect("alice", token="bad"); self.assertTrue(ok); self.assertEqual((await c.receive_output())["type"], "websocket.close")
        c = WebsocketCommunicator(app(), "/ws/chat/", headers=ORIGIN); await c.connect()
        await c.send_json_to({"type": "send", "conversation": self.conv.id, "text": "x"}); self.assertEqual((await c.receive_output())["type"], "websocket.close")
        self.assertEqual(await Message.objects.acount(), 0)
        c, _ = await self.connect("alice"); self.assertEqual((await c.receive_json_from())["type"], "ready"); await c.disconnect()

    async def test_delivery_to_participants_only_and_persisted(self):
        a, _ = await self.connect("alice"); b, _ = await self.connect("bob"); k, _ = await self.connect("carol")
        for c in (a, b, k): self.assertEqual((await c.receive_json_from())["type"], "ready")
        await a.send_json_to({"type": "send", "conversation": self.conv.id, "text": "hey bob", "client_id": "c1"})
        got = {}
        for name, c in (("a1", a), ("a2", a)):
            m = await c.receive_json_from(); got[m["type"]] = m
        self.assertEqual(got["ack"]["client_id"], "c1"); self.assertEqual(got["message"]["message"]["text"], "hey bob")   # sender echo + ack
        m = await b.receive_json_from(); self.assertEqual((m["type"], m["message"]["sender"], m["message"]["client_id"]), ("message", "alice", "c1"))
        self.assertTrue(await k.receive_nothing())                                                   # carol gets nothing
        self.assertEqual(await Message.objects.acount(), 1)
        for c in (a, b, k): await c.disconnect()

    async def test_membership_blocked_validation_and_dedupe(self):
        k, _ = await self.connect("carol"); await k.receive_json_from()
        await k.send_json_to({"type": "send", "conversation": self.conv.id, "text": "intrude", "client_id": "z"})
        e = await k.receive_json_from(); self.assertEqual((e["type"], e["status"]), ("error", 404))
        a, _ = await self.connect("alice"); await a.receive_json_from()
        await a.send_json_to({"type": "send", "conversation": self.conv.id, "text": "  "}); self.assertEqual((await a.receive_json_from())["status"], 400)
        await a.send_json_to({"type": "send", "conversation": self.conv.id, "text": "once", "client_id": "dup"})
        await a.receive_json_from(); await a.receive_json_from()
        await a.disconnect(); a, _ = await self.connect("alice"); await a.receive_json_from()            # reconnect + resend same client_id
        await a.send_json_to({"type": "send", "conversation": self.conv.id, "text": "once", "client_id": "dup"})
        msgs = [await a.receive_json_from() for _ in range(1)]; ack = msgs[0]
        self.assertEqual((ack["type"], ack["duplicate"]), ("ack", True)); self.assertEqual(await Message.objects.acount(), 1)
        await Block.objects.acreate(blocker=self.u["bob"], blocked=self.u["alice"])
        await a.send_json_to({"type": "send", "conversation": self.conv.id, "text": "blocked?"}); self.assertEqual((await a.receive_json_from())["status"], 403)
        self.assertEqual(await Message.objects.acount(), 1)
        for c in (a, k): await c.disconnect()
