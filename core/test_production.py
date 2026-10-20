"""Production-readiness tests: media serving (Range), health/root endpoints, production settings matrix, upload validation, SPA fallback config."""
import json, os, subprocess, sys, tempfile
from pathlib import Path
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.cache import cache
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings
from rest_framework.authtoken.models import Token
from rest_framework.test import APITestCase
from .models import User, Video
from .media import serve_media, sniff_video

MP4 = b"\x00\x00\x00\x18ftypmp42" + bytes(range(256)) * 4
WEBM = b"\x1a\x45\xdf\xa3" + b"\x00" * 64
ROOT = Path(__file__).resolve().parent.parent

def body(resp):
    if not resp.streaming: return resp.content
    from asgiref.sync import async_to_sync
    async def collect(): return b"".join([c async for c in resp.streaming_content]) if resp.is_async else b"".join(resp.streaming_content)
    return async_to_sync(collect)()

class MediaServingTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(); (Path(self.tmp) / "videos").mkdir(); (Path(self.tmp) / "videos" / "v.mp4").write_bytes(MP4); self.o = override_settings(MEDIA_ROOT=self.tmp); self.o.enable()
    def tearDown(self): self.o.disable()
    def test_full_file_has_correct_type_and_advertises_ranges(self):
        r = self.client.get("/media/videos/v.mp4"); self.assertEqual((r.status_code, r["Content-Type"], r["Accept-Ranges"], int(r["Content-Length"])), (200, "video/mp4", "bytes", len(MP4))); self.assertEqual(body(r), MP4)
    def test_range_requests_return_206_with_exact_bytes(self):   # required for seeking and for Safari to play video at all
        r = self.client.get("/media/videos/v.mp4", headers={"Range": "bytes=0-9"}); self.assertEqual((r.status_code, r["Content-Range"], body(r)), (206, f"bytes 0-9/{len(MP4)}", MP4[:10]))
        r = self.client.get("/media/videos/v.mp4", headers={"Range": "bytes=100-"}); self.assertEqual((r.status_code, body(r)), (206, MP4[100:]))
        r = self.client.get("/media/videos/v.mp4", headers={"Range": "bytes=-20"}); self.assertEqual((r.status_code, body(r)), (206, MP4[-20:]))
        r = self.client.get("/media/videos/v.mp4", headers={"Range": f"bytes={len(MP4)}-"}); self.assertEqual((r.status_code, r["Content-Range"]), (416, f"bytes */{len(MP4)}"))
    def test_head_missing_post_and_path_traversal(self):
        r = self.client.head("/media/videos/v.mp4"); self.assertEqual((r.status_code, int(r["Content-Length"])), (200, len(MP4)))
        self.assertEqual(self.client.get("/media/videos/nope.mp4").status_code, 404); self.assertEqual(self.client.post("/media/videos/v.mp4").status_code, 405)
        (Path(self.tmp).parent / "secret.txt").write_text("secret")
        from asgiref.sync import async_to_sync
        from django.http import Http404
        for evil in ("../secret.txt", "videos/../../secret.txt", "/etc/passwd"):
            with self.assertRaises(Http404): async_to_sync(serve_media)(RequestFactory().get("/media/x"), evil)

class HealthTests(TestCase):
    def test_root_and_health_are_public_json_without_secrets(self):
        r = self.client.get("/"); self.assertEqual((r.status_code, r.json()["service"]), (200, "zingo-api"))
        r = self.client.get("/healthz/"); d = r.json(); self.assertEqual((r.status_code, d["status"], d["database"], d["storage"]), (200, "ok", "ok", "local"))
        txt = json.dumps(d).lower(); [self.assertNotIn(w, txt) for w in ("secret", "password", "access_key", "token")]
        self.assertEqual(self.client.get("/definitely/not/a/route/").status_code, 404)   # unknown URLs are still a real 404 (nothing is swallowed)

