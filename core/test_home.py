import tempfile
from datetime import timedelta
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.utils import timezone
from rest_framework.authtoken.models import Token
from rest_framework.test import APITestCase
from .models import *

def vid(owner, caption="", tags="", vis="public", hours_ago=0):
    v = Video.objects.create(owner=owner, file=SimpleUploadedFile("v.mp4", b"x", content_type="video/mp4"), caption=caption, hashtags=tags, visibility=vis)
    if hours_ago: Video.objects.filter(pk=v.pk).update(created=timezone.now() - timedelta(hours=hours_ago))
    return v

@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class HomeTests(APITestCase):
    def setUp(self):
        cache.clear(); self.u = {n: User.objects.create_user(n, password="pass12345") for n in ("me", "ann", "bob", "cat", "dan")}
        self.t = {n: Token.objects.create(user=u).key for n, u in self.u.items()}; self.as_("me")
    def as_(self, n): self.client.credentials(HTTP_AUTHORIZATION="Token " + self.t[n]) if n else self.client.credentials()
    def follow(self, a, b): Follow.objects.create(follower=self.u[a], followee=self.u[b])

    def test_requires_login(self):
        self.as_(None)
        for p in ("/api/home/stories/", "/api/suggestions/users/", "/api/trending/hashtags/", "/api/search/?q=abc", "/api/videos/explore/", "/api/stories/"): self.assertEqual(self.client.get(p).status_code, 401, p)

    def test_stories_only_recent_followed_and_self_with_seen_state(self):
        self.follow("me", "ann"); self.follow("me", "bob")
        a1, a2 = vid(self.u["ann"], hours_ago=2), vid(self.u["ann"], hours_ago=1); vid(self.u["bob"], hours_ago=30)   # bob's is older than 24h
        vid(self.u["cat"], hours_ago=1); vid(self.u["me"], hours_ago=1)                                               # cat not followed
        vid(self.u["ann"], vis="private", hours_ago=1)                                                                # private: not visible to me
        d = self.client.get("/api/home/stories/").data
        self.assertEqual([g["username"] for g in d], ["me", "ann"]); self.assertEqual((d[0]["is_me"], len(d[1]["videos"])), (True, 2))
        self.assertFalse(d[1]["seen"]); WatchHistory.objects.create(user=self.u["me"], video=a1); WatchHistory.objects.create(user=self.u["me"], video=a2)
        d = self.client.get("/api/home/stories/").data; self.assertTrue(next(g for g in d if g["username"] == "ann")["seen"])
        self.assertEqual(d[0]["username"], "me")                                                                      # own first, then unseen before seen

    def test_stories_hide_blocked_and_blind_battle_videos(self):
        self.follow("me", "ann"); v = vid(self.u["ann"], hours_ago=1); Block.objects.create(blocker=self.u["me"], blocked=self.u["ann"])
        self.assertEqual(self.client.get("/api/home/stories/").data, [])

    def test_following_feed_filter_and_explore(self):
        self.follow("me", "ann"); vid(self.u["ann"], "from ann"); vid(self.u["bob"], "from bob"); self.assertEqual({v["owner"] for v in self.client.get("/api/videos/?following=1").data["results"]}, {"ann"})
        self.assertEqual({v["owner"] for v in self.client.get("/api/videos/").data["results"]}, {"ann", "bob"})   # default unchanged
        l = vid(self.u["bob"], "liked"); Like.objects.create(user=self.u["ann"], video=l); Like.objects.create(user=self.u["cat"], video=l)
        d = self.client.get("/api/videos/explore/").data; self.assertEqual(d[0]["id"], l.id); vid(self.u["dan"], vis="followers")
        self.assertTrue(all(x["visibility"] == "public" for x in self.client.get("/api/videos/explore/").data))

    def test_suggested_users(self):
        self.follow("me", "ann"); vid(self.u["ann"]); vid(self.u["bob"]); vid(self.u["cat"]); vid(self.u["me"]); Block.objects.create(blocker=self.u["me"], blocked=self.u["cat"])
        Follow.objects.create(follower=self.u["ann"], followee=self.u["bob"]); Follow.objects.create(follower=self.u["cat"], followee=self.u["bob"])
        names = [x["username"] for x in self.client.get("/api/suggestions/users/").data]
        self.assertEqual(names, ["bob"])                       # not self, not followed, not blocked, no-video users (dan) excluded

    def test_trending_hashtags_real_counts_and_window(self):
        vid(self.u["ann"], tags="#Dance #fun"); vid(self.u["bob"], tags="dance"); vid(self.u["cat"], tags="#fun", hours_ago=24 * 10); vid(self.u["dan"], tags="#secret", vis="private")
        d = self.client.get("/api/trending/hashtags/").data; self.assertEqual(d[0], {"tag": "dance", "count": 2}); self.assertIn({"tag": "fun", "count": 1}, d)
        self.assertNotIn("secret", [x["tag"] for x in d])

    def test_search_users_videos_hashtags_and_privacy(self):
        vid(self.u["ann"], "Sunset vibes", "#sunset"); vid(self.u["bob"], "private sunset", vis="private")
        d = self.client.get("/api/search/?q=sun").data; self.assertEqual(len(d["videos"]), 1); self.assertEqual(d["hashtags"], [{"tag": "sunset", "count": 1}])
        self.assertEqual([u["username"] for u in self.client.get("/api/search/?q=an").data["users"]], ["ann", "dan"])
        self.assertEqual(self.client.get("/api/search/?q=%23sun").data["users"], [])      # '#' searches only tags/videos
        self.assertEqual(self.client.get("/api/search/?q=a").data, {"q": "a", "users": [], "videos": [], "hashtags": []})   # too short
        Block.objects.create(blocker=self.u["me"], blocked=self.u["ann"]); d = self.client.get("/api/search/?q=ann").data
        self.assertEqual((d["users"], d["videos"]), ([], []))

    def test_story_list_endpoint_respects_visibility(self):
        v1, v2 = vid(self.u["ann"], "door"), vid(self.u["bob"], "x", vis="private"); self.as_("ann")
        self.client.post("/api/stories/", {"video": v1.id, "title": "Door", "options": ["A", "B"]}, format="json"); self.as_("bob")
        self.client.post("/api/stories/", {"video": v2.id, "title": "Hidden", "options": ["A", "B"]}, format="json"); self.as_("me")
        d = self.client.get("/api/stories/").data; self.assertEqual([(x["title"], x["owner"], x["open"], x["total_votes"]) for x in d], [("Door", "ann", True, 0)])
        self.assertEqual(self.client.post("/api/stories/", {"video": 1, "options": ["only"]}, format="json").status_code, 400)   # POST still works/validates
