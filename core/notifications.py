import re
from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.decorators import api_view
from rest_framework.pagination import CursorPagination
from rest_framework.response import Response
from .models import *

def _blocked(user):
    ids = set()
    for a, b in Block.objects.filter(Q(blocker=user) | Q(blocked=user)).values_list("blocker_id", "blocked_id"): ids |= {a, b}
    ids.discard(user.id); return ids

def notify(user, kind, actor=None, key="", refresh=False, **refs):
    """Create a notification for a real event. Call inside the transaction of the action so a rollback leaves nothing behind.
    `key` makes the event idempotent (e.g. unlike->like again does not notify twice). Never notifies self or blocked pairs."""
    if user is None: return None
    if actor is not None and (actor.pk == user.pk or actor.pk in _blocked(user)): return None
    if key:
        if refresh: Notification.objects.filter(user=user, dedupe_key=key).delete()
        n, created = Notification.objects.get_or_create(user=user, dedupe_key=key, defaults={"kind": kind, "actor": actor, **refs})
        return n if created else None
    return Notification.objects.create(user=user, kind=kind, actor=actor, **refs)

def notify_comment(c, video):
    done = {c.user_id}
    names = {n.rstrip(".") for n in re.findall(r"@([\w.@+-]+)", c.text)}
    for u in User.objects.filter(username__in=list(names)[:5]):
        if u.id not in done: notify(u, "mention", c.user, key=f"mention:{c.id}", video=video); done.add(u.id)
    if c.parent_id and c.parent.user_id not in done:
        notify(c.parent.user, "reply", c.user, key=f"reply:{c.id}", video=video); done.add(c.parent.user_id)
    if video.owner_id not in done: notify(video.owner, "comment", c.user, key=f"comment:{c.id}", video=video)

def settle_stories():
    """WHATNEXT result: once a poll's deadline passes, tell the creator and every voter exactly once."""
    for n in StoryNode.objects.filter(closes_at__lte=timezone.now(), result_notified=False).values_list("pk", flat=True)[:50]:
        with transaction.atomic():
            n = StoryNode.objects.select_for_update().select_related("story__owner").get(pk=n)
            if n.result_notified: continue
            n.result_notified = True; n.save(update_fields=["result_notified"])
            voters = [v for v in StoryVote.objects.filter(node=n).values_list("user_id", flat=True) if v != n.story.owner_id]
            if not voters: continue
            notify(n.story.owner, "story_result", key=f"story_result:{n.id}", story_node=n.id)
            Notification.objects.bulk_create([Notification(user_id=u, kind="story_result", dedupe_key=f"story_result:{n.id}", story_node=n.id) for u in voters], ignore_conflicts=True)

class NPager(CursorPagination): ordering = "-id"; page_size = 20

def ser(n): return {"id": n.id, "kind": n.kind, "actor": n.actor.username if n.actor else None, "read": n.read, "created": n.created,
                    "video": n.video_id, "conversation": n.conversation_id, "battle": n.battle_id, "story_node": n.story_node}

@api_view(["GET"])
def notifications(request):
    from .battles import settle_due
    settle_due(); settle_stories()
    qs = Notification.objects.filter(user=request.user).exclude(actor_id__in=_blocked(request.user)).select_related("actor")
    if request.query_params.get("unread") == "1": qs = qs.filter(read=False)
    p = NPager(); rows = p.paginate_queryset(qs, request)
    return p.get_paginated_response([ser(n) for n in rows])

@api_view(["GET"])
def unread_count(request):
    from .battles import settle_due
    settle_due(); settle_stories()
    return Response({"count": Notification.objects.filter(user=request.user, read=False).exclude(actor_id__in=_blocked(request.user)).count()})

@api_view(["POST"])
def mark_read(request, pk):
    n = get_object_or_404(Notification, pk=pk, user=request.user)  # other users' ids => 404
    if not n.read: n.read = True; n.save(update_fields=["read"])
    return Response(ser(n))

@api_view(["POST"])
def mark_all_read(request):
    return Response({"updated": Notification.objects.filter(user=request.user, read=False).update(read=True)})
