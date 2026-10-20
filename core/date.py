"""ZINGO Date: opt-in 18+ dating on top of the existing account. Dating data lives in its own tables; the social profile is only ever READ."""
import io
from datetime import date, timedelta
from django.conf import settings
from django.core.files.base import ContentFile
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone
from PIL import Image, ImageOps
from rest_framework.decorators import api_view, parser_classes, throttle_classes
from rest_framework.pagination import CursorPagination
from rest_framework.parsers import MultiPartParser
from rest_framework.response import Response
from rest_framework.throttling import SimpleRateThrottle
from .models import *
from .notifications import notify
from .util import clean_interests
from .views import blocked_ids

GENDERS = ["woman", "man", "nonbinary", "other"]; INTEREST_OPTIONS = GENDERS + ["everyone"]
INTENTIONS = ["long_term", "short_term", "friends", "unsure"]
PHOTO_TYPES = {"image/jpeg", "image/png", "image/webp"}

class DateError(Exception):
    def __init__(self, status, code, msg, **extra): super().__init__(msg); self.status, self.code, self.msg, self.extra = status, code, msg, extra
def err(e): return Response({"detail": e.msg, "code": e.code, **e.extra}, status=e.status)

def pack(items): return "," + ",".join(items) + "," if items else ""
def unpack(s): return [x for x in (s or "").split(",") if x]
def years_ago(n):
    t = date.today()
    try: return t.replace(year=t.year - n)
    except ValueError: return t.replace(year=t.year - n, day=28)
def pair_key(a, b): return f"{min(a.id, b.id)}:{max(a.id, b.id)}"

# ---------- eligibility (server-side; age is SELF-DECLARED unless a provider sets age_verified) ----------
def eligibility(user):
    r = []
    if settings.DATE_LAUNCH_STATE == "restricted" and not user.is_staff: r.append("restricted")
    if user.date_of_birth is None: r.append("dob_missing")
    elif user.age() < 18: r.append("underage")
    if settings.DATE_REQUIRE_VERIFIED_AGE and not user.age_verified: r.append("age_not_verified")
    p = DatingProfile.objects.filter(user=user).only("moderation_hold").first()
    if p and p.moderation_hold: r.append("moderation_hold")
    return r

def missing_fields(user, p):
    m = []
    if not user.date_of_birth: m.append("date_of_birth")
    if not p.bio.strip(): m.append("bio")
    if not unpack(p.interested_in): m.append("interested_in")
    return m

def ready(user):
    """Viewer must be eligible AND have an active dating profile (browsing is opt-in too)."""
    r = eligibility(user)
    if r: raise DateError(403, r[0], "You're not eligible for ZINGO Date: " + ", ".join(r))
    p = DatingProfile.objects.filter(user=user).first()
    if not p or not p.is_active: raise DateError(403, "not_active", "Activate your dating profile first.")
    return p

# ---------- serialization ----------
def photos(p): return [{"id": x.id, "url": x.image.url} for x in p.photos.all()]
def ser_self(p):
    u = p.user
    return {"exists": True, "username": u.username, "age": u.age(), "bio": p.bio, "gender": p.gender, "interested_in": unpack(p.interested_in), "interests": clean_interests(p.interests),
            "intention": p.intention, "city": p.city, "min_age": p.min_age, "max_age": p.max_age, "is_active": p.is_active, "moderation_hold": p.moderation_hold,
            "activated_at": p.activated_at, "photos": photos(p), "missing": missing_fields(u, p), "created": p.created, "updated": p.updated}

def ser_card(p, viewer):
    """What OTHER people see. No email, birth date, exact location, gender/preferences, or private-account content."""
    u = p.user; mine = set(clean_interests(viewer.interests)) | set()
    vp = getattr(viewer, "dating", None); mine = set(clean_interests(vp.interests)) if vp else mine
    theirs = clean_interests(p.interests)
    return {"username": u.username, "age": u.age(), "bio": p.bio, "city": p.city, "interests": theirs, "shared_interests": [i for i in theirs if i in mine], "intention": p.intention,
            "photos": photos(p), "social_visible": (not u.is_private) or Follow.objects.filter(follower=viewer, followee=u).exists()}

# ---------- discovery rules ----------
def compatible_qs(vp):
    """Everyone this viewer is allowed to see: active, not held, mutual age range + mutual gender interest, not blocked."""
    v = vp.user; age = v.age(); mine = unpack(vp.interested_in)
    qs = (DatingProfile.objects.filter(is_active=True, moderation_hold=False, user__date_of_birth__isnull=False).exclude(user=v).exclude(user_id__in=blocked_ids(v))
          .filter(user__date_of_birth__lte=years_ago(vp.min_age), user__date_of_birth__gt=years_ago(vp.max_age + 1)).filter(min_age__lte=age, max_age__gte=age))
    if "everyone" not in mine: qs = qs.filter(gender__in=mine)
    theirs = Q(interested_in__contains=",everyone,")
    if vp.gender: theirs |= Q(interested_in__contains=f",{vp.gender},")
    qs = qs.filter(theirs)
    if settings.DATE_REQUIRE_VERIFIED_AGE: qs = qs.filter(user__age_verified=True)
    if settings.DATE_LAUNCH_STATE == "restricted": qs = qs.filter(user__is_staff=True)
    return qs.select_related("user").prefetch_related("photos")