class UploadValidationTests(APITestCase):
    def setUp(self):
        cache.clear(); u = User.objects.create_user("up", password="pass12345"); self.client.credentials(HTTP_AUTHORIZATION="Token " + Token.objects.create(user=u).key); self.o = override_settings(MEDIA_ROOT=tempfile.mkdtemp()); self.o.enable()
    def tearDown(self): self.o.disable()
    def up(self, name, data, ct): return self.client.post("/api/videos/", {"file": SimpleUploadedFile(name, data, content_type=ct), "caption": "c"}, format="multipart")
    def test_real_videos_are_accepted_and_urls_point_at_the_media_route(self):
        r = self.up("a.mp4", MP4, "video/mp4"); self.assertEqual(r.status_code, 201); self.assertRegex(r.data["file"], r"^http://testserver/media/videos/[0-9a-f]{32}\.mp4$")
        self.assertEqual(self.client.get(r.data["file"].replace("http://testserver", "")).status_code, 200)       # the URL the API hands out actually serves the file
        self.assertEqual(self.up("a.webm", WEBM, "video/webm").status_code, 201); self.assertEqual(self.up("a.mov", b"\x00\x00\x00\x14ftypqt  " + b"\x00" * 32, "video/quicktime").status_code, 201)
    def test_fake_or_unsupported_uploads_are_rejected_with_clear_errors(self):
        self.assertIn("valid", str(self.up("a.mp4", b"this is not a video at all", "video/mp4").data))                # lying content-type
        self.assertEqual(self.up("a.mp4", b"hello", "text/plain").status_code, 400)
        self.assertIn("Unsupported", str(self.up("a.exe", MP4, "video/mp4").data)); self.assertEqual(self.up("a.mp4", b"MZ\x90\x00" + b"\x00" * 64, "video/mp4").status_code, 400)
        with override_settings(MAX_VIDEO_MB=0): self.assertIn("too large", str(self.up("a.mp4", MP4, "video/mp4").data))
        self.assertEqual(Video.objects.count(), 0)
        self.client.credentials(); self.assertEqual(self.up("a.mp4", MP4, "video/mp4").status_code, 401)
    def test_sniffer(self): self.assertEqual((sniff_video(MP4), sniff_video(WEBM), sniff_video(b"x" * 20), sniff_video(b"")), ("mp4", "webm", None, None))

