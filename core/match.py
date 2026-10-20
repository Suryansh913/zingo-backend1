"""ZINGO Match: eligibility, shared DB queue, session lifecycle, ICE config. Used by REST views and the signaling consumer."""
import base64, hashlib, hmac, logging, time
from datetime import timedelta
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework.decorators import api_view, throttle_classes
from rest_framework.response import Response
from rest_framework.throttling import SimpleRateThrottle
from .models import *
from .util import clean_interests
from .views import blocked_ids
log = logging.getLogger(__name__)
MODES = ("random", "interest", "creator")
LIVE = ("pending", "active")

class MatchError(Exception):
    def __init__(self, status, code, msg): super().__init__(msg); self.status, self.code, self.msg = status, code, msg
class _Skip(Exception): pass

# ---------- eligibility / consent / launch gate (all server-side) ----------
def eligibility(user):
    """Reasons the user can NOT use Match. Empty list = eligible. Age is SELF-DECLARED unless MATCH_REQUIRE_VERIFIED_AGE + a provider sets age_verified."""
    r = []; now = timezone.now()
    if settings.MATCH_LAUNCH_STATE == "restricted" and not user.is_staff: r.append("restricted")
    if user.date_of_birth is None: r.append("dob_missing")
    elif user.age() < 18: r.append("underage")
    if settings.MATCH_REQUIRE_VERIFIED_AGE and not user.age_verified: r.append("age_not_verified")
    if user.match_banned_until and user.match_banned_until > now: r.append("restricted_by_moderation")
    return r

def consented(user): return user.match_consent_version >= settings.MATCH_CONSENT_VERSION

def require_ready(user):
    r = eligibility(user)
    if r: raise MatchError(403, r[0], "You're not eligible for ZINGO Match: " + ", ".join(r))
    if not consented(user): raise MatchError(403, "consent_required", "Please read and accept the Match terms first.")

def is_creator(user): return Video.objects.filter(owner=user, status="active", visibility="public").exists()

# ---------- serialization & push ----------
def iso(d): return d.isoformat() if d else None

def ser_session(s, viewer):
    a = s.user_a_id == viewer.id; o = s.user_b if a else s.user_a
    return {"id": s.id, "mode": s.mode, "status": s.status, "role": "caller" if a else "callee", "accept_deadline": iso(s.accept_deadline),
            "i_accepted": s.a_accepted if a else s.b_accepted, "peer_accepted": s.b_accepted if a else s.a_accepted,
            "started_at": iso(s.started_at), "ended_at": iso(s.ended_at), "end_reason": s.end_reason,
            "shared_interests": [i for i in s.shared_interests.split(",") if i],
            # Everything the other person is shown. No email, DOB, age, city, location, or numeric id.
            "other": {"username": o.username, "bio": o.bio, "is_creator": is_creator(o), "is_following": Follow.objects.filter(follower=viewer, followee=o).exists()}}

def push_session(s):
    try:
        layer = get_channel_layer()
        for u in (s.user_a, s.user_b):
            async_to_sync(layer.group_send)(f"mu_{u.id}", {"type": "match.event", "event": {"type": "session", "session": ser_session(s, u)}})
    except Exception: log.exception("match push failed")

# ---------- queue ----------
def _recent_partners(user):
    since = timezone.now() - timedelta(minutes=settings.MATCH_COOLDOWN_MINUTES); ids = set()
    for a, b in MatchSession.objects.filter(Q(user_a=user) | Q(user_b=user), created__gte=since, end_reason__in=["skip", "declined", "expired", "reported", "blocked"]).values_list("user_a_id", "user_b_id"): ids |= {a, b}
    ids.discard(user.id); return ids

def _shared(a, b): return [i for i in clean_interests(a) if i in set(clean_interests(b))]

