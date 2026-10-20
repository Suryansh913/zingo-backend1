"""End-to-end media storage test against a REAL S3-compatible HTTP server (moto). Proves: uploads land in the bucket, the URL the API returns is
a working absolute URL, HTTP Range playback works from the bucket, privacy (signed URLs) is configured, and the repair commands behave.
It does NOT prove anything about your actual AWS/R2/B2 account - see DEPLOY.md for the live verification checklist."""
import io, socket, tempfile, unittest
from datetime import date
from pathlib import Path
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import override_settings
from rest_framework.authtoken.models import Token
from rest_framework.test import APITestCase
try:
    import boto3, requests
    from moto.server import ThreadedMotoServer
    HAVE = True
except ImportError: HAVE = False
from PIL import Image
from .models import DatingPhoto, User, Video

MP4 = b"\x00\x00\x00\x18ftypmp42" + bytes(range(256)) * 40   # ~10 KB

def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p

@unittest.skipUnless(HAVE, "moto/boto3/requests not installed")
class S3MediaEndToEnd(APITestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass(); cls.port = free_port(); cls.server = ThreadedMotoServer(ip_address="127.0.0.1", port=cls.port, verbose=False); cls.server.start()
        cls.endpoint = f"http://127.0.0.1:{cls.port}"
        cls.s3 = boto3.client("s3", endpoint_url=cls.endpoint, aws_access_key_id="test", aws_secret_access_key="test", region_name="us-east-1"); cls.s3.create_bucket(Bucket="zingo-test")
        cls.storages = {"default": {"BACKEND": "storages.backends.s3.S3Storage", "OPTIONS": {"bucket_name": "zingo-test", "access_key": "test", "secret_key": "test", "endpoint_url": cls.endpoint, "region_name": "us-east-1",
                        "signature_version": "s3v4", "addressing_style": "path", "querystring_auth": True, "querystring_expire": 3600, "file_overwrite": False, "default_acl": None}},
                        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}}
    @classmethod
    def tearDownClass(cls): cls.server.stop(); super().tearDownClass()
    def setUp(self):
        cache.clear()
        for o in self.s3.list_objects_v2(Bucket="zingo-test").get("Contents", []): self.s3.delete_object(Bucket="zingo-test", Key=o["Key"])   # isolate tests: empty bucket
        self.o = override_settings(STORAGES=self.storages, USES_S3=True, MEDIA_PUBLIC_READ=False, DATE_LAUNCH_STATE="development"); self.o.enable()
        self.u = User.objects.create_user("s3user", password="pass12345", date_of_birth=date(1995, 1, 1)); self.client.credentials(HTTP_AUTHORIZATION="Token " + Token.objects.create(user=self.u).key)
    def tearDown(self): self.o.disable()
    def keys(self): return [o["Key"] for o in self.s3.list_objects_v2(Bucket="zingo-test").get("Contents", [])]

    def test_upload_goes_to_bucket_and_playback_url_works_with_range_requests(self):
        r = self.client.post("/api/videos/", {"file": SimpleUploadedFile("clip.mp4", MP4, content_type="video/mp4"), "caption": "hi"}, format="multipart"); self.assertEqual(r.status_code, 201, r.data)
        url = r.data["file"]; self.assertTrue(url.startswith(self.endpoint + "/zingo-test/videos/"), url); self.assertIn("X-Amz-Signature", url)           # absolute, bucket-hosted, signed
        key = Video.objects.get().file.name; self.assertIn(key, self.keys()); self.assertRegex(key, r"^videos/[0-9a-f]{32}\.mp4$")
        full = requests.get(url); self.assertEqual((full.status_code, full.headers["Content-Type"], full.content), (200, "video/mp4", MP4))
        part = requests.get(url, headers={"Range": "bytes=0-9"}); self.assertEqual((part.status_code, part.content, part.headers["Content-Range"]), (206, MP4[:10], f"bytes 0-9/{len(MP4)}"))   # what <video> seeking needs
        self.assertEqual(self.client.get(f"/api/videos/{r.data['id']}/").data["file"].split("?")[0], url.split("?")[0])                          # detail endpoint returns the same object (fresh signature)
        self.assertEqual(self.client.get("/healthz/").json()["storage"], "s3")

    def test_same_filename_never_overwrites_another_upload(self):
        for _ in range(2): self.client.post("/api/videos/", {"file": SimpleUploadedFile("clip.mp4", MP4, content_type="video/mp4")}, format="multipart")
        self.assertEqual(len({v.file.name for v in Video.objects.all()}), 2); self.assertEqual(len(self.keys()), 2)

    def test_dating_photos_use_the_same_private_storage(self):
        self.assertEqual(self.client.post("/api/date/profile/create/").status_code, 201)
        b = io.BytesIO(); Image.new("RGB", (60, 80), "pink").save(b, "PNG")
        r = self.client.post("/api/date/profile/photos/", {"image": SimpleUploadedFile("me.png", b.getvalue(), content_type="image/png")}, format="multipart"); self.assertEqual(r.status_code, 201)
        self.assertTrue(r.data["url"].startswith(self.endpoint + "/zingo-test/date/") and "X-Amz-Signature" in r.data["url"]); got = requests.get(r.data["url"]); self.assertEqual((got.status_code, got.headers["Content-Type"]), (200, "image/jpeg"))

    def test_repair_commands_report_lost_files_honestly_and_restore_recoverable_ones(self):
        r = self.client.post("/api/videos/", {"file": SimpleUploadedFile("c.mp4", MP4, content_type="video/mp4")}, format="multipart"); v = Video.objects.get(pk=r.data["id"]); name = v.file.name
        out = io.StringIO(); call_command("check_media", stdout=out); self.assertIn("missing in storage: 0", out.getvalue())
        self.s3.delete_object(Bucket="zingo-test", Key=name)                                                                          # simulate a lost file (what happened to old Render-disk uploads)
        out = io.StringIO(); call_command("check_media", "--list", stdout=out); self.assertIn("missing in storage: 1", out.getvalue()); self.assertIn(f"MISSING video #{v.pk}", out.getvalue())
        out = io.StringIO(); call_command("check_media", "--hide-missing", stdout=out, stderr=io.StringIO()); v.refresh_from_db(); self.assertEqual(v.status, "active")   # refuses without --yes: nothing changes
        src = Path(tempfile.mkdtemp()); (src / "videos").mkdir(); (src / name).write_bytes(MP4)                                       # the file still exists on an old local disk
        out = io.StringIO(); call_command("sync_media_to_storage", "--source", str(src), "--dry-run", stdout=out); self.assertIn("would upload: 1", out.getvalue()); self.assertNotIn(name, self.keys())
        out = io.StringIO(); call_command("sync_media_to_storage", "--source", str(src), stdout=out); self.assertIn("uploaded: 1", out.getvalue()); self.assertIn(name, self.keys())
        out = io.StringIO(); call_command("check_media", stdout=out); self.assertIn("missing in storage: 0", out.getvalue())
        Video.objects.create(owner=self.u, file="videos/never-existed.mp4"); out = io.StringIO(); call_command("check_media", "--hide-missing", "--yes", stdout=out)   # unrecoverable record
        self.assertEqual(Video.objects.get(file="videos/never-existed.mp4").status, "removed"); self.assertEqual(Video.objects.get(pk=v.pk).status, "active")