class ProductionSettingsMatrix(SimpleTestCase):
    """Loads the real settings module in a clean process under production-like environments."""
    BASE = {"PATH": os.environ["PATH"], "DJANGO_SETTINGS_MODULE": "config.settings", "PYTHONPATH": str(ROOT)}
    def cfg(self, env, expr):
        r = subprocess.run([sys.executable, "-c", f"from django.conf import settings as s; import json; print(json.dumps({expr}))"], env={**self.BASE, **env}, capture_output=True, text=True, cwd=ROOT)
        return json.loads(r.stdout) if r.returncode == 0 else r.stderr
    def test_refuses_to_start_without_a_secret_key(self): self.assertIn("DJANGO_SECRET_KEY", self.cfg({}, "s.DEBUG"))
    def test_render_production_profile(self):
        d = self.cfg({"DJANGO_SECRET_KEY": "k" * 50, "RENDER": "true", "RENDER_EXTERNAL_HOSTNAME": "api.example.onrender.com", "CORS_ALLOWED_ORIGINS": "https://app.example.onrender.com"},
                     "{'debug': s.DEBUG, 'hosts': s.ALLOWED_HOSTS, 'cors': s.CORS_ALLOWED_ORIGINS, 'creds': s.CORS_ALLOW_CREDENTIALS, 'ssl': s.SECURE_SSL_REDIRECT, 'cookie': s.SESSION_COOKIE_SECURE, 'proxy': s.SECURE_PROXY_SSL_HEADER, 'csrf': s.CSRF_TRUSTED_ORIGINS, 'launch': [s.MATCH_LAUNCH_STATE, s.DATE_LAUNCH_STATE], 'db': s.DATABASES['default']['ENGINE']}")
        self.assertEqual((d["debug"], d["cors"], d["creds"], d["ssl"], d["cookie"]), (False, ["https://app.example.onrender.com"], False, True, True)); self.assertIn("api.example.onrender.com", d["hosts"]); self.assertNotIn("*", d["hosts"] + d["cors"])
        self.assertEqual((d["proxy"], d["csrf"], d["launch"]), (["HTTP_X_FORWARDED_PROTO", "https"], ["https://api.example.onrender.com"], ["restricted", "restricted"])); self.assertTrue(d["db"].endswith("sqlite3"))
    def test_launch_gates_fail_safe_and_self_declared_is_an_explicit_opt_in(self):
        base = {"DJANGO_SECRET_KEY": "k"}; expr = "[s.MATCH_LAUNCH_STATE, s.DATE_LAUNCH_STATE]"
        self.assertEqual(self.cfg({**base, "MATCH_LAUNCH_STATE": "self_declared", "DATE_LAUNCH_STATE": "self_declared"}, expr), ["self_declared", "self_declared"])
        self.assertEqual(self.cfg({**base, "MATCH_LAUNCH_STATE": "open-to-all", "DATE_LAUNCH_STATE": "PUBLIC"}, expr), ["restricted", "restricted"])      # typos/unknown => staff only
        self.assertEqual(self.cfg({**base, "MATCH_LAUNCH_STATE": "public", "DATE_LAUNCH_STATE": "public"}, expr), ["restricted", "restricted"])          # public without age verification + moderation
        self.assertEqual(self.cfg(base, expr), ["restricted", "restricted"])                                                                              # default outside the dev server
    def test_debug_can_not_be_switched_on_by_the_dev_server_on_render(self):
        self.assertFalse(self.cfg({"DJANGO_SECRET_KEY": "k", "RENDER": "true"}, "s.DEBUG"))
    def test_database_url_selects_postgres_and_s3_env_selects_signed_s3_media(self):
        d = self.cfg({"DJANGO_SECRET_KEY": "k", "DATABASE_URL": "postgres://u:p@db.example.com:5432/zingo", "AWS_STORAGE_BUCKET_NAME": "zingo-media", "AWS_ACCESS_KEY_ID": "id", "AWS_SECRET_ACCESS_KEY": "sec", "AWS_S3_ENDPOINT_URL": "https://acct.r2.cloudflarestorage.com", "AWS_S3_REGION_NAME": "auto"},
                     "{'db': s.DATABASES['default']['ENGINE'], 'host': s.DATABASES['default']['HOST'], 'media': s.STORAGES['default']['BACKEND'], 'o': {k: v for k, v in s.STORAGES['default']['OPTIONS'].items() if k not in ('access_key', 'secret_key')}, 's3': s.USES_S3}")
        self.assertEqual((d["db"], d["host"], d["media"], d["s3"]), ("django.db.backends.postgresql", "db.example.com", "storages.backends.s3.S3Storage", True))
        self.assertEqual((d["o"]["bucket_name"], d["o"]["querystring_auth"], d["o"]["endpoint_url"], d["o"]["file_overwrite"]), ("zingo-media", True, "https://acct.r2.cloudflarestorage.com", False))   # private bucket, signed URLs
    def test_public_media_option_and_dotenv_inline_comments(self):
        d = self.cfg({"DJANGO_SECRET_KEY": "k", "AWS_STORAGE_BUCKET_NAME": "b", "MEDIA_PUBLIC_READ": "1"}, "s.STORAGES['default']['OPTIONS']['querystring_auth']"); self.assertFalse(d)
        tmp = Path(tempfile.mkdtemp()); (tmp / "m.py").write_text("")
        env_file = ROOT / ".env"; existed = env_file.exists()
        if not existed:
            env_file.write_text('DJANGO_SECRET_KEY=abc   # trailing comment\nCORS_ALLOWED_ORIGINS="https://x.example"  # q\n')
            try: self.assertEqual(self.cfg({}, "[s.SECRET_KEY, s.CORS_ALLOWED_ORIGINS]"), ["abc", ["https://x.example"]])
            finally: env_file.unlink()

class SpaFallbackConfigTests(SimpleTestCase):
    def test_static_host_rewrites_every_path_to_index_html(self):
        y = (ROOT / "render.yaml").read_text(); front = y[y.index("name: zingo-frontend"):]
        self.assertIn("type: rewrite", front); self.assertIn("source: /*", front); self.assertIn("destination: /index.html", front); self.assertIn("staticPublishPath: dist", front)
        if (ROOT / "frontend").exists(): self.assertIn("/index.html", (ROOT / "frontend/public/_redirects").read_text())
        self.assertIn("VITE_API_URL", front); self.assertNotIn("localhost", y)                                   # no localhost in deployment config
    def test_api_is_not_swallowed_by_the_fallback(self):   # the rewrite lives on the static frontend host only; the Django API host has no catch-all route
        from django.urls import Resolver404, resolve
        for p in ("/api/does-not-exist/", "/anything/else/"):
            with self.assertRaises(Resolver404): resolve(p)
