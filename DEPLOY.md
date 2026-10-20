# ZINGO — Production deployment, 404 diagnosis, backup & verification

Live URLs: frontend `https://zingo-frontend.onrender.com` · backend `https://zingo-backend-o11v.onrender.com`

> **What was and wasn't verified.** Everything below marked ✅ was run in a sandbox against this code (real HTTP server, real WebSockets,
> a real S3-compatible server). **I had no access to your Render account or live site**, so nothing was verified on
> `onrender.com` itself. Section 9 is the checklist to prove it live.

---

## 1. Root causes found (and how each was reproduced)

| # | Symptom you see | Root cause (in the code) | Evidence | Fix |
|---|---|---|---|---|
| A | **Uploaded Zings don't play after deploy** (video URL → 404) | `config/urls.py` served `/media/` with Django's `static()` helper, which **only works when `DEBUG=True`**. In production it returns nothing → every media URL 404s. Also the Render disk is **ephemeral**, so even served files vanish on each deploy. | ✅ Reproduced: with `DJANGO_DEBUG=0` the file existed on disk but `GET /media/videos/…mp4` → **404**; with debug on → 200. | Media now goes to **S3-compatible storage** (private bucket + signed URLs), with a Range-capable local fallback. |
| B | **"Not Found" when you refresh or open a shared link** like `/zings`, `/u/name`, `/date` | A React Router SPA served from a **static host with no rewrite rule**: the host looks for a file named `/zings`, finds none, returns 404. Only `/` works. | ✅ Reproduced on a static server: `/` 200, `/zings` `/date` `/u/x` `/video/1` all **404**. | `render.yaml` rewrite `/* → /index.html`, `frontend/public/_redirects`, and a real in-app 404 page. **You must also add the rule in the Render dashboard (Section 4).** |
| C | **Every API call 404s** after deploy | If `VITE_API_URL` is set with a **trailing slash** (`…onrender.com/`) or ends in **`/api`**, requests become `//api/…` or `/api/api/…`. Django has no such route. | ✅ Reproduced on a real server: `//api/match/eligibility/` → 404, `/api/api/match/eligibility/` → 404. | The frontend now normalizes the base URL (14 unit tests) and shows a red banner if it is missing/wrong instead of calling the wrong host. |
| D | Opening the **backend URL itself** shows "Not Found" | Django had no route for `/`. | ✅ Reproduced. | `/` returns a small JSON banner; `/healthz/` is the health check. Unknown URLs are still real 404s. |
| E | Backend admin pages unstyled / app falls over in production | No `STATIC_ROOT`/WhiteNoise, `DEBUG` defaulted **on**, no `DATABASE_URL`, dev `SECRET_KEY` fallback, localhost-only CORS & hosts. | ✅ Reproduced (admin CSS 404). | `settings.py` rewritten: secure by default, WhiteNoise, Postgres via `DATABASE_URL`, hard error if no secret key. |
| F | **Matching "Under Development"** | **No such message exists anywhere in this repository** (verified by a test that fails if placeholder text is added). ZINGO Match and ZINGO Date are fully implemented here. If your live site shows it, the live site is running an **older commit** — check `/status` → "Backend release". | ✅ grep + static test. | Deploy this version. In production the features are gated (Section 6). |

**Not a cause:** the frontend→backend API contract. ✅ A test parses all **76** API calls in the React code (61 distinct paths) and checks each resolves in Django with an allowed HTTP method — all do. So there is no route/method mismatch to fix; the 404s are deployment-configuration problems above.

---

## 2. Architecture you need

```
Browser ──► zingo-frontend (Render Static Site, React build)
   │                       │  VITE_API_URL
   └──► zingo-backend (Render Web Service, Daphne: HTTP + WebSocket)
                │──► Postgres (DATABASE_URL)            ← persistent data
                └──► S3-compatible bucket (AWS_*)       ← persistent videos/photos
```
Render's free web-service disk and free SQLite are wiped on every deploy/restart. **Postgres + a bucket are required** for anything users create to survive.

