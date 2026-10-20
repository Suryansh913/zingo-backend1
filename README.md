# ZINGO backend (Phase 1-2 + WHATNEXT core)
Setup: `pip install -r requirements.txt && python manage.py migrate && python manage.py createsuperuser && python manage.py runserver`
Tests: `python manage.py test` (9 pass). Env: DJANGO_SECRET_KEY, DJANGO_DEBUG, DJANGO_ALLOWED_HOSTS.

## Implemented (tested)
Token auth, unique usernames, profile edit/privacy-safe public profile, follow/unfollow, block (hides content, removes follows), reports,
video upload (type/size validation), likes (unique), threaded comments, watch history, cursor pagination, mood tags,
basic For You feed, WHATNEXT (2-4 options, server-side one-vote-per-user, closing time, percentages, creator-linked follow-ups, branch tree), Django admin.

## For You algorithm (heuristic, NOT ML)
Excludes own/completed/blocked/hidden videos; mood filter; orders by followed-creator, then like count, then recency. Feed returns top 20, not yet cursor-paginated.

## NOT yet built
React frontend; audience polls; Battles; ZINGO Match (WebSocket signaling, WebRTC, TURN, 18+ enforcement - needs Channels, Redis, TURN server,
real age verification and moderation before any public launch); DMs; notifications; search; nearby discovery; password reset;
S3 storage/thumbnails/transcoding (local filesystem only - not persistent in production); rate limits beyond DRF defaults; live WebSocket vote updates (poll GET /api/nodes/<id>/).

---
# Phase 2: React frontend (`frontend/`)
**Run:** backend `pip install -r requirements.txt && python manage.py migrate && python manage.py runserver` (port 8000);
frontend `cd frontend && cp .env.example .env && npm install && npm run dev` (port 5173). Build: `npm run build`.
**Env:** frontend `VITE_API_URL` (backend origin). Backend `CORS_ALLOWED_ORIGINS` (comma list, defaults to localhost:5173 only), `DJANGO_SECRET_KEY`, and in production `DJANGO_DEBUG=0` + `DJANGO_ALLOWED_HOSTS`.
**Auth:** `Authorization: Token <key>` kept in localStorage (XSS-exposed; consider httpOnly cookies before launch). A 401 clears it and returns to login.

**Backend additions (backward compatible, tested):** videos include `liked`, `owner_following`, `story_root`; `GET /api/videos/?owner=<username>`;
`/api/videos/feed/` returns `{results, next_offset}` (offset paging via `?offset=`); profile adds `is_me`/`is_following`; node state adds `story`/`closes_at`; CORS via django-cors-headers.

**Frontend features:** signup/login/logout, protected routes, ZINGS vertical snap feed (autoplay of active video, tap to pause, mood chips, For You, infinite load, retry/empty/skeleton states),
like, threaded comments, follow, share link (`/video/:id`), watch-history reporting, profile (edit, grid, follow), upload with validation and progress (optional WHATNEXT),
WHATNEXT page (vote, percentages, countdown, 5s polling, branch links, story map).

**Known gaps:** feed uses offset paging (not cursor); no UI for creators to attach follow-up branches (API exists: `POST /api/options/<id>/link/`); no thumbnail upload UI; no password reset;
no story listing besides a video's WHATNEXT link; no report/block buttons in the UI (APIs exist); no frontend automated tests; Battles, Match, DMs, notifications, search, nearby still unbuilt.

---
# Phase 3: Battles
**Migration:** `python manage.py migrate` (adds Battle, BattleEntry, BattleVote; alters Video upload paths to random filenames. Existing data untouched).
**Cron (optional):** `python manage.py finalize_battles` — battles are also finalized lazily whenever the API is read.

## API
`GET /api/battles/?status=open|active|finished` (cursor-paged) · `POST /api/battles/` {video, opponent?, title?, blind?, voting_hours?} · `GET /api/battles/mine/` ·
`GET /api/battles/<id>/` · `POST .../accept/` {video} · `.../decline/` · `.../withdraw/` · `.../vote/` {entry} · `.../rematch/` {video} · `GET /api/users/<username>/battles/` (finished history).

