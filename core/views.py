from datetime import timedelta
from django.db import IntegrityError, transaction
from django.db.models import Subquery, Count, Exists, OuterRef, Q, F, Sum
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.authtoken.models import Token
from rest_framework.decorators import api_view, permission_classes, action
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from .models import *
from .serializers import *
from .util import clean_interests
from .notifications import notify, notify_comment, settle_stories

def blocked_ids(user):
    a = Block.objects.filter(blocker=user).values_list("blocked_id", flat=True)
    b = Block.objects.filter(blocked=user).values_list("blocker_id", flat=True)
    return set(a) | set(b)

def visible_videos(user):
    followed = Follow.objects.filter(follower=user).values_list("followee_id", flat=True)
    q = Q(visibility="public") | Q(owner=user) | Q(visibility="followers", owner_id__in=followed)
    # videos in a live blind battle are hidden from everyone but their owner so identities can't be matched
    hidden = BattleEntry.objects.filter(battle__blind=True, battle__status__in=["open", "pending", "active"]).values("video_id")
    return (Video.objects.filter(q, status="active").exclude(owner_id__in=blocked_ids(user))
            .exclude(Q(id__in=hidden) & ~Q(owner=user))
            .annotate(like_count=Count("likes", distinct=True), comment_count=Count("comments", distinct=True),
                      liked=Exists(Like.objects.filter(user=user, video=OuterRef("pk"))),
                      owner_following=Exists(Follow.objects.filter(follower=user, followee=OuterRef("owner_id"))),
                      story_root=Subquery(StoryNode.objects.filter(video=OuterRef("pk"), is_root=True).values("id")[:1])))

@api_view(["POST"])
@permission_classes([AllowAny])
def register(request):
    s = RegisterSerializer(data=request.data); s.is_valid(raise_exception=True); u = s.save()
    return Response({"token": Token.objects.create(user=u).key}, status=201)

@api_view(["POST"])
@permission_classes([AllowAny])
def login(request):
    from django.contrib.auth import authenticate
    u = authenticate(username=request.data.get("username"), password=request.data.get("password"))
    if not u: return Response({"detail": "Invalid credentials."}, status=400)
    return Response({"token": Token.objects.get_or_create(user=u)[0].key})

@api_view(["POST"])
def logout(request):
    Token.objects.filter(user=request.user).delete(); return Response(status=204)

@api_view(["GET", "PATCH"])
def me(request):
    u = request.user
    if request.method == "PATCH":
        d = request.data
        if "bio" in d: u.bio = str(d["bio"])[:300]
        if "city" in d: u.city = str(d["city"])[:80]
        if "is_private" in d: u.is_private = d["is_private"] in (True, "true", "True", 1, "1")
        if d.get("date_of_birth"):
            try: dob = serializers.DateField().to_internal_value(d["date_of_birth"])
            except serializers.ValidationError: return Response({"detail": "Invalid date of birth."}, status=400)
            if dob > timezone.now().date(): return Response({"detail": "Invalid date of birth."}, status=400)
            if u.date_of_birth and u.date_of_birth != dob:  # stops users flipping their declared age to unlock adult features
                return Response({"detail": "Date of birth can't be changed once set. Contact support."}, status=400)
            u.date_of_birth = dob
        if "interests" in d: u.interests = ",".join(clean_interests(d["interests"]))
        if "language" in d: u.language = str(d["language"]).strip().lower()[:10]
        u.save()
    return Response({**PublicUserSerializer(u).data, "email": u.email, "age": u.age(), "date_of_birth": u.date_of_birth,
                     "interests": clean_interests(u.interests), "language": u.language})

@api_view(["GET"])
def profile(request, username):
    u = get_object_or_404(User, username=username)
    if u.id in blocked_ids(request.user): return Response(status=404)
    return Response({**PublicUserSerializer(u).data, "is_me": u == request.user,
                     "is_following": Follow.objects.filter(follower=request.user, followee=u).exists()})

@api_view(["POST", "DELETE"])
def follow(request, username):
    t = get_object_or_404(User, username=username)
    if t == request.user or t.id in blocked_ids(request.user): return Response({"detail": "Not allowed."}, status=400)
    if request.method == "POST":
        with transaction.atomic():
            _, created = Follow.objects.get_or_create(follower=request.user, followee=t)
            if created: notify(t, "follow", request.user, key=f"follow:{request.user.id}")
    else: Follow.objects.filter(follower=request.user, followee=t).delete()
    return Response(status=204)