---

## 3. Safe order of operations (do this in order)

### 3.1 Backup first (before the new code touches any database)
* **If your backend already uses Render Postgres:** Dashboard → your database → *Backups* → create a backup/export, or run
  `pg_dump "$EXTERNAL_DATABASE_URL" -Fc -f zingo_backup_$(date +%F).dump`.
* **If it has been using SQLite on Render** (check `https://zingo-backend-o11v.onrender.com/healthz/` → `database_engine`; a *new* build shows it, an old one returns 404): that file lives on the ephemeral disk and was already being lost on every deploy. There is nothing reliable to back up; start fresh on Postgres.
* This release's migrations are **additive** (new tables/columns with defaults; nothing is dropped or rewritten).
* Rollback: redeploy the previous commit. To also undo schema changes: `python manage.py migrate core <previous_migration_number>` (new tables' data would be lost; old tables untouched).

### 3.2 Create the media bucket (S3-compatible). Example: Cloudflare R2 (10 GB free, no egress fees)
1. Cloudflare → R2 → *Create bucket* (e.g. `zingo-media`). **Keep it private** (do not enable public access).
2. R2 → *Manage API tokens* → *Create token* with **Object Read & Write** on that bucket. Note **Access Key ID**, **Secret Access Key**, and the **S3 endpoint** `https://<ACCOUNT_ID>.r2.cloudflarestorage.com`.
3. Env values: `AWS_STORAGE_BUCKET_NAME=zingo-media`, `AWS_S3_ENDPOINT_URL=<endpoint>`, `AWS_S3_REGION_NAME=auto`.
* AWS S3 instead: create a private bucket + an IAM user with `s3:PutObject/GetObject/DeleteObject/ListBucket` on it; leave `AWS_S3_ENDPOINT_URL` empty and set `AWS_S3_REGION_NAME` (e.g. `ap-south-1`). Backblaze B2 / Supabase Storage also work (use their S3 endpoint).
* Keys live **only** in Render env vars — never in React or Git.

### 3.3 Create Postgres on Render
New → PostgreSQL → copy the **Internal Database URL** (use the *internal* one for a backend in the same region). Note: Render's free Postgres expires after 30 days — use a paid plan for real users.

### 3.4 Backend service (`zingo-backend-o11v`) — Settings
| Setting | Value |
|---|---|
| Build Command | `pip install -r requirements.txt && python manage.py collectstatic --noinput` |
| Start Command | `bash start.sh` |
| Health Check Path | `/healthz/` |
| Python | env `PYTHON_VERSION=3.12.7` (Django 6 needs ≥ 3.12) |

**Environment variables** (values are yours; names only here):

| Variable | Required | Meaning |
|---|---|---|
| `DJANGO_SECRET_KEY` | **yes** | Long random string. App refuses to start without it. Generate: `python -c "from django.core.management.utils import get_random_secret_key as k; print(k())"` |
| `DATABASE_URL` | **yes** | Postgres URL from 3.3 |
| `CORS_ALLOWED_ORIGINS` | **yes** | `https://zingo-frontend.onrender.com` (comma-separate several; no trailing slash; never `*`). Also used for the WebSocket origin check. |
| `AWS_STORAGE_BUCKET_NAME` `AWS_ACCESS_KEY_ID` `AWS_SECRET_ACCESS_KEY` | **yes** (for uploads to persist) | Bucket + credentials from 3.2 |
| `AWS_S3_ENDPOINT_URL`, `AWS_S3_REGION_NAME` | R2/B2/MinIO | See 3.2 |
| `RUN_MIGRATIONS` | recommended `1` | `start.sh` applies pending additive migrations on boot (free plans have no shell) |
| `DJANGO_ALLOWED_HOSTS` | optional | `RENDER_EXTERNAL_HOSTNAME` is added automatically; add custom domains here |
| `CSRF_TRUSTED_ORIGINS` | optional | Needed to log into `/admin` from a custom domain |
| `MATCH_LAUNCH_STATE`, `DATE_LAUNCH_STATE` | see §6 | `restricted` (default) / `self_declared` / `public` |
| `DJANGO_SUPERUSER_USERNAME` `…_PASSWORD` `…_EMAIL` | optional | Creates the first admin on boot (then remove the password var) |
| `RUN_MEDIA_CHECK` | optional `1` | Prints which stored files are missing (read-only) to the logs |
| `REDIS_URL` | only if >1 instance | Redis/Key Value URL for chat/match across instances |
| `MEDIA_PUBLIC_READ` | optional `0` | `0` = private bucket + signed URLs (recommended) |
| `MAX_VIDEO_MB`, `MEDIA_URL_EXPIRE_SECONDS` | optional | Upload limit (100) / signed-URL lifetime (21600 s) |
| `DJANGO_DEBUG` | **never `1` in production** | Defaults to off |

### 3.5 Frontend service (`zingo-frontend`) — Settings
| Setting | Value |
|---|---|
| Build Command | `npm ci && npm run build` (Root Directory `frontend` if it's a monorepo) |
| Publish Directory | `dist` |
| Env var | `VITE_API_URL=https://zingo-backend-o11v.onrender.com` — **origin only, no trailing slash, no `/api`** |
| **Redirects/Rewrites** | **Add rule: Source `/*` → Destination `/index.html` → Action `Rewrite`** (this is what fixes cause B) |

> Vite bakes `VITE_API_URL` in **at build time**. After changing it you must **Clear build cache & deploy**.

### 3.6 Existing videos
Old uploads that lived on Render's disk are almost certainly gone. Set `RUN_MEDIA_CHECK=1` once and read the deploy log: it lists every database record whose file is missing.
Nothing is fabricated or hidden automatically. Options (need a shell, e.g. a paid plan or a one-off job):
`python manage.py check_media --list` · `python manage.py check_media --hide-missing --yes` (marks those videos `removed` so the feed doesn't show broken players; reversible) ·
`python manage.py sync_media_to_storage --source <folder>` (uploads files you still have locally into the bucket; never deletes).

### 3.7 Git
```bash
git status
git add -A
git commit -m "Production readiness: media storage, SPA fallback, API base URL, security settings, matching/date, tests"
git push origin main        # Render auto-deploys; for two separate repos push each (backend root / frontend folder) separately
```

---

## 4. Troubleshooting 404s (match the symptom)

| What you see | It is | Fix |
|---|---|---|
| Browser console `GET https://…/zings 404` / Render "Not Found" page on refresh | SPA fallback missing | §3.5 rewrite rule (and redeploy) |
| Console `[api] 404 GET https://…onrender.com//api/…` or `/api/api/…` | Bad `VITE_API_URL` | §3.5 — the app now also shows a red banner and `/status` |
| `[api] 404 GET …/api/some/new/route/` on a route that exists in this repo | Backend still running an old commit | Check `/status` → *Backend release* vs your latest commit; redeploy backend |
| Video player shows "couldn't load" / media URL 404 or 403 | Storage not configured, or file lost | `/status` must say *S3-compatible bucket*; upload a **new** video; old files see §3.6 |
| "Can't reach the server" | Backend asleep/down, wrong URL, or CORS | Open `/status`; add the exact frontend origin to `CORS_ALLOWED_ORIGINS` |
| Response "not JSON" | `VITE_API_URL` points at the frontend, not the backend | Fix `VITE_API_URL` |

The app logs the exact failing `METHOD URL` to the browser console for 404/5xx/network errors, and **`/status`** (public, no login) shows backend reachability, database, storage type, CORS, release and warnings.

---

## 5. What changed in the code (summary)
Backend: `config/settings.py` (rewritten), `config/urls.py`, `core/media.py` (Range-capable media + video sniffing), `core/health.py` (`/` and `/healthz/`), `core/checks.py`, `core/serializers.py` (real MP4/MOV/WebM validation), management commands `check_media` & `sync_media_to_storage`, `start.sh`, `render.yaml`, `requirements*.txt`, `.python-version`.
Frontend: `lib/config.ts`+`api.ts` (base URL normalization, loud errors), `components/ZVideo.tsx` (loading/buffering/error/retry, auto-refresh of expired signed URLs), `ErrorBoundary`, `pages/NotFound.tsx`, `pages/StatusPage.tsx`, upload form format checks, `public/_redirects`, vitest.
No models were added or removed; no data is modified by this release.

## 6. Match & Date availability (be deliberate)
Default is **`restricted` = staff accounts only**, because ages are self-declared (not verified) and dating photos are not moderated. That is the only reason "other users" may see a gated message.
* `MATCH_LAUNCH_STATE=self_declared` / `DATE_LAUNCH_STATE=self_declared` → open to **any 18+ self-declared adult** who opts in; the UI states ages are unverified. **You accept the legal/safety responsibility** for running unverified-age video chat/dating.
* `public` is honoured only with `*_REQUIRE_VERIFIED_AGE=1` **and** `*_MODERATION_CONFIRMED=1`, and requires a real age-verification provider to set `User.age_verified`. Otherwise it silently downgrades to `restricted`.

## 7. Local development
```bash
pip install -r requirements-dev.txt
python manage.py migrate && python manage.py runserver      # debug is on automatically for runserver
cd frontend && cp .env.example .env && npm install && npm run dev
```
Tests: `python manage.py test` · `cd frontend && npm test && npm run build`.

## 8. Remaining blockers / honest limits
* Not verified on your live Render services (no access). Not verified against a real AWS/R2 bucket (verified against an S3-compatible test server).
* **Bucket privacy:** signed URLs are configured, but I could not verify that *your* bucket is private — confirm in the provider dashboard.
* HEVC `.mov` files from iPhones can't play in Chrome/Firefox; upload MP4 (H.264) or WebM. No server-side transcoding exists.
* Videos upload through Django (≤ `MAX_VIDEO_MB`); very large files/slow links may time out. Direct-to-bucket uploads or a transcoder would be the next step.
* Age verification, photo/video moderation and moderator staffing are still required before opening Match/Date to the public (see §6).
* Free Render services sleep (first request is slow); free Postgres expires after 30 days.

## 9. Final live-site verification checklist
1. `https://zingo-backend-o11v.onrender.com/healthz/` → `status ok`, `database_engine postgresql`, `storage s3`, `warnings []`.
2. `https://zingo-frontend.onrender.com/status` → all ✅; Backend URL has no trailing slash; *Check my login* works after logging in.
3. Open `https://zingo-frontend.onrender.com/zings` directly (new tab) and refresh → page loads, not "Not Found".
4. Open a made-up URL like `/xyz` → the in-app 404 page (not a blank screen, not a redirect).
5. Sign up / log in on **a phone** and a desktop; confirm the nav, Home, Search, Explore work.
6. Upload a short **MP4 (H.264)** → you are taken to the video and it **plays**. Seek in it (drag the bar). Test on **iPhone Safari** too.
7. In DevTools → Network, the video request goes to your bucket host, returns **206**, and there are no CORS/mixed-content errors.
8. Redeploy the backend, reload: the video **still plays** (proves storage persists), and your account still exists (proves the database persists).
9. Like, comment, follow, chat (send a message from two accounts), notifications badge.
10. Create a WHATNEXT story and a Battle; vote from a second account.
11. With launch state enabled: ZINGO Match (two accounts, two devices, camera permission) and ZINGO Date (create → activate → like each other → chat). Test video calls across two different networks.
12. `/admin/` loads with styling; reports from Match/Date appear there.
