from django.core.cache import cache
import json, tempfile
from datetime import timedelta
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APITestCase
from .models import *

@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class BattleTests(APITestCase):
    def setUp(self):
        cache.clear()  # reset rate-limit counters between tests
        self.tok = {}
        for n in ("alice", "bob", "carol", "dave", "erin"):
            self.tok[n] = self.client.post("/api/auth/register/", {"username": n, "password": "pass12345"}).data["token"]
        self.v = {n: self.vid(n) for n in ("alice", "bob", "carol")}
    def as_(self, n): self.client.credentials(HTTP_AUTHORIZATION="Token " + self.tok[n]) if n else self.client.credentials()
    def vid(self, n, vis="public"):
        self.as_(n)
        return self.client.post("/api/videos/", {"file": SimpleUploadedFile("secret_" + n + ".mp4", b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 16, content_type="video/mp4"), "visibility": vis}, format="multipart").data["id"]
    def mk(self, opp="bob", **kw):
        self.as_("alice"); d = {"video": self.v["alice"], "opponent": opp, **kw}
        return self.client.post("/api/battles/", d, format="json")
    def active(self, **kw):
        bid = self.mk(**kw).data["id"]; self.as_("bob")
        r = self.client.post(f"/api/battles/{bid}/accept/", {"video": self.v["bob"]}, format="json"); self.assertEqual(r.status_code, 200, r.data); return bid
    def entries(self, bid):
        self.as_("dave"); return self.client.get(f"/api/battles/{bid}/").data["entries"]
    def vote(self, who, bid, idx):
        e = self.entries(bid)[idx]["id"]; self.as_(who); return self.client.post(f"/api/battles/{bid}/vote/", {"entry": e}, format="json")
    def end(self, bid): Battle.objects.filter(id=bid).update(ends_at=timezone.now() - timedelta(seconds=1))

    def test_create_validation(self):
        r = self.mk(); self.assertEqual(r.status_code, 201); self.assertEqual(r.data["status"], "pending")
        self.assertEqual(self.mk(opp=None).data["status"], "open")
        self.assertEqual(self.mk(opp="alice").status_code, 400)      # self-challenge
        self.assertEqual(self.mk(voting_hours=9999).status_code, 400)
        self.as_("alice"); self.assertEqual(self.client.post("/api/battles/", {"video": self.v["bob"]}, format="json").status_code, 400)  # not own video
        priv = self.vid("alice", "private"); self.assertEqual(self.client.post("/api/battles/", {"video": priv}, format="json").status_code, 400)
        self.as_(None); self.assertEqual(self.client.get("/api/battles/").status_code, 401)

    def test_accept_decline_and_unauthorized(self):
        bid = self.mk().data["id"]
        self.as_("carol"); self.assertEqual(self.client.post(f"/api/battles/{bid}/accept/", {"video": self.v["carol"]}, format="json").status_code, 403)
        self.assertEqual(self.client.post(f"/api/battles/{bid}/decline/", format="json").status_code, 403)
        self.assertEqual(self.client.post(f"/api/battles/{bid}/withdraw/").status_code, 403)
        self.as_("alice"); self.assertEqual(self.client.post(f"/api/battles/{bid}/accept/", {"video": self.v["alice"]}, format="json").status_code, 403)
        self.as_("bob"); self.assertEqual(self.client.post(f"/api/battles/{bid}/accept/", {"video": self.v["alice"]}, format="json").status_code, 400)  # not bob's video
        self.assertEqual(self.client.post(f"/api/battles/{bid}/decline/").data["status"], "declined")
        self.assertEqual(self.client.post(f"/api/battles/{bid}/accept/", {"video": self.v["bob"]}, format="json").status_code, 409)
    def test_public_challenge_single_joiner_and_expiry(self):
        bid = self.mk(opp=None).data["id"]
        self.as_("bob"); self.assertEqual(self.client.post(f"/api/battles/{bid}/accept/", {"video": self.v["bob"]}, format="json").data["status"], "active")
        self.as_("carol"); self.assertEqual(self.client.post(f"/api/battles/{bid}/accept/", {"video": self.v["carol"]}, format="json").status_code, 409)
        old = self.mk().data["id"]; Battle.objects.filter(id=old).update(challenge_expires_at=timezone.now() - timedelta(hours=1))
        self.as_("bob"); self.assertEqual(self.client.post(f"/api/battles/{old}/accept/", {"video": self.v["bob"]}, format="json").status_code, 409)
        self.assertEqual(self.client.get(f"/api/battles/{old}/").data["status"], "expired")
        self.assertEqual(len(self.client.get("/api/battles/?status=open").data["results"]), 0)

    def test_voting_duplicates_participants_and_winner(self):
        bid = self.active()
        self.assertEqual(self.vote("dave", bid, 0).status_code, 200)
        self.assertEqual(self.vote("dave", bid, 1).status_code, 409)           # duplicate
        self.assertEqual(self.vote("alice", bid, 0).status_code, 403)          # participant
        self.assertEqual(BattleVote.objects.filter(battle_id=bid).count(), 1)
        self.assertEqual(self.vote("erin", bid, 0).status_code, 200); self.vote("carol", bid, 1)
        self.as_("dave"); d = self.client.get(f"/api/battles/{bid}/").data
        self.assertTrue(d["counts_hidden"]); self.assertIsNone(d["entries"][0]["votes"])   # counts hidden while live
        self.assertEqual(d["total_votes"], 3); self.assertEqual(d["my_vote"], d["entries"][0]["id"])
        r = self.client.post(f"/api/battles/{bid}/vote/", {"entry": 99999}, format="json"); self.assertEqual(r.status_code, 400)
        self.end(bid); d = self.client.get(f"/api/battles/{bid}/").data
        self.assertEqual(d["status"], "finished"); self.assertEqual(d["result"], "winner")
        win = next(e for e in d["entries"] if e["id"] == d["winner_entry"]); self.assertEqual(win["votes"], 2); self.assertEqual(win["percent"], 66.7)
        self.assertEqual(self.vote("erin", bid, 0).status_code, 409)           # deadline passed

    def test_deadline_blocks_late_votes_even_before_settle(self):
        bid = self.active(); self.end(bid); self.as_("dave")
        e = self.client.get(f"/api/battles/{bid}/").data["entries"][0]["id"]  # GET settles it
        self.assertEqual(self.client.post(f"/api/battles/{bid}/vote/", {"entry": e}, format="json").status_code, 409)

    def test_tie_and_no_votes(self):
        bid = self.active(); self.vote("dave", bid, 0); self.vote("erin", bid, 1); self.end(bid)
        d = self.client.get(f"/api/battles/{bid}/").data; self.assertEqual((d["result"], d["winner"]), ("tie", None))
        b2 = self.active(); self.end(b2); self.assertEqual(self.client.get(f"/api/battles/{b2}/").data["result"], "tie")

    def test_withdraw_forfeit_and_cancel(self):
        bid = self.active(); self.as_("bob"); d = self.client.post(f"/api/battles/{bid}/withdraw/").data
        self.assertEqual((d["status"], d["result"], d["winner"]), ("finished", "forfeit", "alice"))
        self.assertEqual(self.client.post(f"/api/battles/{bid}/withdraw/").status_code, 409)
        p = self.mk().data["id"]; self.as_("alice"); self.assertEqual(self.client.post(f"/api/battles/{p}/withdraw/").data["status"], "cancelled")

    def test_removed_video_forfeits_and_pauses_voting(self):
        bid = self.active(); Video.objects.filter(id=self.v["bob"]).update(status="removed")
        self.assertEqual(self.vote("dave", bid, 0).status_code, 409)
        self.end(bid); self.as_("dave"); d = self.client.get(f"/api/battles/{bid}/").data
        self.assertEqual((d["result"], d["winner"]), ("forfeit", "alice"))
        self.v["alice"], self.v["bob"] = self.vid("alice"), self.vid("bob"); b2 = self.active(); Video.objects.filter(id__in=[self.v["alice"], self.v["bob"]]).delete(); self.end(b2)
        self.assertEqual(self.client.get(f"/api/battles/{b2}/").data["result"], "void")

    def test_blind_privacy_until_finished(self):
        bid = self.active(blind=True, title="blind")
        for who in ("dave", "carol"):
            self.as_(who); txt = json.dumps(self.client.get(f"/api/battles/{bid}/").data, default=str) + json.dumps(self.client.get("/api/battles/?status=active").data, default=str)
            self.assertNotIn("alice", txt); self.assertNotIn("bob", txt); self.assertNotIn("secret_", txt)
            self.assertIsNone(self.client.get(f"/api/battles/{bid}/").data["entries"][0]["user"])
        self.as_("dave"); self.assertEqual(self.client.get("/api/videos/").data["results"][0]["owner"], "carol")  # hidden from feed/list
        self.assertEqual(len(self.client.get("/api/videos/?owner=alice").data["results"]), 0)
        self.as_("alice"); self.assertEqual(len(self.client.get("/api/videos/?owner=alice").data["results"]), 1)       # owner still sees own
        d = self.client.get(f"/api/battles/{bid}/").data; self.assertEqual(d["opponent"], "bob")                         # participant sees all
        self.vote("dave", bid, 0); self.end(bid); self.as_("dave"); d = self.client.get(f"/api/battles/{bid}/").data
        self.assertTrue(d["revealed"]); self.assertEqual({e["user"] for e in d["entries"]}, {"alice", "bob"}); self.assertIsNotNone(d["winner"])
        self.assertEqual(len(self.client.get("/api/videos/?owner=alice").data["results"]), 1)

    def test_blocked_users(self):
        bid = self.active(); self.as_("dave"); self.client.post("/api/users/alice/block/")
        self.assertEqual(self.client.get(f"/api/battles/{bid}/").status_code, 404)
        self.assertEqual(self.client.get("/api/battles/?status=active").data["results"], [])
        self.assertEqual(self.client.post(f"/api/battles/{bid}/vote/", {"entry": 1}, format="json").status_code, 404)
        self.as_("alice"); self.client.post("/api/users/carol/block/"); self.assertEqual(self.mk(opp="carol").status_code, 400)
        p = self.mk(opp=None).data["id"]; self.as_("carol"); self.assertEqual(self.client.post(f"/api/battles/{p}/accept/", {"video": self.v["carol"]}, format="json").status_code, 404)

    def test_history_mine_and_rematch(self):
        bid = self.active(); self.vote("dave", bid, 0); self.end(bid)
        self.as_("bob"); self.assertEqual(self.client.get("/api/battles/mine/").data["results"][0]["id"], bid)
        self.as_("erin"); self.assertEqual(len(self.client.get("/api/users/alice/battles/").data["results"]), 1)
        self.assertEqual(self.client.post(f"/api/battles/{bid}/rematch/", {"video": self.v["carol"]}, format="json").status_code, 403)  # non-participant
        self.as_("bob"); r = self.client.post(f"/api/battles/{bid}/rematch/", {"video": self.v["bob"]}, format="json")
        self.assertEqual((r.status_code, r.data["status"], r.data["rematch_of"], r.data["opponent"]), (200, "pending", bid, "alice"))
        self.assertEqual(self.client.post(f"/api/battles/{bid}/rematch/", {"video": self.v["bob"]}, format="json").status_code, 409)
