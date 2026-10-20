from django.core.cache import cache
from datetime import timedelta
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APITestCase
from .models import *
import tempfile

@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class ZingoTests(APITestCase):
    def mk(self, name):
        r = self.client.post("/api/auth/register/", {"username": name, "password": "pass12345", "date_of_birth": "2000-01-01"})
        return r.data["token"]
    def auth(self, tok): self.client.credentials(HTTP_AUTHORIZATION="Token " + tok)
    def vid(self, ct="video/mp4"):
        return self.client.post("/api/videos/", {"file": SimpleUploadedFile("a.mp4", b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 16, content_type=ct), "caption": "hi", "mood": "happy"}, format="multipart")
    def setUp(self):
        cache.clear()  # reset rate-limit counters between tests
        self.ta, self.tb = self.mk("alice"), self.mk("bob")

    def test_register_login_duplicate_username(self):
        self.assertEqual(self.client.post("/api/auth/register/", {"username": "alice", "password": "pass12345"}).status_code, 400)
        self.assertEqual(self.client.post("/api/auth/login/", {"username": "alice", "password": "pass12345"}).status_code, 200)
    def test_unauthorized(self):
        self.client.credentials(); self.assertEqual(self.client.get("/api/videos/").status_code, 401)
    def test_profile_edit_and_privacy(self):
        self.auth(self.ta); self.client.patch("/api/me/", {"bio": "yo"}, format="json")
        self.auth(self.tb); d = self.client.get("/api/users/alice/").data
        self.assertEqual(d["bio"], "yo"); self.assertNotIn("email", d); self.assertNotIn("date_of_birth", d)
    def test_follow_unfollow(self):
        self.auth(self.ta); self.client.post("/api/users/bob/follow/")
        self.assertEqual(self.client.get("/api/users/bob/").data["followers"], 1)
        self.client.delete("/api/users/bob/follow/"); self.assertEqual(self.client.get("/api/users/bob/").data["followers"], 0)
        self.assertEqual(self.client.post("/api/users/alice/follow/").status_code, 400)
    def test_upload_validation_like_comment_watch(self):
        self.auth(self.ta)
        self.assertEqual(self.vid("text/plain").status_code, 400)
        vid = self.vid().data["id"]
        self.client.post(f"/api/videos/{vid}/like/"); r = self.client.post(f"/api/videos/{vid}/like/")
        self.assertEqual(r.data["likes"], 1)
        c = self.client.post(f"/api/videos/{vid}/comments/", {"text": "x"}).data["id"]
        self.assertEqual(self.client.post(f"/api/videos/{vid}/comments/", {"text": "re", "parent": c}).status_code, 201)
        self.client.post(f"/api/videos/{vid}/watch/", {"seconds": 3, "completed": True}, format="json")
        self.assertEqual(len(self.client.get("/api/videos/history/").data), 1)
    def test_feed_and_pagination(self):
        self.auth(self.ta)
        for _ in range(12): self.vid()
        self.assertEqual(len(self.client.get("/api/videos/").data["results"]), 10)
        self.assertIsNotNone(self.client.get("/api/videos/").data["next"])
        self.auth(self.tb); self.assertEqual(len(self.client.get("/api/videos/feed/?mood=happy").data["results"]), 10)
        self.assertEqual(len(self.client.get("/api/videos/feed/?mood=sad").data["results"]), 0)
    def test_block_hides_content_and_report(self):
        self.auth(self.ta); self.vid()
        self.auth(self.tb); self.client.post("/api/users/alice/block/")
        self.assertEqual(len(self.client.get("/api/videos/").data["results"]), 0)
        self.assertEqual(self.client.get("/api/users/alice/").status_code, 404)
        self.assertEqual(self.client.post("/api/report/", {"user": 1, "reason": "spam"}).status_code, 201)
    def test_whatnext_voting_and_branching(self):
        self.auth(self.ta); vid = self.vid().data["id"]
        s = self.client.post("/api/stories/", {"video": vid, "title": "Door", "options": ["Open", "Run"]}, format="json").data
        node = self.client.get(f"/api/nodes/{s['root']}/").data; a, b = [o["id"] for o in node["options"]]
        self.auth(self.tb)
        self.assertEqual(self.client.post(f"/api/nodes/{s['root']}/vote/", {"option": a}).status_code, 201)
        self.assertEqual(self.client.post(f"/api/nodes/{s['root']}/vote/", {"option": b}).status_code, 409)
        st = self.client.get(f"/api/nodes/{s['root']}/").data
        self.assertEqual(st["total_votes"], 1); self.assertEqual(st["options"][0]["percent"], 100.0)
        self.assertEqual(self.client.post(f"/api/options/{a}/link/", {"video": vid}).status_code, 404)  # non-owner
        self.auth(self.ta); self.assertEqual(self.client.post(f"/api/options/{a}/link/", {"video": vid}).status_code, 201)
        tree = self.client.get(f"/api/stories/{s['story']}/tree/").data
        self.assertEqual(len(tree["nodes"]), 2)
    def test_whatnext_closed_poll(self):
        self.auth(self.ta); vid = self.vid().data["id"]
        s = self.client.post("/api/stories/", {"video": vid, "options": ["A", "B"]}, format="json").data
        StoryNode.objects.filter(id=s["root"]).update(closes_at=timezone.now() - timedelta(minutes=1))
        o = StoryOption.objects.filter(node_id=s["root"]).first().id
        self.assertEqual(self.client.post(f"/api/nodes/{s['root']}/vote/", {"option": o}).status_code, 400)
        self.assertEqual(self.client.post("/api/stories/", {"video": vid, "options": ["only"]}, format="json").status_code, 400)

    def test_video_flags_and_owner_filter(self):
        self.auth(self.ta); vid = self.vid().data["id"]
        s = self.client.post("/api/stories/", {"video": vid, "options": ["A", "B"]}, format="json").data
        self.auth(self.tb); self.client.post(f"/api/videos/{vid}/like/"); self.client.post("/api/users/alice/follow/")
        d = self.client.get("/api/videos/?owner=alice").data["results"][0]
        self.assertTrue(d["liked"]); self.assertTrue(d["owner_following"]); self.assertEqual(d["story_root"], s["root"])
        self.assertEqual(len(self.client.get("/api/videos/?owner=nobody").data["results"]), 0)
        self.assertTrue(self.client.get("/api/users/alice/").data["is_following"])
        self.assertEqual(self.client.get("/api/videos/feed/?offset=10").data["next_offset"], None)