def join_queue(user, mode, interests, language):
    require_ready(user)
    if mode not in MODES: raise MatchError(400, "bad_mode", "Mode must be random, interest or creator.")
    tags = clean_interests(interests or user.interests) if mode == "interest" else []
    if mode == "interest" and not tags: raise MatchError(400, "interests_required", "Pick at least one interest.")
    if mode == "creator" and not is_creator(user): raise MatchError(400, "creator_required", "Creator Match needs at least one public video.")
    cleanup_user(user)
    if ActiveMatch.objects.filter(user=user).exists(): raise MatchError(409, "already_matched", "You're already in a match.")
    if MatchSession.objects.filter(Q(user_a=user) | Q(user_b=user), created__gte=timezone.now() - timedelta(hours=1)).count() >= settings.MATCH_MAX_SESSIONS_PER_HOUR:
        raise MatchError(429, "hourly_limit", "You've reached the hourly match limit. Take a break and try again later.")
    now = timezone.now()
    MatchTicket.objects.update_or_create(user=user, defaults={"mode": mode, "interests": ",".join(tags), "language": (language or user.language or "")[:10].lower(),
                                                              "created": now, "expires_at": now + timedelta(seconds=settings.MATCH_QUEUE_TTL)})
    return try_match(user)

def try_match(user):
    """Atomically pair `user` with the best waiting candidate. Returns a pending MatchSession or None (still queued)."""
    s = None
    with transaction.atomic():
        me = MatchTicket.objects.select_for_update().select_related("user").filter(user=user).first()
        if not me:
            slot = ActiveMatch.objects.select_related("session").filter(user=user).first(); return slot.session if slot else None
        now = timezone.now()
        cands = (MatchTicket.objects.select_for_update(skip_locked=True).select_related("user").filter(mode=me.mode, expires_at__gt=now)
                 .exclude(user=user).exclude(user_id__in=blocked_ids(user) | _recent_partners(user)).order_by("created"))
        usable = []
        for c in cands:
            if eligibility(c.user) or not consented(c.user): continue
            sh = _shared(me.interests, c.interests)
            if me.mode == "interest" and not sh: continue
            if me.mode == "creator" and not is_creator(c.user): continue
            usable.append((0 if (me.language and me.language == c.language) else 1, c.created, c, sh))
        for _, _, c, sh in sorted(usable, key=lambda x: (x[0], x[1])):
            try:
                with transaction.atomic():  # everything below commits together or not at all
                    if MatchTicket.objects.filter(pk=c.pk).delete()[0] != 1: raise _Skip()  # someone else claimed them
                    MatchTicket.objects.filter(pk=me.pk).delete()
                    s = MatchSession.objects.create(user_a=c.user, user_b=user, mode=me.mode, shared_interests=",".join(sh), accept_deadline=now + timedelta(seconds=settings.MATCH_ACCEPT_SECONDS))
                    ActiveMatch.objects.create(user=c.user, session=s); ActiveMatch.objects.create(user=user, session=s)  # UNIQUE(user): cannot be in two sessions
                break
            except (_Skip, IntegrityError): s = None; continue
    if s: s = MatchSession.objects.select_related("user_a", "user_b").get(pk=s.pk); push_session(s)
    return s

def cancel_queue(user): MatchTicket.objects.filter(user=user).delete()

# ---------- session lifecycle ----------
def get_session(user, sid):
    s = MatchSession.objects.filter(pk=sid).filter(Q(user_a=user) | Q(user_b=user)).select_related("user_a", "user_b").first()
    if not s: raise MatchError(404, "not_found", "Not found.")  # same answer for "doesn't exist" and "not yours"
    return s

def end_session(sid, status, reason, by=None):
    with transaction.atomic():
        s = MatchSession.objects.select_for_update().select_related("user_a", "user_b").get(pk=sid)
        if s.status not in LIVE: return s, False
        s.status, s.end_reason, s.ended_at, s.ended_by = status, reason, timezone.now(), by; s.save()
        ActiveMatch.objects.filter(session=s).delete()
    push_session(s); return s, True

def cleanup_session(sid):
    s = MatchSession.objects.filter(pk=sid).first(); now = timezone.now()
    if not s: return None
    if s.status == "pending" and s.accept_deadline <= now: return end_session(sid, "expired", "expired")[0]
    if s.status == "active":
        cut = now - timedelta(seconds=settings.MATCH_DISCONNECT_GRACE)
        if (s.a_seen or s.started_at) <= cut or (s.b_seen or s.started_at) <= cut: return end_session(sid, "ended", "disconnected")[0]
    return s