## Rules (server-enforced)
- Entry video must be your own, public, active. Opponent given => status `pending` (only they can accept/decline); none => `open` public challenge (first valid joiner wins the slot, locked in a transaction).
- Challenges expire after `BATTLE_CHALLENGE_EXPIRY_HOURS` (72). Voting length 1-`BATTLE_VOTING_HOURS_MAX` (168) hours, starts when accepted. `BATTLE_LIVE_COUNTS=1` shows counts during voting (default: hidden until close).
- One vote per user per battle (DB unique constraint). Participants and users blocked with a participant can't vote; blocked users get 404 on the battle.
- Tie (equal votes or zero votes) => `tie`, no winner. Withdraw while active => forfeit, other side wins. Video removed/deleted/made non-public => voting pauses; at close the remaining entry wins (`forfeit`), both gone => `void`.
- Rematch: either participant of a finished battle can invite the other (one in-flight rematch at a time).
- Blind: creators, winner, captions and video ids are hidden from non-participants until the battle finishes; blind-battle videos are removed from public feeds/lists while live; uploads now get random filenames.

## Limitations
- **No notifications exist** (no notification model); invitations appear under My battles -> Invitations only. Polling (10 s) not WebSockets.
- Participants can see each other (and can reveal identities themselves). Blind mode does not stop a participant from announcing their entry elsewhere.
- Vote counts for open voting are computed on read (fine for small scale; add caching/denormalized counters at scale). No admin moderation actions beyond Django admin (Battle models not yet registered there).
- No automated frontend tests; UI was not exercised in a real browser. Public challenges have no eligibility filters beyond block/own-video checks.

---
# Phase 4: Chat and Notifications
**Migration:** `python manage.py migrate` (0003: Conversation, ConversationParticipant, Message, Notification; `Report.message`, `StoryNode.result_notified`). Existing data untouched.
**Run:** backend `python manage.py runserver` (Daphne serves HTTP **and** WebSockets) or in production `daphne -b 0.0.0.0 -p 8000 config.asgi:application`; frontend `cd frontend && npm install && npm run dev`.
**Env:** `REDIS_URL=redis://localhost:6379` enables the Redis channel layer (needed for >1 server process; `pip install channels-redis`). Unset = in-memory layer (single process, fine for local dev). Redis path is configured but **not tested here** (no Redis available). WebSocket origins are limited to `CORS_ALLOWED_ORIGINS` + `ALLOWED_HOSTS`.

## REST
`GET|POST /api/conversations/` (POST {username} => get-or-create, 201/200) · `GET /api/conversations/unread-count/` · `GET /api/conversations/<id>/` ·
`GET|POST /api/conversations/<id>/messages/` (POST {text?, video?, client_id?}; GET cursor-paged newest first) · `POST /api/conversations/<id>/read/` ·
`GET /api/notifications/[?unread=1]` · `GET /api/notifications/unread-count/` · `POST /api/notifications/<id>/read/` · `POST /api/notifications/read-all/` · `POST /api/report/` now also accepts `{message}`.
## WebSocket `ws://host/ws/chat/`
Client sends `{type:"auth",token}` first (closed with 4401 if wrong/late; tokens are never in the URL), then `{type:"send",conversation,text?,video?,client_id?}`.
Server sends `ready`, `ack` {client_id,message_id,duplicate}, `message` {message}, `error` {client_id,detail,status}, `pong`. Max 30 sends/10 s per socket; REST send is limited to 60/min/user.