@api_view(["POST", "DELETE"])
def block(request, username):
    t = get_object_or_404(User, username=username)
    if t == request.user: return Response(status=400)
    if request.method == "POST":
        Block.objects.get_or_create(blocker=request.user, blocked=t)
        from .match import on_block; on_block(request.user, t)
        from .date import on_block as date_block; date_block(request.user, t)  # blocking ends any live match with that person
        Follow.objects.filter(Q(follower=request.user, followee=t) | Q(follower=t, followee=request.user)).delete()
    else: Block.objects.filter(blocker=request.user, blocked=t).delete()
    return Response(status=204)

@api_view(["POST"])
def report(request):
    v = request.data.get("video"); u = request.data.get("user"); mid = request.data.get("message")
    if not (v or u or mid) or not request.data.get("reason"): return Response({"detail": "video/user/message and reason required."}, status=400)
    if mid:  # only participants can report a message (and thereby expose it to moderators)
        msg = Message.objects.filter(id=mid, conversation__participants__user=request.user).first()
        if not msg: return Response({"detail": "Not found."}, status=404)
        Report.objects.create(reporter=request.user, message=msg, user=msg.sender, reason=request.data["reason"][:300]); return Response(status=201)
    Report.objects.create(reporter=request.user, video_id=v or None, user_id=u or None, reason=request.data["reason"][:300])
    return Response(status=201)

class VideoViewSet(viewsets.ModelViewSet):
    serializer_class = VideoSerializer; ordering = "-created"
    def get_queryset(self):
        qs = visible_videos(self.request.user)
        if self.request.query_params.get("following") == "1":
            qs = qs.filter(owner_id__in=Follow.objects.filter(follower=self.request.user).values("followee_id"))
        if self.request.query_params.get("owner"): qs = qs.filter(owner__username=self.request.query_params["owner"])
        if self.action in ("update", "partial_update", "destroy"): qs = qs.filter(owner=self.request.user)
        return qs
    def perform_create(self, s): s.save(owner=self.request.user)

    @action(detail=True, methods=["post", "delete"])
    def like(self, request, pk=None):
        v = self.get_object()
        if request.method == "POST":
            with transaction.atomic():
                _, created = Like.objects.get_or_create(user=request.user, video=v)
                if created: notify(v.owner, "like", request.user, key=f"like:{request.user.id}:{v.id}", video=v)
        else: Like.objects.filter(user=request.user, video=v).delete()
        return Response({"likes": v.likes.count()})

    @action(detail=True, methods=["get", "post"])
    def comments(self, request, pk=None):
        v = self.get_object()
        if request.method == "POST":
            p = request.data.get("parent")
            if p and not v.comments.filter(id=p).exists(): return Response({"detail": "Bad parent."}, status=400)
            s = CommentSerializer(data=request.data); s.is_valid(raise_exception=True)
            with transaction.atomic():
                c = s.save(user=request.user, video=v); notify_comment(c, v)
            return Response(s.data, status=201)
        return Response(CommentSerializer(v.comments.select_related("user")[:100], many=True).data)

    @action(detail=True, methods=["post"])
    def watch(self, request, pk=None):
        v = self.get_object()
        WatchHistory.objects.update_or_create(user=request.user, video=v, defaults={
            "seconds": float(request.data.get("seconds", 0)), "completed": bool(request.data.get("completed", False))})
        return Response(status=204)

    @action(detail=False)
    def feed(self, request):
        """For You: simple heuristic ranking (see README). Mood filter via ?mood=."""
        u = request.user
        qs = self.get_queryset().exclude(owner=u).exclude(watchhistory__user=u, watchhistory__completed=True)
        if request.query_params.get("mood"): qs = qs.filter(mood=request.query_params["mood"])
        followed = Follow.objects.filter(follower=u, followee=OuterRef("owner_id"))
        qs = qs.annotate(is_followed=Exists(followed)).order_by("-is_followed", "-like_count", "-created")
        try: off = max(0, int(request.query_params.get("offset", 0)))
        except ValueError: off = 0
        rows = list(qs[off:off + 11])  # fetch one extra to know if more exist
        return Response({"results": VideoSerializer(rows[:10], many=True).data, "next_offset": off + 10 if len(rows) > 10 else None})

    @action(detail=False)
    def explore(self, request):
        """Most-liked public videos of the last 30 days (not personalised)."""
        qs = self.get_queryset().filter(created__gte=timezone.now() - timedelta(days=30), visibility="public").order_by("-like_count", "-created")[:30]
        return Response(VideoSerializer(qs, many=True).data)

    @action(detail=False)
    def history(self, request):
        ids = WatchHistory.objects.filter(user=request.user).order_by("-updated").values_list("video_id", flat=True)[:50]
        return Response(VideoSerializer(self.get_queryset().filter(id__in=ids), many=True).data)