def cleanup_user(user):
    MatchTicket.objects.filter(user=user, expires_at__lte=timezone.now()).delete()
    slot = ActiveMatch.objects.filter(user=user).first()
    if slot: cleanup_session(slot.session_id)

def cleanup_all():
    n = MatchTicket.objects.filter(expires_at__lte=timezone.now()).delete()[0]
    for sid in MatchSession.objects.filter(status__in=LIVE).values_list("pk", flat=True): cleanup_session(sid)
    return n

def touch(sid, user):
    f = "a_seen" if MatchSession.objects.filter(pk=sid, user_a=user).exists() else "b_seen"
    MatchSession.objects.filter(pk=sid, status__in=LIVE).update(**{f: timezone.now()})

def accept(user, sid):
    with transaction.atomic():
        s = MatchSession.objects.select_for_update().select_related("user_a", "user_b").filter(pk=sid).filter(Q(user_a=user) | Q(user_b=user)).first()
        if not s: raise MatchError(404, "not_found", "Not found.")
        if s.status == "pending" and s.accept_deadline <= timezone.now(): s = None
        elif s.status != "pending": raise MatchError(409, "not_pending", "This match is no longer waiting for a response.")
        if s:
            if s.user_a_id == user.id: s.a_accepted = True
            else: s.b_accepted = True
            if s.a_accepted and s.b_accepted: s.status, s.started_at, s.a_seen, s.b_seen = "active", timezone.now(), timezone.now(), timezone.now()
            s.save()
    if s is None: end_session(sid, "expired", "expired"); raise MatchError(409, "expired", "The match request expired.")
    push_session(s); return s

def leave(user, sid, reason):
    """decline / skip / end: any participant, any live state. pending => declined; active => ended."""
    s = get_session(user, sid)
    if s.status not in LIVE: return s
    return end_session(sid, "declined" if s.status == "pending" else "ended", reason, by=user)[0]

def on_block(blocker, target):
    for s in MatchSession.objects.filter(status__in=LIVE).filter(Q(user_a=blocker, user_b=target) | Q(user_a=target, user_b=blocker)): end_session(s.pk, "declined" if s.status == "pending" else "ended", "blocked", by=blocker)

def report(user, sid, category, reason, block=False):
    s = get_session(user, sid); other = s.user_b if s.user_a_id == user.id else s.user_a
    if not str(reason or "").strip(): raise MatchError(400, "reason_required", "Please describe the problem.")
    with transaction.atomic():
        Report.objects.create(reporter=user, user=other, session=s, category=str(category or "other")[:20], reason=str(reason)[:300])
        if block: Block.objects.get_or_create(blocker=user, blocked=other)
    leave(user, sid, "reported")  # reporting always ends the call immediately
    since = timezone.now() - timedelta(hours=24)
    if Report.objects.filter(user=other, session__isnull=False, created__gte=since).values("reporter").distinct().count() >= settings.MATCH_AUTO_RESTRICT_REPORTS:
        other.match_banned_until = timezone.now() + timedelta(hours=24); other.save(update_fields=["match_banned_until"])  # temporary; staff review in admin
        cancel_queue(other)
        for sl in ActiveMatch.objects.filter(user=other): end_session(sl.session_id, "ended", "restricted")

# ---------- ICE (STUN/TURN) ----------
def ice_config(user):
    servers = [{"urls": settings.STUN_URLS}] if settings.STUN_URLS else []
    if settings.TURN_URLS:
        if settings.TURN_SHARED_SECRET:  # coturn "use-auth-secret": credentials are minted per user and expire
            name = f"{int(time.time()) + settings.TURN_TTL}:{user.id}"
            cred = base64.b64encode(hmac.new(settings.TURN_SHARED_SECRET.encode(), name.encode(), hashlib.sha1).digest()).decode()
            servers.append({"urls": settings.TURN_URLS, "username": name, "credential": cred})
        elif settings.TURN_USERNAME and settings.TURN_CREDENTIAL:
            servers.append({"urls": settings.TURN_URLS, "username": settings.TURN_USERNAME, "credential": settings.TURN_CREDENTIAL})
    return {"iceServers": servers, "turn_configured": any("username" in s for s in servers)}

