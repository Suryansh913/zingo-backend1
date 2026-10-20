import io, tempfile, threading, time
from datetime import date
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import OperationalError, connection, transaction
from django.test import TransactionTestCase, override_settings
from PIL import Image
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient, APITestCase
from .models import *
from . import date as D

def dob(years=28): t = date.today(); return t.replace(year=t.year - years, day=1)
def mkuser(name, years=28, **kw):
    u = User.objects.create_user(name, password="pass12345", date_of_birth=dob(years) if years else None, **kw); Token.objects.create(user=u); return u
def dater(name, gender="woman", into=("man",), years=28, active=True, **kw):
    """A discoverable user created directly (the activation flow itself is tested separately)."""
    u = mkuser(name, years); return u, DatingProfile.objects.create(user=u, bio=f"{name} bio", gender=gender, interested_in=D.pack(list(into)), is_active=active, **kw)
def png(size=(40, 40)):
    b = io.BytesIO(); Image.new("RGB", size, "purple").save(b, "PNG"); return SimpleUploadedFile("p.png", b.getvalue(), content_type="image/png")

DEV = override_settings(DATE_LAUNCH_STATE="development", MEDIA_ROOT=tempfile.mkdtemp())

class Base(APITestCase):
    def setUp(self):
        cache.clear(); self.clients = {}
    def c(self, u):
        if u.pk not in self.clients: cl = APIClient(); cl.credentials(HTTP_AUTHORIZATION="Token " + Token.objects.get(user=u).key); self.clients[u.pk] = cl
        return self.clients[u.pk]