## Rules
- One conversation per user pair (unique `pair_key`). Non-members get 404 (ids can't be probed). Blocked pairs can't start/send and conversations are hidden. Private accounts accept messages only from connected users (either side follows the other; there is no follow-approval system yet).
- Text 1-1000 chars. `client_id` makes sends idempotent: reconnect/retry/REST-fallback never duplicates. Shared videos must be viewable by both sender and recipient; later-removed videos show "unavailable".
- Notifications (follow, like, comment, reply, mention, dm, story_result, battle_invite, battle_result) are created inside the action's transaction, never for self/blocked/failed actions, and are idempotent per event (unfollow->follow or unlike->like does not re-notify). DMs keep one notification per conversation. WHATNEXT results are notified when a poll's deadline has passed (checked when notifications/story are read).

## Limitations
- Notifications are not pushed over the socket: the badge refreshes on socket messages and every 30 s. Chat unread/"Seen" updates on message events/refetch, not instantly on read. No typing indicators, message delete/edit, image attachments, or push notifications.
- WebSocket and REST are tested (41 backend tests incl. real Channels consumers) and a live Daphne + real WebSocket client run was done; the React chat UI was only compiled (`npm run build`), **not exercised in a browser**. No automated frontend tests.
- Reports of messages go to Django admin only. Match-related notifications don't exist because ZINGO Match isn't built.

---
# Phase 5: ZINGO Match (live 1:1 video)
**Migration:** `python manage.py migrate` (0004: `MatchTicket`, `MatchSession`, `ActiveMatch`; `User.interests/language/age_verified/match_consent_version/match_banned_until`; `Report.session/category`).
**Run (dev):** backend `MATCH_LAUNCH_STATE=development python manage.py runserver` (DEBUG defaults to development mode) · frontend `cd frontend && npm install && npm run dev` · cron/loop: `python manage.py match_cleanup` every minute (also done lazily).
Two-device test: open the app on two devices/profiles with two adult accounts (add a birth date in Profile -> Edit), both go to **Match**, accept the terms, **Find a match**.
> Camera access needs a secure context: `localhost` works; any other device needs **HTTPS** (e.g. a tunnel such as ngrok/cloudflared for the dev server and the API).

## API
`GET /api/match/eligibility/` · `POST /api/match/consent/` {accept:true} · `GET /api/match/ice/` · `GET|POST|DELETE /api/match/queue/` (POST {mode: random|interest|creator, interests[], language}; GET = status + keep-alive + retry matching) ·
`GET /api/match/sessions/<id>/` · `POST .../accept|decline|skip|end/` · `POST .../report/` {category, reason, block?}. Participants only; anyone else gets 404.
## WebSocket `/ws/match/` (signaling only; media never touches Django)
`{type:"auth",token}` first -> `ready`,`state`. `{type:"join",session}` -> `joined{role}`. `{type:"signal",session,kind:"ready|offer|answer|ice",data}` is relayed to the peer **only if** the sender belongs to that session and it is `active` (checked on every message; 20 KB cap; 300 msgs/10 s). Server events: `session` (state changes, match found), `signal`, `error`.
Flow: queue -> match (both see username/bio/shared interests) -> both Accept within 20 s -> lobby -> each user presses **Start video call** (only then `getUserMedia`) -> caller (user_a) offers, callee answers -> ICE (STUN, TURN if configured), ICE-restart on network trouble.

## Safety design
- **Server-enforced** eligibility: date of birth present and >= 18, not restricted, explicit consent (versioned), launch gate. DOB can't be changed once set. **Age is self-declared** unless `MATCH_REQUIRE_VERIFIED_AGE=1` and a real provider sets `User.age_verified` (not built: you need an ID/face-age-estimation vendor and a webhook that sets it).
- **Launch gate:** `MATCH_LAUNCH_STATE=public` is silently downgraded to `restricted` (staff-only) unless both `MATCH_REQUIRE_VERIFIED_AGE=1` and `MATCH_MODERATION_CONFIRMED=1`. Default outside DEBUG is `restricted`. Treat anything else as **not** production-safe.
- Blocked pairs never match (queue excludes them; blocking ends a live match). Skipped/declined/reported partners aren't re-matched for 10 min. Hourly session cap (60), 30 POST/min throttle, one active match per user (DB unique constraint).
- Matches see only username, bio, creator flag, shared interests. Never email, DOB/age, city, location, user id. Calls are **not recorded**; no media is stored.
- **Moderation workflow:** report -> call ends at once (optional block) -> `Report` row linked to the session (Django admin: Reports + read-only Match sessions) -> staff review; 3 distinct reporters in 24 h auto-restrict the account for 24 h; admin actions "Restrict from Match (7 days)" / "Lift". Because calls aren't recorded, moderators only have reporter text + metadata, not evidence. Needs a human review process and an incident/escalation policy (esp. for minors/CSAM reports and legal-request handling) before launch.

## Services & deployment
- **Postgres** (not SQLite) in production: the queue uses `SELECT ... FOR UPDATE SKIP LOCKED` + unique constraints for correct concurrent matching. **Redis** channel layer is required for >1 Daphne worker so signaling reaches the other user's process (`REDIS_URL`). Run `daphne -b 0.0.0.0 -p 8000 config.asgi:application` behind a TLS proxy that forwards WebSocket upgrades (nginx `proxy_set_header Upgrade/Connection`, long `proxy_read_timeout`). Use `wss://`.
- **STUN** is free (public default `stun.l.google.com`; fine for dev). **TURN is required in practice**: some networks (symmetric NAT, corporate/mobile) can't connect peer-to-peer. Self-host coturn (UDP 3478 + TLS 5349/443 + relay port range; `use-auth-secret`/`static-auth-secret` = `TURN_SHARED_SECRET`) or use a managed TURN provider. Prefer `turns:` on 443 for strict firewalls.
- **Browsers:** current Chrome/Edge/Firefox, Safari 14.3+ (iOS: all browsers use WebKit). HTTPS required for camera/mic (localhost excepted). Mobile browsers need a user tap before media (the Start button provides it); permission-denied/no-device/in-use states are handled in the UI.
- **Rough cost guide (approximate; check current provider pricing):** small VPS for Django+coturn ~US$5-15/mo; managed Redis free tiers are enough for dev, small paid ~US$5-10/mo; managed Postgres free tiers are small/may pause, paid ~US$7-25/mo; TURN relay bandwidth ~1-3 GB per relayed call-hour (only the fraction of calls that can't go peer-to-peer); per-check age verification and moderator staffing usually dominate real cost. **Don't launch on HTTP-only or sleep/ephemeral free hosts** (no HTTPS/UDP for coturn, WebSockets dropped, local media disk lost).

## What was tested
- **Automated (66 backend tests, all passing):** eligibility/age/consent/launch gate, queue create/cancel, matching + duplicate prevention, **concurrent joins (threads; SQLite only)**, blocked exclusion, accept/decline/skip/end, cleanup, rate limits, report + auto-restrict, privacy of payloads, ICE/TURN credentials, WebSocket auth/origin, signaling authorization, disconnect cleanup.
- **Real WebRTC media, automated but NOT browsers:** `core/test_match_media.py` runs two `aiortc` peers that negotiate through the real signaling consumer over loopback ICE/DTLS and each receive real video + audio frames.
- **Live network smoke:** real Daphne + real WebSocket clients: queue, match push, accept, join, offer relay, outsider denied, end notification.
- **NOT tested:** any real browser or phone, two separate devices/networks, the React Match UI (it only compiles: `npm run build` OK), TURN relaying, Redis channel layer, Postgres concurrency, Safari/iOS permission behavior. **Do not treat video calling as verified in browsers until you run the two-device test above.**

## Remaining blockers before any public launch
Real age assurance provider; moderation staffing + escalation policy; two-device/browser testing incl. mobile Safari and a TURN-only network; production TURN + HTTPS/WSS + Redis + Postgres; legal review (age, privacy, data retention, regional rules); abuse tooling beyond reports (e.g. automated nudity detection, device/IP bans); match history UI, push notifications; automated frontend tests.

---
# Phase 6: Instagram-style Home + light theme
**Run:** nothing new to migrate. Frontend `cd frontend && npm install && npm run dev` (adds `lucide-react`); backend as before.
## Routes
`/` Home (stories row, For You / Following feed, right rail) · `/zings` full-screen vertical ZINGS feed (moved from `/`) · `/search` · `/explore` · `/whatnext` · existing `/battles`, `/match`, `/chat`, `/notifications`, `/upload`, `/u/<name>`.
## New read-only API (all login-required, respect blocks/visibility)
`GET /api/home/stories/` (ZINGS from the last 24 h by you + people you follow, grouped by creator; `seen` from real watch history) · `GET /api/suggestions/users/` · `GET /api/trending/hashtags/` (last 7 days, real counts) ·
`GET /api/search/?q=` (accounts, videos, hashtags; `#tag` searches tags only; min 2 chars) · `GET /api/videos/explore/` (top-liked public videos, 30 days) · `GET /api/videos/?following=1` (cursor-paged) · `GET /api/stories/` (WHATNEXT list).
## Design system (frontend/src/index.css)
Tokens: `ink #202124`, `mute #777B85`, `line #E6E8EE`, `soft #F8F9FC`, purple/pink/blue/orange accents. `.btn` = purple->pink gradient, `.btn2` = light grey, `.inp`, `grad-ring` (orange/pink/purple/blue story ring). Icons: Lucide. Video surfaces (ZINGS full-screen player, live call, story viewer) stay dark on purpose so video letterboxing isn't white.
## What "stories" and "posts" mean here (no fake data)
ZINGO has **no photo posts and no ephemeral photo stories**. The Stories row is built from real ZINGS posted in the last 24 h; the feed contains ZINGS videos only (WHATNEXT videos carry a vote badge). Adding photo posts would need a new model/upload pipeline.
## Limitations
Not verified visually in a browser (build + static audit only). Some older pages still use emoji icons (Match, Battles, Chat headers); the nav/feed use Lucide. Avatars are letter circles (no profile photos yet). No saved/bookmarked videos (no backend). Right-rail "Live battles" shows active battles only. No automated frontend tests.

---
# Phase 7: ZINGO Date (opt-in 18+ dating)
**Migration:** `python manage.py migrate` (0005: `DatingProfile`, `DatingPhoto`, `DatingLike`, `DateMatch`; `Conversation.origin`). Existing users, profiles, chats untouched.
**Run:** same as before (`runserver`, `npm run dev`). In dev (`DEBUG`) any consenting self-declared adult can use it; outside DEBUG the default is `restricted` (staff only) - see env vars in `.env.example`.
## Flow
Welcome page -> **Add Profile to Date** (`POST /api/date/profile/create/`, identity from the auth token only, idempotent: 201 first time, 200 afterwards) -> review/edit screen with live "how others see you" preview -> **Activate Dating Profile** (confirm dialog; server re-checks eligibility + required fields) -> Discover / Matches / My profile tabs. Nothing is visible to anyone until activation; deactivate any time.
## API (`/api/date/...`; all login-required)
`profile/` GET · `profile/create/` POST · `profile/update/` PATCH · `profile/activate/` · `profile/deactivate/` · `profile/photos/` POST (multipart `image`) · `profile/photos/<id>/` DELETE · `preferences/` GET (options + limits) ·
`discover/` GET (cursor-paged) · `profiles/<username>/` GET · `like/` · `pass/` · `matches/` GET · `unmatch/` · `report/`. Blocking uses the existing `/api/users/<u>/block/` (now also ends a live match).
## Rules (server-enforced)
- **Eligibility:** date of birth present and >= 18 (age is **self-declared** - `age_verified` is only set by an admin/provider), not on moderation hold, launch gate. Underage users can't even create a draft. DOB is stored on the account (not the dating profile), can't be changed once set, and is never exposed (only the computed age).
- **Reuse, not duplication:** the draft is seeded by *copying* bio / interests / city from the social profile; the `User` row is never written by any Date endpoint (tested). ZINGO has **no profile photo or display name**, so there was nothing to reuse: dating photos are a new upload (JPEG/PNG/WebP, <=5 MB, <=6, re-encoded to JPEG to strip EXIF/GPS) and the username is the display name.
- **Required to activate:** DOB, a bio, and "interested in". Gender, intention, city, photos are optional. Age range 18-99.
- **Discovery:** only active, non-held profiles whose age fits your range *and* whose range fits you, with mutual gender interest ("Everyone" matches all; a blank gender is shown only to people interested in Everyone). Excludes yourself, blocked either way, anyone you liked/passed/reported, and any pair that has ever matched. Browsing requires your own active profile. Order: newest first (no ranking algorithm). Private accounts stay discoverable (they opted in) but their ZINGO-profile link is only shown to followers.
- **Matching:** like + existing reverse like = exactly one `DateMatch` per pair (unique `pair_key`, pair rows locked so simultaneous likes can't miss each other); a pass is final; max 100 likes/day. Both users get an in-app `date_match` notification (not push).
- **Chat:** reuses the existing conversations/WebSocket chat. A match creates (or reuses) the pair's conversation. If it was created by a match (`origin="date"`) it is only readable/writable **while the match is active**; unmatch or block makes it 404 for both and hides it from the inbox. A pre-existing normal chat is never removed. Matched users may message even across a private-account setting (mutual consent).
- **Safety:** report (only profiles you can actually see; reporter never sees them again), block, unmatch, deactivate. 3 distinct reporters in 7 days auto-hides a profile (`moderation_hold`); staff clear/confirm it in Django admin ("Dating profiles" with hold/clear actions). Exact location is never collected: city is free text.
## Tests
`core/test_date.py` (20): draft creation/idempotency/social-profile-unchanged/not-visible, missing fields, underage/unverified/restricted/held, validation, photo validation & ownership, discovery rules + pagination + privacy of card, private accounts, one-sided vs mutual like, exactly-one-match (incl. a **concurrent** mutual-like test, SQLite), duplicates/invalid likes, daily cap, unmatch/block chat access, reports + auto-hold, preferences. Full backend suite: 94 tests pass.
## Limitations / before a public launch
Age is not verified (needs a verification vendor + `DATE_REQUIRE_VERIFIED_AGE=1`); **uploaded photos are not moderated** (no nudity/face/fake-profile detection - needs a service and a review queue); no moderation staffing/escalation policy; no distance/geo filtering, ranking or "who liked me"; no undo of a pass; no push notifications; matching race safety is tested on SQLite only (production should use Postgres); Date UI was compiled and exercised only via API (no browser rendering/automated frontend tests); legal review (age, privacy, data retention, regional dating rules) not done.

---
# Phase 8: Production readiness (Render) - see DEPLOY.md
**Read `DEPLOY.md` first** - it has the root causes of the 404 / video-won't-play problems, exact Render settings, env vars, backup & rollback, git commands and the live verification checklist.
Summary of the fixes: S3-compatible private media storage with signed URLs + Range-capable local fallback; real MP4/MOV/WebM validation; `/` and `/healthz/`; secure-by-default `settings.py` (no DEBUG/dev-key/localhost defaults in production, Postgres via `DATABASE_URL`, WhiteNoise, HTTPS/HSTS/cookie flags, strict CORS);
frontend API-base normalization (trailing `/` or `/api` caused //api and /api/api 404s) with loud config errors; SPA fallback (`render.yaml` rewrite + `public/_redirects`) and a real in-app 404 page; resilient video player (loading/buffering/error/retry, auto-refresh of expired signed URLs); `/status` diagnostics page; error boundary.
Dev: `pip install -r requirements-dev.txt` · `python manage.py test` · `cd frontend && npm test && npm run build`.
New commands: `check_media`, `sync_media_to_storage`. New tests: `test_production`, `test_s3_storage` (real S3-compatible server), `test_contract` (76 frontend API calls vs Django URLs), `test_frontend_static` (dead links/buttons/placeholders), frontend `config.test.ts`. Full suite verified on SQLite **and** PostgreSQL 16.
