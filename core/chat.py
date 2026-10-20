import logging
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Count, Exists, OuterRef, Q, Subquery
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.decorators import api_view, throttle_classes
from rest_framework.pagination import CursorPagination
from rest_framework.response import Response
from rest_framework.throttling import SimpleRateThrottle
from .models import *
from .notifications import notify
from .views import blocked_ids, visible_videos
log = logging.getLogger(__name__)

class ChatSendThrottle(SimpleRateThrottle):
    """Rate-limits message *sends* only (POST), per user. Rate comes from DEFAULT_THROTTLE_RATES['chat']."""
    scope = "chat"
    def get_cache_key(self, request, view):
        if request.method != "POST" or not request.user.is_authenticated: return None
        return self.cache_format % {"scope": self.scope, "ident": request.user.pk}

class ChatError(Exception):
    def __init__(self, status, msg): super().__init__(msg); self.status, self.msg = status, msg

def can_message(a, b, ignore_private=False):
    if a.id == b.id: return False, "You can't message yourself."
    if Block.objects.filter(Q(blocker=a, blocked=b) | Q(blocker=b, blocked=a)).exists(): return False, "You can't message this user."
    # No follow-approval system exists yet, so "connected" = either side follows the other. A ZINGO Date match is mutual consent, so it bypasses this.
    if b.is_private and not ignore_private and not Follow.objects.filter(Q(follower=a, followee=b) | Q(follower=b, followee=a)).exists():
        return False, "This account only accepts messages from people it's connected with."
    return True, ""

def membership(user, conv_id):
    try: cid = int(conv_id)
    except (TypeError, ValueError): raise ChatError(404, "Not found.")
    p = ConversationParticipant.objects.filter(user=user, conversation_id=cid).select_related("conversation").first()
    if not p: raise ChatError(404, "Not found.")  # never reveal whether the id exists
    if p.conversation.origin == "date" and not DateMatch.objects.filter(pair_key=p.conversation.pair_key, active=True).exists():
        raise ChatError(404, "Not found.")  # unmatched/blocked dating chats are no longer accessible to either side
    other = ConversationParticipant.objects.filter(conversation_id=cid).exclude(user=user).select_related("user").first()
    return p, other

def video_preview(m, ok=True):
    if m.kind != "video": return None
    v = m.video
    if v is None or v.status != "active" or not ok: return {"unavailable": True}
    return {"id": v.id, "caption": v.caption, "thumbnail": v.thumbnail.url if v.thumbnail else None, "file": v.file.url, "owner": v.owner.username}

def ser_message(m, visible=None):
    ok = True if visible is None or m.video_id is None else m.video_id in visible
    return {"id": m.id, "conversation": m.conversation_id, "sender": m.sender.username, "kind": m.kind, "text": m.text,
            "video": video_preview(m, ok), "created": m.created, "client_id": m.client_id}

def push(user_ids, payload):
    """Deliver to every connected socket of each participant. Failure never loses the message (it is already saved; REST/refetch recovers)."""
    try:
        layer = get_channel_layer()
        for uid in user_ids: async_to_sync(layer.group_send)(f"user_{uid}", {"type": "chat.message", "message": payload})
    except Exception: log.exception("chat push failed")

def send_message(user, conv_id, text, video_id=None, client_id=""):
    """Single entry point for REST and WebSocket. Returns (message, created)."""
    text = (text or "").strip() if isinstance(text, str) or text is None else None
    if text is None: raise ChatError(400, "Text must be a string.")
    if len(text) > settings.CHAT_MAX_LEN: raise ChatError(400, f"Message is too long (max {settings.CHAT_MAX_LEN}).")
    client_id = str(client_id or "")[:64]
    part, other = membership(user, conv_id)
    if other is None: raise ChatError(404, "Not found.")
    ok, why = can_message(user, other.user, ignore_private=part.conversation.origin == "date")
    if not ok: raise ChatError(403, why)
    video = None
    if video_id not in (None, ""):
        try: vid = int(video_id)
        except (TypeError, ValueError): raise ChatError(400, "Invalid video.")
        video = Video.objects.filter(id=vid).first()
        if not video or not all(visible_videos(w).filter(id=vid).exists() for w in (user, other.user)):
            raise ChatError(400, "That video can't be shared with this user.")  # sender and recipient must both be allowed to view it
    elif not text: raise ChatError(400, "Message is empty.")
    with transaction.atomic():
        if client_id:
            dup = Message.objects.filter(conversation_id=part.conversation_id, sender=user, client_id=client_id).select_related("sender", "video__owner").first()
            if dup: return dup, False
        try:
            with transaction.atomic():
                m = Message.objects.create(conversation_id=part.conversation_id, sender=user, text=text, video=video, kind="video" if video else "text", client_id=client_id)
        except IntegrityError:
            return Message.objects.select_related("sender", "video__owner").get(conversation_id=part.conversation_id, sender=user, client_id=client_id), False
        Conversation.objects.filter(pk=part.conversation_id).update(last_message=m, updated=m.created)
        ConversationParticipant.objects.filter(pk=part.pk).update(last_read_id=m.id)
        notify(other.user, "dm", user, key=f"dm:{part.conversation_id}", refresh=True, conversation_id=part.conversation_id)
    m = Message.objects.select_related("sender", "video__owner").get(pk=m.pk)
    payload = {**ser_message(m), "created": m.created.isoformat()}  # channel layers/JSON need plain types
    push([user.id, other.user_id], payload)  # after the transaction committed
    return m, True