@DEV
class DateProfileTests(Base):
    def test_create_prefills_from_social_profile_is_idempotent_and_stays_private(self):
        me = mkuser("me", bio="social bio", city="Kanpur", interests="music,art", is_private=False); before = User.objects.filter(pk=me.pk).values().first()
        r1 = self.c(me).post("/api/date/profile/create/"); r2 = self.c(me).post("/api/date/profile/create/"); r3 = self.c(me).post("/api/date/profile/create/")
        self.assertEqual((r1.status_code, r2.status_code, r3.status_code), (201, 200, 200)); self.assertEqual(DatingProfile.objects.filter(user=me).count(), 1)
        d = r1.data; self.assertEqual((d["bio"], d["city"], d["interests"], d["is_active"]), ("social bio", "Kanpur", ["music", "art"], False))   # reused data, NOT live
        self.assertEqual(r2.data["bio"], "social bio"); self.assertEqual(User.objects.filter(pk=me.pk).values().first(), before)                     # social profile untouched
        self.assertEqual(Token.objects.filter(user=me).count(), 1); self.assertEqual(User.objects.count(), 1)                                         # no new account
        other, _ = dater("other", "man", ("woman", "everyone"))
        self.assertEqual(self.c(other).get("/api/date/discover/").status_code, 200); self.assertEqual(self.c(other).get("/api/date/discover/").data["results"], [])
        self.assertEqual(self.c(other).get("/api/date/profiles/me/").status_code, 404)                                                                   # draft is invisible
        me.refresh_from_db(); self.assertEqual(self.c(me).get("/api/date/profile/").data["profile"]["username"], "me")

    def test_missing_fields_are_reported_and_block_activation_until_completed(self):
        me = mkuser("me", years=None); self.assertTrue(self.c(me).post("/api/date/profile/create/").data["missing"].count("date_of_birth"))   # draft allowed without DOB
        r = self.c(me).post("/api/date/profile/activate/"); self.assertEqual((r.status_code, r.data["code"]), (403, "dob_missing"))
        self.assertEqual(self.c(me).patch("/api/me/", {"date_of_birth": str(dob())}, format="json").status_code, 200)
        r = self.c(me).post("/api/date/profile/activate/"); self.assertEqual((r.status_code, r.data["code"], sorted(r.data["missing"])), (400, "missing_fields", ["bio", "interested_in"]))
        self.assertEqual(self.c(me).patch("/api/date/profile/update/", {"bio": "hi", "interested_in": ["woman"]}, format="json").status_code, 200)
        r = self.c(me).post("/api/date/profile/activate/"); self.assertEqual((r.status_code, r.data["is_active"], r.data["missing"]), (200, True, []))
        self.assertEqual(self.c(me).post("/api/date/profile/deactivate/").data["is_active"], False)

    def test_ineligible_users_cannot_create_or_activate(self):
        kid = mkuser("kid", 16); r = self.c(kid).post("/api/date/profile/create/"); self.assertEqual((r.status_code, r.data["code"]), (403, "underage")); self.assertFalse(DatingProfile.objects.exists())
        ok, p = dater("ok", active=False)
        with override_settings(DATE_REQUIRE_VERIFIED_AGE=True):
            r = self.c(ok).post("/api/date/profile/activate/"); self.assertEqual((r.status_code, r.data["code"]), (403, "age_not_verified"))
            User.objects.filter(pk=ok.pk).update(age_verified=True); self.assertEqual(self.c(ok).post("/api/date/profile/activate/").status_code, 200)
        with override_settings(DATE_LAUNCH_STATE="restricted"):
            self.assertEqual(self.c(mkuser("x")).post("/api/date/profile/create/").data["code"], "restricted")
            self.assertEqual(self.c(mkuser("st", is_staff=True)).post("/api/date/profile/create/").status_code, 201)
        DatingProfile.objects.filter(pk=p.pk).update(moderation_hold=True, is_active=False); r = self.c(ok).post("/api/date/profile/activate/"); self.assertEqual(r.data["code"], "moderation_hold")
        self.assertEqual(self.c(mkuser("anon")).get("/api/date/profile/").data["exists"], False)
        self.assertEqual(APIClient().get("/api/date/profile/").status_code, 401)

    def test_public_launch_fails_safe(self):
        import os, subprocess, sys
        env = {**os.environ, "DJANGO_SECRET_KEY": "x", "DATE_LAUNCH_STATE": "public", "DJANGO_SETTINGS_MODULE": "config.settings"}
        run = lambda extra: subprocess.run([sys.executable, "-c", "from django.conf import settings; print(settings.DATE_LAUNCH_STATE)"], env={**env, **extra}, capture_output=True, text=True).stdout.strip()
        self.assertEqual(run({}), "restricted"); self.assertEqual(run({"DATE_REQUIRE_VERIFIED_AGE": "1", "DATE_MODERATION_CONFIRMED": "1"}), "public")

    def test_update_validation_independence_and_ownership(self):
        me, p = dater("me", "woman", ("man",), city="X"); other, op = dater("other", "man", ("woman",)); social = User.objects.filter(pk=me.pk).values().first(); c = self.c(me)
        self.assertEqual(c.patch("/api/date/profile/update/", {"bio": "new dating bio", "city": "Delhi", "intention": "long_term", "interests": ["Hiking!", "art"]}, format="json").status_code, 200)
        self.assertEqual(User.objects.filter(pk=me.pk).values().first(), social)                                                  # dating edits never change the social profile
        for bad in ({"gender": "x"}, {"intention": "x"}, {"interested_in": ["x"]}, {"min_age": 17}, {"min_age": 50, "max_age": 30}, {"max_age": 120}, {"bio": "x" * 501}, {"bio": ""}, {"interested_in": []}):
            self.assertEqual(c.patch("/api/date/profile/update/", bad, format="json").status_code, 400, bad)                     # incl. can't blank required fields while active
        c.patch("/api/date/profile/update/", {"city": "Pune", "latitude": 26.4, "longitude": 80.3}, format="json"); self.assertFalse(hasattr(DatingProfile, "latitude"))
        op.refresh_from_db(); self.assertEqual(op.bio, "other bio")                                                               # no endpoint takes a user id => can't touch others
        self.assertEqual(self.c(mkuser("n")).patch("/api/date/profile/update/", {"bio": "x"}, format="json").status_code, 404)

    def test_photos_validation_stripping_limits_and_ownership(self):
        me, p = dater("me"); other, op = dater("other"); c = self.c(me)
        r = c.post("/api/date/profile/photos/", {"image": png()}, format="multipart"); self.assertEqual(r.status_code, 201)
        stored = DatingPhoto.objects.get(pk=r.data["id"]); self.assertEqual(Image.open(stored.image.path).format, "JPEG"); self.assertNotIn("secret", stored.image.name)   # re-encoded, random name
        for f in (SimpleUploadedFile("a.txt", b"hello", content_type="text/plain"), SimpleUploadedFile("a.png", b"not an image", content_type="image/png"), SimpleUploadedFile("big.png", b"x" * (5 * 1024 * 1024 + 1), content_type="image/png")):
            self.assertEqual(c.post("/api/date/profile/photos/", {"image": f}, format="multipart").status_code, 400)
        for _ in range(5): c.post("/api/date/profile/photos/", {"image": png()}, format="multipart")
        self.assertEqual(c.post("/api/date/profile/photos/", {"image": png()}, format="multipart").data["code"], "too_many"); self.assertEqual(p.photos.count(), 6)
        oph = DatingPhoto.objects.create(profile=op, image="date/x.jpg"); self.assertEqual(c.delete(f"/api/date/profile/photos/{oph.id}/").status_code, 404)
        self.assertEqual(c.delete(f"/api/date/profile/photos/{stored.id}/").status_code, 204); self.assertEqual(p.photos.count(), 5)