def paired_ids(v):
    ids = set()
    for a, b in DateMatch.objects.filter(Q(user_low=v) | Q(user_high=v)).values_list("user_low_id", "user_high_id"): ids |= {a, b}
    ids.discard(v.id); return ids

def discover_qs(vp):
    v = vp.user
    return compatible_qs(vp).exclude(user_id__in=DatingLike.objects.filter(from_user=v).values("to_user_id")).exclude(user_id__in=paired_ids(v))

def active_match(a, b): return DateMatch.objects.filter(pair_key=pair_key(a, b), active=True).first()
def visible_to(vp, target):
    return compatible_qs(vp).filter(user=target).exists() or bool(active_match(vp.user, target))

class DatePager(CursorPagination): ordering = "-id"; page_size = 10
class DateThrottle(SimpleRateThrottle):
    scope = "date"
    def get_cache_key(self, request, view):
        if request.method == "GET" or not request.user.is_authenticated: return None
        return self.cache_format % {"scope": self.scope, "ident": request.user.pk}

# ---------- profile lifecycle ----------
def _mine(user):
    p = DatingProfile.objects.filter(user=user).prefetch_related("photos").select_related("user").first()
    if not p: raise DateError(404, "no_profile", "Create your dating profile first.")
    return p

@api_view(["GET"])
def profile_get(request):
    u = request.user; r = eligibility(u); p = DatingProfile.objects.filter(user=u).prefetch_related("photos").select_related("user").first()
    return Response({"exists": bool(p), "profile": ser_self(p) if p else None, "eligible": not r, "reasons": r, "launch_state": settings.DATE_LAUNCH_STATE,
                     "age_verified": u.age_verified, "age_note": "Your age is self-declared and has not been verified." if not u.age_verified else "Your age was verified.",
                     "has_dob": u.date_of_birth is not None})

@api_view(["POST"])
@throttle_classes([DateThrottle])
def profile_create(request):
    """Idempotent. Copies bio/interests/city from the social profile into a separate, INACTIVE draft. Never writes to the social profile."""
    u = request.user; p = DatingProfile.objects.filter(user=u).prefetch_related("photos").select_related("user").first()
    if p: return Response(ser_self(p), status=200)
    blocking = [x for x in eligibility(u) if x in ("restricted", "underage", "moderation_hold")]
    if blocking: return err(DateError(403, blocking[0], "You can't create a dating profile: " + blocking[0]))
    p, created = DatingProfile.objects.get_or_create(user=u, defaults={"bio": u.bio[:settings.DATE_BIO_MAX], "interests": ",".join(clean_interests(u.interests)), "city": u.city[:80]})
    return Response(ser_self(_mine(u)), status=201 if created else 200)

@api_view(["PATCH"])
def profile_update(request):
    u = request.user
    try: p = _mine(u)
    except DateError as e: return err(e)
    d = request.data
    try:
        if "bio" in d:
            if not isinstance(d["bio"], str) or len(d["bio"].strip()) > settings.DATE_BIO_MAX: raise DateError(400, "bad_bio", f"Bio must be text up to {settings.DATE_BIO_MAX} characters.")
            p.bio = d["bio"].strip()
        if "gender" in d:
            if d["gender"] not in GENDERS + [""]: raise DateError(400, "bad_gender", "Invalid gender option.")
            p.gender = d["gender"]
        if "interested_in" in d:
            v = d["interested_in"]; v = v if isinstance(v, list) else []
            if any(x not in INTEREST_OPTIONS for x in v): raise DateError(400, "bad_interested_in", "Invalid 'interested in' option.")
            p.interested_in = pack(sorted(set(v), key=INTEREST_OPTIONS.index))
        if "interests" in d: p.interests = ",".join(clean_interests(d["interests"]))
        if "intention" in d:
            if d["intention"] not in INTENTIONS + [""]: raise DateError(400, "bad_intention", "Invalid relationship intention.")
            p.intention = d["intention"]
        if "city" in d: p.city = str(d["city"]).strip()[:80]   # broad city text only; no coordinates are accepted or stored
        for k in ("min_age", "max_age"):
            if k in d:
                try: setattr(p, k, int(d[k]))
                except (TypeError, ValueError): raise DateError(400, "bad_age_range", "Ages must be numbers.")
        if not (18 <= p.min_age <= p.max_age <= 99): raise DateError(400, "bad_age_range", "Age range must be between 18 and 99, with min <= max.")
        if p.is_active and (not p.bio or not unpack(p.interested_in)): raise DateError(400, "required_while_active", "Deactivate your profile before clearing required details.")
    except DateError as e: return err(e)
    p.save(); return Response(ser_self(_mine(u)))   # note: nothing here touches the User row