# ---------- REST ----------
class ConvPager(CursorPagination): ordering = "-updated"; page_size = 20
class MsgPager(CursorPagination): ordering = "-id"; page_size = 30

def conv_qs(user):
    me = ConversationParticipant.objects.filter(conversation=OuterRef("pk"), user=user).values("last_read_id")[:1]
    live_match = DateMatch.objects.filter(pair_key=OuterRef("pair_key"), active=True)
    return (Conversation.objects.filter(participants__user=user).filter(Q(origin="direct") | Exists(live_match)).exclude(participants__user_id__in=blocked_ids(user))
            .annotate(unread=Count("messages", filter=~Q(messages__sender=user) & Q(messages__id__gt=Subquery(me)), distinct=True))
            .select_related("last_message__sender").prefetch_related("participants__user"))

def other_of(conv, user): return next(p.user for p in conv.participants.all() if p.user_id != user.id)

def ser_conv(c, user):
    o = other_of(c, user); lm = c.last_message
    return {"id": c.id, "other": {"id": o.id, "username": o.username}, "unread": c.unread, "updated": c.updated,
            "last_message": None if not lm else {"sender": lm.sender.username, "kind": lm.kind, "text": lm.text, "created": lm.created}}

def fail(e): return Response({"detail": e.msg}, status=e.status)

@api_view(["GET", "POST"])
def conversations(request):
    user = request.user
    if request.method == "GET":
        p = ConvPager(); rows = p.paginate_queryset(conv_qs(user), request)
        return p.get_paginated_response([ser_conv(c, user) for c in rows])
    target = User.objects.filter(username=request.data.get("username")).first()
    if not target: return Response({"detail": "User not found."}, status=404)
    ok, why = can_message(user, target)
    if not ok: return Response({"detail": why}, status=403)
    key = f"{min(user.id, target.id)}:{max(user.id, target.id)}"
    with transaction.atomic():
        conv, created = Conversation.objects.get_or_create(pair_key=key)
        if created: ConversationParticipant.objects.bulk_create([ConversationParticipant(conversation=conv, user=user), ConversationParticipant(conversation=conv, user=target)])
    return Response({"id": conv.id, "other": {"id": target.id, "username": target.username}}, status=201 if created else 200)

@api_view(["GET"])
def chat_unread(request):
    return Response({"count": conv_qs(request.user).filter(unread__gt=0).count()})

@api_view(["GET"])
def conversation_detail(request, pk):
    try: part, other = membership(request.user, pk)
    except ChatError as e: return fail(e)
    if other is None or other.user_id in blocked_ids(request.user): return Response({"detail": "Not found."}, status=404)
    ok, why = can_message(request.user, other.user, ignore_private=part.conversation.origin == "date")
    return Response({"id": part.conversation_id, "other": {"id": other.user.id, "username": other.user.username}, "other_last_read_id": other.last_read_id, "can_message": ok, "reason": why})

@api_view(["GET", "POST"])
@throttle_classes([ChatSendThrottle])
def messages(request, pk):
    user = request.user
    try: part, other = membership(user, pk)
    except ChatError as e: return fail(e)
    if request.method == "POST":
        try: m, created = send_message(user, pk, request.data.get("text"), request.data.get("video"), request.data.get("client_id"))
        except ChatError as e: return fail(e)
        return Response(ser_message(m), status=201 if created else 200)
    if other is None or other.user_id in blocked_ids(user): return Response({"detail": "Not found."}, status=404)
    p = MsgPager(); rows = p.paginate_queryset(Message.objects.filter(conversation_id=part.conversation_id).select_related("sender", "video__owner"), request)
    ids = [m.video_id for m in rows if m.video_id]
    vis = set(visible_videos(user).filter(id__in=ids).values_list("id", flat=True)) if ids else set()
    return p.get_paginated_response([ser_message(m, vis) for m in rows])

@api_view(["POST"])
def mark_conversation_read(request, pk):
    try: part, _ = membership(request.user, pk)
    except ChatError as e: return fail(e)
    last = Message.objects.filter(conversation_id=part.conversation_id).order_by("-id").values_list("id", flat=True).first() or 0
    ConversationParticipant.objects.filter(pk=part.pk, last_read_id__lt=last).update(last_read_id=last)
    Notification.objects.filter(user=request.user, kind="dm", conversation_id=part.conversation_id, read=False).update(read=True)
    return Response(status=204)
