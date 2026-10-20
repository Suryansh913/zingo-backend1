"""Read-only endpoints powering the Home page: stories row, suggestions, trending hashtags, search. All respect blocks/visibility."""
import re
from collections import Counter
from datetime import timedelta
from django.db.models import Count, Q
from django.utils import timezone
from rest_framework.decorators import api_view
from rest_framework.response import Response
from .models import *
from .serializers import VideoSerializer
from .views import blocked_ids, visible_videos

def _tags(s): return [t.lstrip("#").lower() for t in re.split(r"[\s,]+", s or "") if t.lstrip("#")]

@api_view(["GET"])
def home_stories(request):
    """'Stories' = ZINGS posted in the last 24 h by you and people you follow, grouped by creator. `seen` comes from real watch history."""
    u = request.user; since = timezone.now() - timedelta(hours=24)
    ids = set(Follow.objects.filter(follower=u).values_list("followee_id", flat=True)) | {u.id}
    vids = list(visible_videos(u).filter(owner_id__in=ids, created__gte=since).select_related("owner").order_by("created")[:300])
    seen = set(WatchHistory.objects.filter(user=u, video_id__in=[v.id for v in vids]).values_list("video_id", flat=True))
    groups = {}
    for v in vids:
        g = groups.setdefault(v.owner_id, {"username": v.owner.username, "is_me": v.owner_id == u.id, "videos": [], "latest": v.created})
        if len(g["videos"]) < 10:
            g["videos"].append({"id": v.id, "file": v.file.url, "thumbnail": v.thumbnail.url if v.thumbnail else None, "caption": v.caption, "created": v.created, "seen": v.id in seen})
        g["latest"] = v.created
    out = list(groups.values())
    for g in out: g["seen"] = all(x["seen"] for x in g["videos"])
    out.sort(key=lambda g: (not g["is_me"], g["seen"], -g["latest"].timestamp()))
    for g in out: g.pop("latest")
    return Response(out)

@api_view(["GET"])
def suggested_users(request):
    u = request.user
    skip = set(Follow.objects.filter(follower=u).values_list("followee_id", flat=True)) | blocked_ids(u) | {u.id}
    qs = (User.objects.exclude(id__in=skip).filter(videos__status="active", videos__visibility="public")
          .annotate(n=Count("follower_set", distinct=True)).order_by("-n", "-id").distinct()[:6])
    return Response([{"username": x.username, "bio": x.bio, "followers": x.n, "is_following": False} for x in qs])

@api_view(["GET"])
def trending_hashtags(request):
    since = timezone.now() - timedelta(days=7); c = Counter()
    for h in visible_videos(request.user).filter(created__gte=since, visibility="public").exclude(hashtags="").values_list("hashtags", flat=True)[:500]:
        c.update(set(_tags(h)))
    return Response([{"tag": t, "count": n} for t, n in c.most_common(8)])

@api_view(["GET"])
def search(request):
    u = request.user; q = (request.query_params.get("q") or "").strip()[:50]; term = q.lstrip("#").strip()
    if len(term) < 2: return Response({"q": q, "users": [], "videos": [], "hashtags": []})
    blk = blocked_ids(u)
    users = [] if q.startswith("#") else list(User.objects.filter(username__icontains=term).exclude(id__in=blk).annotate(n=Count("follower_set", distinct=True)).order_by("-n")[:10])
    following = set(Follow.objects.filter(follower=u, followee_id__in=[x.id for x in users]).values_list("followee_id", flat=True))
    vids = list(visible_videos(u).filter(Q(caption__icontains=term) | Q(hashtags__icontains=term)).select_related("owner").order_by("-created")[:24])
    c = Counter(t for v in vids for t in set(_tags(v.hashtags)) if term.lower() in t)
    return Response({"q": q, "users": [{"username": x.username, "bio": x.bio, "followers": x.n, "is_following": x.id in following} for x in users],
                     "videos": VideoSerializer(vids, many=True).data, "hashtags": [{"tag": t, "count": n} for t, n in c.most_common(8)]})