# ---------- REST ----------
class MatchThrottle(SimpleRateThrottle):
    scope = "match"
    def get_cache_key(self, request, view):
        if request.method == "GET" or not request.user.is_authenticated: return None
        return self.cache_format % {"scope": self.scope, "ident": request.user.pk}

def err(e): return Response({"detail": e.msg, "code": e.code}, status=e.status)
def queue_state(user, s=None):
    if s is None:
        slot = ActiveMatch.objects.select_related("session__user_a", "session__user_b").filter(user=user).first(); s = slot.session if slot else None
    if s and s.status in LIVE: return {"status": "matched", "session": ser_session(s, user)}
    t = MatchTicket.objects.filter(user=user).first()
    if t: return {"status": "queued", "mode": t.mode, "waited": int((timezone.now() - t.created).total_seconds())}
    return {"status": "idle"}

@api_view(["GET"])
def eligibility_view(request):
    u = request.user; r = eligibility(u)
    return Response({"eligible": not r, "reasons": r, "consent": consented(u), "launch_state": settings.MATCH_LAUNCH_STATE, "age_verified": u.age_verified,
                     "age_note": "Your date of birth is self-declared and not verified." if not u.age_verified else "Your age was verified.",
                     "has_dob": u.date_of_birth is not None, "is_creator": is_creator(u), "turn_configured": ice_config(u)["turn_configured"]})

@api_view(["POST"])
@throttle_classes([MatchThrottle])
def consent_view(request):
    r = eligibility(request.user)
    if r: return Response({"detail": "Not eligible.", "code": r[0]}, status=403)
    if request.data.get("accept") is not True: return Response({"detail": "Consent must be explicitly accepted."}, status=400)
    request.user.match_consent_version = settings.MATCH_CONSENT_VERSION; request.user.save(update_fields=["match_consent_version"])
    return Response({"consent": True})

@api_view(["GET"])
def ice_view(request):
    try: require_ready(request.user)
    except MatchError as e: return err(e)
    return Response(ice_config(request.user))

@api_view(["GET", "POST", "DELETE"])
@throttle_classes([MatchThrottle])
def queue_view(request):
    u = request.user
    try:
        if request.method == "DELETE": cancel_queue(u); return Response(status=204)
        if request.method == "POST":
            s = join_queue(u, request.data.get("mode"), request.data.get("interests"), request.data.get("language")); return Response(queue_state(u, s))
        require_ready(u); cleanup_user(u)
        t = MatchTicket.objects.filter(user=u).first()
        if t and t.expires_at > timezone.now():  # polling keeps the ticket alive and retries matching
            MatchTicket.objects.filter(pk=t.pk).update(expires_at=timezone.now() + timedelta(seconds=settings.MATCH_QUEUE_TTL)); return Response(queue_state(u, try_match(u)))
        return Response(queue_state(u))
    except MatchError as e: return err(e)

@api_view(["GET"])
def session_view(request, pk):
    try: get_session(request.user, pk); s = cleanup_session(pk); return Response(ser_session(MatchSession.objects.select_related("user_a", "user_b").get(pk=pk), request.user))
    except MatchError as e: return err(e)

def _action(fn):
    @api_view(["POST"])
    @throttle_classes([MatchThrottle])
    def view(request, pk):
        try: s = fn(request, pk); return Response(ser_session(MatchSession.objects.select_related("user_a", "user_b").get(pk=pk), request.user))
        except MatchError as e: return err(e)
    return view
accept_view = _action(lambda r, pk: accept(r.user, pk))
decline_view = _action(lambda r, pk: leave(r.user, pk, "declined"))
skip_view = _action(lambda r, pk: leave(r.user, pk, "skip"))
end_view = _action(lambda r, pk: leave(r.user, pk, "ended"))

@api_view(["POST"])
@throttle_classes([MatchThrottle])
def report_view(request, pk):
    try: report(request.user, pk, request.data.get("category"), request.data.get("reason"), bool(request.data.get("block"))); return Response(status=201)
    except MatchError as e: return err(e)