@api_view(["POST"])
def profile_activate(request):
    u = request.user
    try:
        p = _mine(u); r = eligibility(u)
        if r: raise DateError(403, r[0], "You're not eligible for ZINGO Date: " + ", ".join(r), reasons=r)
        m = missing_fields(u, p)
        if m: raise DateError(400, "missing_fields", "Complete the missing details first.", missing=m)
    except DateError as e: return err(e)
    DatingProfile.objects.filter(pk=p.pk).update(is_active=True, activated_at=timezone.now()); return Response(ser_self(_mine(u)))

@api_view(["POST"])
def profile_deactivate(request):
    try: p = _mine(request.user)
    except DateError as e: return err(e)
    DatingProfile.objects.filter(pk=p.pk).update(is_active=False); return Response(ser_self(_mine(request.user)))

@api_view(["GET"])
def preferences(request):
    r = eligibility(request.user)
    return Response({"genders": GENDERS, "interested_in_options": INTEREST_OPTIONS, "intentions": INTENTIONS, "eligible": not r, "reasons": r,
                     "limits": {"min_age": 18, "max_age": 99, "max_photos": settings.DATE_MAX_PHOTOS, "photo_mb": settings.DATE_MAX_PHOTO_MB, "bio_max": settings.DATE_BIO_MAX, "likes_per_day": settings.DATE_MAX_LIKES_PER_DAY}})

# ---------- photos ----------
@api_view(["POST"])
@parser_classes([MultiPartParser])
@throttle_classes([DateThrottle])
def photo_upload(request):
    try: p = _mine(request.user)
    except DateError as e: return err(e)
    f = request.FILES.get("image")
    if not f: return err(DateError(400, "no_file", "Choose an image."))
    if p.photos.count() >= settings.DATE_MAX_PHOTOS: return err(DateError(400, "too_many", f"You can add up to {settings.DATE_MAX_PHOTOS} photos."))
    if (f.content_type or "") not in PHOTO_TYPES: return err(DateError(400, "bad_type", "Use a JPEG, PNG or WebP image."))
    if f.size > settings.DATE_MAX_PHOTO_MB * 1024 * 1024: return err(DateError(400, "too_big", f"Image must be under {settings.DATE_MAX_PHOTO_MB} MB."))
    try:
        img = Image.open(f); img.load()
        if img.width * img.height > 40_000_000: raise ValueError("too many pixels")
        img = ImageOps.exif_transpose(img).convert("RGB"); img.thumbnail((1600, 1600))
        buf = io.BytesIO(); img.save(buf, "JPEG", quality=85)       # re-encoding drops EXIF/GPS and any non-image payload
    except Exception: return err(DateError(400, "bad_image", "That file isn't a valid image."))
    ph = DatingPhoto.objects.create(profile=p, image=ContentFile(buf.getvalue(), name="x.jpg"), position=p.photos.count())
    return Response({"id": ph.id, "url": ph.image.url}, status=201)

@api_view(["DELETE"])
def photo_delete(request, pk):
    ph = DatingPhoto.objects.filter(pk=pk, profile__user=request.user).first()   # only your own; others' ids behave as not found
    if not ph: return Response({"detail": "Not found."}, status=404)
    ph.image.delete(save=False); ph.delete(); return Response(status=204)

# ---------- discovery & actions ----------
@api_view(["GET"])
def discover(request):
    try: vp = ready(request.user)
    except DateError as e: return err(e)
    pg = DatePager(); rows = pg.paginate_queryset(discover_qs(vp), request)
    vp.user.dating = vp
    return pg.get_paginated_response([ser_card(x, request.user) for x in rows])

@api_view(["GET"])
def profile_detail(request, username):
    try: vp = ready(request.user)
    except DateError as e: return err(e)
    t = DatingProfile.objects.filter(user__username=username, is_active=True).select_related("user").prefetch_related("photos").first()
    if not t or not visible_to(vp, t.user): return Response({"detail": "Not found."}, status=404)
    return Response(ser_card(t, request.user))

def ensure_conversation(a, b):
    conv, created = Conversation.objects.get_or_create(pair_key=pair_key(a, b), defaults={"origin": "date"})
    if created: ConversationParticipant.objects.bulk_create([ConversationParticipant(conversation=conv, user=a), ConversationParticipant(conversation=conv, user=b)])
    return conv