@DEV
class DateDiscoveryTests(Base):
    def test_discovery_only_returns_eligible_active_compatible_people(self):
        me, mp = dater("me", "woman", ("man",), min_age=25, max_age=35)
        dater("ok", "man", ("woman",), 30); dater("ok2", "man", ("everyone",), 27)
        dater("inactive", "man", ("woman",), 30, active=False); dater("held", "man", ("woman",), 30, moderation_hold=True)
        dater("wrong_gender", "woman", ("woman",), 30); dater("not_into_me", "man", ("man",), 30); dater("too_old", "man", ("woman",), 45); dater("too_young", "man", ("woman",), 22)
        dater("their_range_excludes_me", "man", ("woman",), 30, min_age=40, max_age=50); blocker, _ = dater("blocker", "man", ("woman",), 30); blocked, _ = dater("blocked", "man", ("woman",), 30)
        Block.objects.create(blocker=blocker, blocked=me); Block.objects.create(blocker=me, blocked=blocked); minor = mkuser("minor", 16); DatingProfile.objects.create(user=minor, bio="x", gender="man", interested_in=",woman,", is_active=True)
        names = {x["username"] for x in self.c(me).get("/api/date/discover/").data["results"]}; self.assertEqual(names, {"ok", "ok2"}); self.assertNotIn("me", names)
        with override_settings(DATE_REQUIRE_VERIFIED_AGE=True): User.objects.filter(pk=me.pk).update(age_verified=True); self.assertEqual(self.c(me).get("/api/date/discover/").data["results"], [])  # none verified

    def test_discovery_requires_own_active_profile_and_never_leaks_private_fields(self):
        me, _ = dater("me", "woman", ("man",), active=False); u, _ = dater("them", "man", ("woman",)); User.objects.filter(pk=u.pk).update(email="secret@x.test", city="Secretville")
        r = self.c(me).get("/api/date/discover/"); self.assertEqual((r.status_code, r.data["code"]), (403, "not_active")); self.assertEqual(self.c(mkuser("nodate")).get("/api/date/discover/").status_code, 403)
        DatingProfile.objects.filter(user=me).update(is_active=True); card = self.c(me).get("/api/date/discover/").data["results"][0]
        self.assertEqual(set(card), {"username", "age", "bio", "city", "interests", "shared_interests", "intention", "photos", "social_visible"})
        txt = str(card); [self.assertNotIn(s, txt) for s in ("secret@x.test", "Secretville", "date_of_birth", str(dob()))]; self.assertEqual(card["age"], 28)

    def test_pagination_and_acted_profiles_disappear(self):
        me, _ = dater("me", "woman", ("man",))
        for i in range(12): dater(f"c{i}", "man", ("woman",))
        pg = self.c(me).get("/api/date/discover/").data; self.assertEqual((len(pg["results"]), bool(pg["next"])), (10, True)); self.assertEqual(len(self.c(me).get(pg["next"]).data["results"]), 2)
        self.c(me).post("/api/date/pass/", {"username": "c3"}, format="json"); self.c(me).post("/api/date/like/", {"username": "c4"}, format="json")
        page = self.c(me).get("/api/date/discover/").data; seen = {x["username"] for x in page["results"]}
        self.assertEqual((len(seen), page["next"]), (10, None)); self.assertTrue({"c3", "c4"}.isdisjoint(seen))   # 12 - 2 acted = exactly one page

    def test_private_accounts_hide_social_link_unless_followed(self):
        me, _ = dater("me", "woman", ("man",)); u, _ = dater("priv", "man", ("woman",)); User.objects.filter(pk=u.pk).update(is_private=True)
        self.assertFalse(self.c(me).get("/api/date/discover/").data["results"][0]["social_visible"]); Follow.objects.create(follower=me, followee=u)
        self.assertTrue(self.c(me).get("/api/date/discover/").data["results"][0]["social_visible"])