# ---- WHATNEXT ----
def node_state(node, user):
    opts = list(node.options.annotate(vote_total=Count("votes")))
    total = sum(o.vote_total for o in opts)
    for o in opts: o.percent = round(100 * o.vote_total / total, 1) if total else 0.0
    open_ = node.closes_at is None or node.closes_at > timezone.now()
    mine = StoryVote.objects.filter(user=user, node=node).values_list("option_id", flat=True).first()
    top = max(opts, key=lambda o: o.vote_total).id if total and not open_ else None
    return {"node": node.id, "story": node.story_id, "closes_at": node.closes_at, "prompt": node.prompt, "video": node.video_id, "video_available": node.video is not None,
            "open": open_, "total_votes": total, "my_vote": mine, "winner": top, "options": OptionSerializer(opts, many=True).data}

def story_list(request):
    """Recent WHATNEXT stories (root node + its video), limited to videos the viewer may see."""
    u = request.user
    nodes = list(StoryNode.objects.filter(is_root=True, video__isnull=False).select_related("story__owner", "video").annotate(tv=Count("votes")).order_by("-id")[:60])
    ok = set(visible_videos(u).filter(id__in=[n.video_id for n in nodes]).values_list("id", flat=True)); now = timezone.now()
    return Response([{"story": n.story_id, "title": n.story.title, "node": n.id, "owner": n.story.owner.username, "prompt": n.prompt,
                      "open": n.closes_at is None or n.closes_at > now, "total_votes": n.tv, "created": n.story.created,
                      "video": {"id": n.video_id, "file": n.video.file.url, "thumbnail": n.video.thumbnail.url if n.video.thumbnail else None}} for n in nodes if n.video_id in ok])

@api_view(["GET", "POST"])
def story_create(request):
    if request.method == "GET": return story_list(request)
    d = request.data; labels = d.get("options", [])
    if not (2 <= len(labels) <= 4): return Response({"detail": "2-4 options required."}, status=400)
    vid = get_object_or_404(Video, id=d.get("video"), owner=request.user)
    with transaction.atomic():
        s = Story.objects.create(owner=request.user, title=d.get("title", "")[:120])
        n = StoryNode.objects.create(story=s, video=vid, prompt=d.get("prompt", ""), closes_at=d.get("closes_at"), is_root=True)
        StoryOption.objects.bulk_create([StoryOption(node=n, label=l[:100]) for l in labels])
    return Response({"story": s.id, "root": n.id}, status=201)

@api_view(["GET"])
def story_node(request, node_id):
    settle_stories()
    n = get_object_or_404(StoryNode.objects.select_related("video"), id=node_id)
    return Response(node_state(n, request.user))

@api_view(["POST"])
def story_vote(request, node_id):
    n = get_object_or_404(StoryNode, id=node_id)
    if n.closes_at and n.closes_at <= timezone.now(): return Response({"detail": "Voting closed."}, status=400)
    o = get_object_or_404(StoryOption, id=request.data.get("option"), node=n)
    try:
        with transaction.atomic(): StoryVote.objects.create(user=request.user, node=n, option=o)
    except IntegrityError: return Response({"detail": "Already voted."}, status=409)
    return Response(node_state(n, request.user), status=201)

@api_view(["POST"])
def story_link(request, option_id):
    """Creator attaches a follow-up video to a choice."""
    o = get_object_or_404(StoryOption.objects.select_related("node__story"), id=option_id, node__story__owner=request.user)
    vid = get_object_or_404(Video, id=request.data.get("video"), owner=request.user)
    with transaction.atomic():
        nxt = StoryNode.objects.create(story=o.node.story, video=vid, prompt=request.data.get("prompt", ""))
        for l in request.data.get("options", []): StoryOption.objects.create(node=nxt, label=l[:100])
        o.next_node = nxt; o.save()
    return Response({"node": nxt.id}, status=201)

@api_view(["GET"])
def story_tree(request, story_id):
    s = get_object_or_404(Story, id=story_id)
    nodes = s.nodes.prefetch_related("options")
    return Response({"story": s.id, "title": s.title, "nodes": [
        {"id": n.id, "root": n.is_root, "video": n.video_id, "options": [{"id": o.id, "label": o.label, "next": o.next_node_id} for o in n.options.all()]}
        for n in nodes]})