def ser_match(m, viewer):
    other = m.user_high if m.user_low_id == viewer.id else m.user_low
    p = DatingProfile.objects.filter(user=other).select_related("user").prefetch_related("photos").first()
    conv = Conversation.objects.filter(pair_key=m.pair_key).first()
    return {"username": other.username, "age": other.age(), "city": p.city if p else "", "photos": photos(p) if p else [], "interests": clean_interests(p.interests) if p else [],
            "conversation": conv.id if conv else None, "matched_at": m.created}

def act(user, username, kind):
    vp = ready(user)
    if kind == "like" and DatingLike.objects.filter(from_user=user, kind="like", created__gte=timezone.now() - timedelta(days=1)).count() >= settings.DATE_MAX_LIKES_PER_DAY:
        raise DateError(429, "daily_limit", "You've used your likes for today. Come back tomorrow.")
    target = User.objects.filter(username=username).first()
    if not target or target.id == user.id: raise DateError(404, "not_found", "Profile not available.")
    with transaction.atomic():
        list(DatingProfile.objects.select_for_update().filter(user_id__in=[user.id, target.id]).order_by("user_id"))  # serialise the pair so simultaneous likes can't miss each other
        if DatingLike.objects.filter(from_user=user, to_user=target).exists(): raise DateError(409, "already_acted", "You've already responded to this profile.")
        if not discover_qs(vp).filter(user=target).exists(): raise DateError(404, "not_found", "Profile not available.")
        try:
            with transaction.atomic(): DatingLike.objects.create(from_user=user, to_user=target, kind=kind)
        except IntegrityError: raise DateError(409, "already_acted", "You've already responded to this profile.")
        if kind == "like" and DatingLike.objects.filter(from_user=target, to_user=user, kind="like").exists():
            lo, hi = sorted([user, target], key=lambda x: x.id)
            m, created = DateMatch.objects.get_or_create(pair_key=pair_key(user, target), defaults={"user_low": lo, "user_high": hi})
            conv = ensure_conversation(user, target)
            if created:
                notify(target, "date_match", user, key=f"date_match:{m.id}", conversation=conv); notify(user, "date_match", target, key=f"date_match:{m.id}", conversation=conv)
            return {"matched": True, "match": ser_match(m, user)}
    return {"matched": False}

def _action(kind):
    @api_view(["POST"])
    @throttle_classes([DateThrottle])
    def view(request):
        try: return Response(act(request.user, request.data.get("username"), kind))
        except DateError as e: return err(e)
    return view
like_view, pass_view = _action("like"), _action("pass")

@api_view(["GET"])
def matches(request):
    qs = DateMatch.objects.filter(Q(user_low=request.user) | Q(user_high=request.user), active=True).select_related("user_low", "user_high")
    pg = DatePager(); rows = pg.paginate_queryset(qs, request)
    return pg.get_paginated_response([ser_match(m, request.user) for m in rows])

@api_view(["POST"])
@throttle_classes([DateThrottle])
def unmatch(request):
    t = User.objects.filter(username=request.data.get("username")).first(); m = active_match(request.user, t) if t else None
    if not m: return Response({"detail": "Not found."}, status=404)   # outsiders and non-matches get the same answer
    DateMatch.objects.filter(pk=m.pk).update(active=False, unmatched_by=request.user, unmatched_at=timezone.now())
    return Response({"unmatched": True})

def on_block(blocker, target):
    DateMatch.objects.filter(pair_key=pair_key(blocker, target), active=True).update(active=False, unmatched_by=blocker, unmatched_at=timezone.now())

@api_view(["POST"])
@throttle_classes([DateThrottle])
def report(request):
    try:
        vp = ready(request.user); reason = str(request.data.get("reason") or "").strip()
        t = User.objects.filter(username=request.data.get("username")).first()
        tp = DatingProfile.objects.filter(user=t).first() if t else None
        if not tp or not visible_to(vp, t): raise DateError(404, "not_found", "Profile not available.")   # can only report profiles you can actually see (anti brigading)
        if not reason: raise DateError(400, "reason_required", "Please describe the problem.")
    except DateError as e: return err(e)
    with transaction.atomic():
        Report.objects.create(reporter=request.user, user=t, category="dating", reason=reason[:300])
        DatingLike.objects.get_or_create(from_user=request.user, to_user=t, defaults={"kind": "pass"})   # reporter never sees them again
        since = timezone.now() - timedelta(days=7)
        if Report.objects.filter(user=t, category="dating", created__gte=since).values("reporter").distinct().count() >= settings.DATE_AUTO_HOLD_REPORTS:
            DatingProfile.objects.filter(pk=tp.pk).update(is_active=False, moderation_hold=True)   # staff review in admin lifts or confirms the hold
    return Response(status=201)