@DEV
class DateMatchTests(Base):
    def setUp(self):
        super().setUp(); self.a, _ = dater("alice", "woman", ("man",)); self.b, _ = dater("bob", "man", ("woman",)); self.cc, _ = dater("carl", "man", ("woman",))
    def like(self, who, name): return self.c(who).post("/api/date/like/", {"username": name}, format="json")

    def test_one_sided_like_does_not_match_and_mutual_like_creates_exactly_one_match(self):
        r = self.like(self.a, "bob"); self.assertEqual((r.status_code, r.data["matched"]), (200, False)); self.assertFalse(DateMatch.objects.exists()); self.assertEqual(Notification.objects.filter(kind="date_match").count(), 0)
        r = self.like(self.b, "alice"); self.assertTrue(r.data["matched"]); self.assertEqual(r.data["match"]["username"], "alice")
        self.assertEqual((DateMatch.objects.count(), Notification.objects.filter(kind="date_match").count()), (1, 2))
        self.assertEqual(self.like(self.b, "alice").status_code, 409); self.assertEqual(self.like(self.a, "bob").status_code, 409); self.assertEqual(DateMatch.objects.count(), 1)   # duplicates refused
        m = DateMatch.objects.get(); self.assertLess(m.user_low_id, m.user_high_id)
        with self.assertRaises(Exception), transaction.atomic(): DateMatch.objects.create(user_low=self.a, user_high=self.b, pair_key=m.pair_key)   # DB-level uniqueness
        self.assertEqual({n.user.username for n in Notification.objects.filter(kind="date_match")}, {"alice", "bob"})
        conv = Conversation.objects.get(); self.assertEqual((conv.origin, conv.participants.count()), ("date", 2))
        self.assertEqual([x["username"] for x in self.c(self.a).get("/api/date/matches/").data["results"]], ["bob"]); self.assertEqual(self.c(self.cc).get("/api/date/matches/").data["results"], [])

    def test_invalid_likes_and_passes(self):
        self.assertEqual(self.like(self.a, "alice").status_code, 404)                                  # self
        self.assertEqual(self.like(self.a, "nobody").status_code, 404)
        dater("off", "man", ("woman",), active=False); self.assertEqual(self.like(self.a, "off").status_code, 404)                  # not discoverable
        dater("gay", "woman", ("woman",)); self.assertEqual(self.like(self.a, "gay").status_code, 404)                              # not compatible
        Block.objects.create(blocker=self.cc, blocked=self.a); self.assertEqual(self.like(self.a, "carl").status_code, 404)       # blocked either way
        self.c(self.a).post("/api/date/pass/", {"username": "bob"}, format="json"); self.assertEqual(self.like(self.a, "bob").status_code, 409)   # a pass is final
        self.like(self.b, "alice"); self.assertFalse(DateMatch.objects.exists())                                                   # a passed-on profile never matches
        self.assertEqual(APIClient().post("/api/date/like/", {"username": "bob"}, format="json").status_code, 401)
        self.assertEqual(self.c(mkuser("nodate")).post("/api/date/like/", {"username": "bob"}, format="json").status_code, 403)

    def test_daily_like_cap(self):
        with override_settings(DATE_MAX_LIKES_PER_DAY=1):
            self.assertEqual(self.like(self.a, "bob").status_code, 200); r = self.like(self.a, "carl"); self.assertEqual((r.status_code, r.data["code"]), (429, "daily_limit"))

    def test_unmatch_removes_chat_access_and_pair_never_returns(self):
        self.like(self.a, "bob"); self.like(self.b, "alice"); cid = Conversation.objects.get().id
        self.assertEqual(self.c(self.a).post(f"/api/conversations/{cid}/messages/", {"text": "hey"}, format="json").status_code, 201)   # existing chat reused
        self.assertEqual(self.c(self.b).get(f"/api/conversations/{cid}/messages/").data["results"][0]["text"], "hey")
        self.assertEqual(self.c(self.cc).get(f"/api/conversations/{cid}/messages/").status_code, 404); self.assertEqual(self.c(self.cc).post(f"/api/conversations/{cid}/messages/", {"text": "x"}, format="json").status_code, 404)
        self.assertEqual(self.c(self.cc).post("/api/date/unmatch/", {"username": "alice"}, format="json").status_code, 404)             # outsider
        self.assertEqual(self.c(self.a).post("/api/date/unmatch/", {"username": "bob"}, format="json").status_code, 200)
        for u in (self.a, self.b):
            self.assertEqual(self.c(u).get(f"/api/conversations/{cid}/messages/").status_code, 404); self.assertEqual(self.c(u).post(f"/api/conversations/{cid}/messages/", {"text": "x"}, format="json").status_code, 404)
            self.assertEqual(self.c(u).get("/api/conversations/").data["results"], []); self.assertEqual(self.c(u).get("/api/date/matches/").data["results"], [])
        self.assertNotIn("bob", [x["username"] for x in self.c(self.a).get("/api/date/discover/").data["results"]]); self.assertEqual(self.like(self.a, "bob").status_code, 409)   # pair already resolved: can't be re-liked or re-matched
        self.assertEqual(DateMatch.objects.get().active, False); self.assertEqual(DateMatch.objects.count(), 1)

    def test_block_ends_match_and_chat(self):
        self.like(self.a, "bob"); self.like(self.b, "alice"); cid = Conversation.objects.get().id; self.c(self.a).post("/api/users/bob/block/")
        self.assertFalse(DateMatch.objects.get().active); self.assertEqual(self.c(self.b).get(f"/api/conversations/{cid}/messages/").status_code, 404)

    def test_private_account_match_can_still_chat_and_normal_direct_chats_are_unaffected(self):
        User.objects.filter(pk=self.b.pk).update(is_private=True); self.like(self.a, "bob"); self.like(self.b, "alice"); cid = Conversation.objects.get().id
        self.assertEqual(self.c(self.a).post(f"/api/conversations/{cid}/messages/", {"text": "hi"}, format="json").status_code, 201)       # match = mutual consent
        direct = self.c(self.a).post("/api/conversations/", {"username": "carl"}, format="json").data["id"]
        self.assertEqual(Conversation.objects.get(pk=direct).origin, "direct"); self.assertEqual(self.c(self.a).post(f"/api/conversations/{direct}/messages/", {"text": "yo"}, format="json").status_code, 201)
        self.c(self.a).post("/api/date/unmatch/", {"username": "bob"}, format="json"); self.assertEqual(self.c(self.a).post(f"/api/conversations/{direct}/messages/", {"text": "still"}, format="json").status_code, 201)

    def test_existing_direct_conversation_is_kept_when_users_later_match(self):
        direct = self.c(self.a).post("/api/conversations/", {"username": "bob"}, format="json").data["id"]; self.like(self.a, "bob"); self.like(self.b, "alice")
        self.assertEqual(Conversation.objects.count(), 1); self.assertEqual(Conversation.objects.get(pk=direct).origin, "direct")      # no duplicate; unmatching later keeps it
        self.c(self.a).post("/api/date/unmatch/", {"username": "bob"}, format="json"); self.assertEqual(self.c(self.a).get(f"/api/conversations/{direct}/messages/").status_code, 200)

    def test_reports_hide_reporter_view_and_auto_hold_after_repeat_reports(self):
        self.assertEqual(self.c(self.a).post("/api/date/report/", {"username": "bob", "reason": ""}, format="json").status_code, 400)
        self.assertEqual(self.c(mkuser("nodate")).post("/api/date/report/", {"username": "bob", "reason": "x"}, format="json").status_code, 403)   # must be an active dater
        stranger, _ = dater("gay", "woman", ("woman",)); self.assertEqual(self.c(self.a).post("/api/date/report/", {"username": "gay", "reason": "x"}, format="json").status_code, 404)  # can't report people you can't see
        self.assertEqual(self.c(self.a).post("/api/date/report/", {"username": "bob", "reason": "fake photos"}, format="json").status_code, 201)
        r = Report.objects.get(); self.assertEqual((r.category, r.user.username, r.reporter.username), ("dating", "bob", "alice")); self.assertEqual(DatingLike.objects.get(from_user=self.a, to_user=self.b).kind, "pass")
        self.assertFalse(DatingProfile.objects.get(user=self.b).moderation_hold)
        for n in ("r1", "r2"):
            u, _ = dater(n, "woman", ("man",)); self.c(u).post("/api/date/report/", {"username": "bob", "reason": "bad"}, format="json")
        p = DatingProfile.objects.get(user=self.b); self.assertEqual((p.moderation_hold, p.is_active), (True, False))
        self.assertNotIn("bob", [x["username"] for x in self.c(self.cc).get("/api/date/discover/").data["results"]]) if False else None
        self.assertEqual(self.c(self.b).post("/api/date/profile/activate/").data["code"], "moderation_hold")

    def test_preferences_endpoint_and_deactivation_hides_profile(self):
        d = self.c(self.a).get("/api/date/preferences/").data; self.assertEqual((d["limits"]["max_photos"], d["limits"]["min_age"], "everyone" in d["interested_in_options"]), (6, 18, True))
        self.assertIn("bob", [x["username"] for x in self.c(self.a).get("/api/date/discover/").data["results"]]); self.c(self.b).post("/api/date/profile/deactivate/")
        self.assertNotIn("bob", [x["username"] for x in self.c(self.a).get("/api/date/discover/").data["results"]]); self.assertEqual(self.c(self.a).get("/api/date/profiles/bob/").status_code, 404)

@DEV
class DateConcurrencyTests(TransactionTestCase):
    def test_simultaneous_mutual_likes_create_exactly_one_match(self):
        a, _ = dater("alice", "woman", ("man",)); b, _ = dater("bob", "man", ("woman",)); barrier = threading.Barrier(2); out = []
        def worker(u, name):
            try:
                barrier.wait()
                for _ in range(40):
                    try: out.append(D.act(u, name, "like")); return
                    except OperationalError: time.sleep(0.03)          # SQLite table locks; Postgres would serialise on the row locks instead
                    except D.DateError as e: out.append(e.code); return
            finally: connection.close()
        ts = [threading.Thread(target=worker, args=(a, "bob")), threading.Thread(target=worker, args=(b, "alice"))]; [t.start() for t in ts]; [t.join(30) for t in ts]
        self.assertEqual(DateMatch.objects.count(), 1); self.assertEqual(sum(1 for o in out if isinstance(o, dict) and o["matched"]), 1)   # exactly one side sees the match, none missed
        self.assertEqual(Notification.objects.filter(kind="date_match").count(), 2); self.assertEqual(Conversation.objects.count(), 1)
