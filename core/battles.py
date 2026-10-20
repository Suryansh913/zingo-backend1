from datetime import timedelta
from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.decorators import api_view
from rest_framework.pagination import CursorPagination
from rest_framework.response import Response
from .models import *
from .views import blocked_ids
from .notifications import notify

def err(msg, code=400): return Response({"detail": msg}, status=code)

def unavailable(e):
    return e.video is None or e.video.status != "active" or e.video.visibility != "public"

# ---------- state machine ----------
def finish(b):
    """Finalize an active battle (caller holds the lock). Tie => no winner. Unavailable video => forfeit."""
    entries = list(b.entries.select_related("video")); ok = [e for e in entries if not unavailable(e)]
    b.winner = None
    if not ok: b.result = "void"
    elif len(ok) == 1 and len(entries) == 2: b.result, b.winner = "forfeit", ok[0].user
    else:
        n = dict(b.votes.values_list("entry_id").annotate(n=Count("id")))
        top = max([n.get(e.id, 0) for e in entries] or [0])
        leaders = [e for e in entries if n.get(e.id, 0) == top]
        if top == 0 or len(leaders) != 1: b.result = "tie"
        else: b.result, b.winner = "winner", leaders[0].user
    b.status = "finished"; b.save()
    for e in entries: notify(e.user, "battle_result", key=f"battle_result:{b.id}", battle=b)

def settle(b):
    now = timezone.now()
    with transaction.atomic():
        b = Battle.objects.select_for_update().get(pk=b.pk)
        if b.status in ("open", "pending") and b.challenge_expires_at <= now: b.status = "expired"; b.save()
        elif b.status == "active" and b.ends_at <= now: finish(b)
    return b

def settle_due():
    now = timezone.now()
    for b in Battle.objects.filter(Q(status__in=["open", "pending"], challenge_expires_at__lte=now) | Q(status="active", ends_at__lte=now)): settle(b)

# ---------- serialization (the only place identities are exposed) ----------
def ser(b, user, blk=None):
    blk = blk if blk is not None else blocked_ids(user)
    entries = sorted(b.entries.all(), key=lambda e: e.id)
    if b.id % 2: entries.reverse()  # neutral ordering: first entry is not always the creator
    votes = list(b.votes.all()); total = len(votes); now = timezone.now()
    participant = user.id in (b.creator_id, b.opponent_id) or any(e.user_id == user.id for e in entries)
    revealed = (not b.blind) or b.status not in ("open", "pending", "active") or participant
    show_counts = b.status == "finished" or settings.BATTLE_LIVE_COUNTS
    n = {}; [n.__setitem__(v.entry_id, n.get(v.entry_id, 0) + 1) for v in votes]
    def ent(i, e):
        un = unavailable(e); vid = None if un else e.video
        d = {"id": e.id, "label": "AB"[i], "available": not un, "is_mine": e.user_id == user.id,
             "user": e.user.username if revealed else None,
             "video": None if un else {"file": vid.file.url, "thumbnail": vid.thumbnail.url if vid.thumbnail else None,
                                      "caption": vid.caption if revealed else "", "id": vid.id if revealed else None},
             "votes": None, "percent": None}
        if show_counts: d["votes"] = n.get(e.id, 0); d["percent"] = round(100 * n.get(e.id, 0) / total, 1) if total else 0.0
        return d
    pids = {e.user_id for e in entries} | {b.creator_id}
    blocked = bool(pids & blk)
    mine_vote = next((v.entry_id for v in votes if v.voter_id == user.id), None)
    live = b.status == "active" and b.starts_at <= now < b.ends_at
    is_creator, is_opp = user.id == b.creator_id, user.id == b.opponent_id
    pending_ok = b.challenge_expires_at > now
    return {
        "id": b.id, "title": b.title, "status": b.status, "blind": b.blind, "revealed": revealed, "result": b.result,
        "creator": b.creator.username if revealed else None,
        "opponent": (b.opponent.username if b.opponent else None) if revealed else None,
        "winner": b.winner.username if (b.winner and revealed) else None,
        "winner_entry": next((e.id for e in entries if b.winner_id and e.user_id == b.winner_id), None) if revealed else None,
        "voting_hours": b.voting_hours, "created": b.created, "starts_at": b.starts_at, "ends_at": b.ends_at,
        "challenge_expires_at": b.challenge_expires_at, "total_votes": total, "my_vote": mine_vote, "counts_hidden": not show_counts,
        "rematch_of": b.rematch_of_id, "entries": [ent(i, e) for i, e in enumerate(entries)],
        "viewer": {"is_participant": participant,
            "can_vote": live and not participant and not blocked and mine_vote is None and all(not unavailable(e) for e in entries) and len(entries) == 2,
            "can_accept": pending_ok and not blocked and ((b.status == "pending" and is_opp) or (b.status == "open" and not is_creator)),
            "can_decline": b.status == "pending" and is_opp and pending_ok,
            "can_withdraw": participant and (b.status in ("open", "pending", "active")),
            "can_rematch": b.status == "finished" and participant}}

class Pager(CursorPagination): ordering = "-created"; page_size = 10

def base_qs(user):
    blk = blocked_ids(user)
    return (Battle.objects.select_related("creator", "opponent", "winner").prefetch_related("entries__video", "entries__user", "votes")
            .exclude(creator_id__in=blk).exclude(opponent_id__in=blk)), blk

def page(request, qs, blk):
    p = Pager(); rows = p.paginate_queryset(qs, request)
    return p.get_paginated_response([ser(b, request.user, blk) for b in rows])

def valid_video(user, vid):
    v = Video.objects.filter(id=vid, owner=user).first()
    if not v: return None, "Choose one of your own videos."
    if v.status != "active" or v.visibility != "public": return None, "Battle videos must be public and not removed."
    return v, None

# ---------- endpoints ----------
@api_view(["GET", "POST"])
def battles(request):
    settle_due(); user = request.user
    if request.method == "GET":
        qs, blk = base_qs(user); st = request.query_params.get("status", "open")
        if st not in ("open", "active", "finished"): return err("status must be open, active or finished.")
        return page(request, qs.filter(status=st), blk)
    d = request.data; vid, e = valid_video(user, d.get("video"))
    if e: return err(e)
    try: hours = int(d.get("voting_hours") or settings.BATTLE_VOTING_HOURS_DEFAULT)
    except (TypeError, ValueError): return err("voting_hours must be a number.")
    if not settings.BATTLE_VOTING_HOURS_MIN <= hours <= settings.BATTLE_VOTING_HOURS_MAX:
        return err(f"voting_hours must be {settings.BATTLE_VOTING_HOURS_MIN}-{settings.BATTLE_VOTING_HOURS_MAX}.")
    opp = None
    if d.get("opponent"):
        opp = User.objects.filter(username=d["opponent"]).first()
        if not opp or opp == user or opp.id in blocked_ids(user): return err("You can't challenge that user.")
    with transaction.atomic():
        b = Battle.objects.create(creator=user, opponent=opp, title=str(d.get("title", ""))[:100], blind=bool(d.get("blind")), voting_hours=hours,
                                  status="pending" if opp else "open", challenge_expires_at=timezone.now() + timedelta(hours=settings.BATTLE_CHALLENGE_EXPIRY_HOURS),
                                  rematch_of_id=None)
        BattleEntry.objects.create(battle=b, user=user, video=vid)
        if opp: notify(opp, "battle_invite", user, key=f"battle_invite:{b.id}", battle=b)
    return Response(ser(Battle.objects.prefetch_related("entries__video", "entries__user", "votes").get(pk=b.pk), user), status=201)

def load(request, pk):
    """Fetch + settle + visibility check (blocked users get 404)."""
    settle_due(); qs, _ = base_qs(request.user); b = qs.filter(pk=pk).first()
    if not b:
        mine = Battle.objects.filter(pk=pk).filter(Q(creator=request.user) | Q(opponent=request.user)).exists()
        if not mine: return None
        b = Battle.objects.prefetch_related("entries__video", "entries__user", "votes").get(pk=pk)
    return b

@api_view(["GET"])
def mine(request):
    settle_due(); qs, blk = base_qs(request.user)
    return page(request, qs.filter(Q(creator=request.user) | Q(opponent=request.user)), blk)

@api_view(["GET"])
def user_history(request, username):
    settle_due(); u = get_object_or_404(User, username=username)
    if u.id in blocked_ids(request.user): return err("Not found.", 404)
    qs, blk = base_qs(request.user)
    return page(request, qs.filter(status="finished").filter(Q(creator=u) | Q(opponent=u)), blk)

@api_view(["GET"])
def detail(request, pk):
    b = load(request, pk)
    return Response(ser(b, request.user)) if b else err("Not found.", 404)

def locked(pk):  # re-read under lock inside a transaction
    return Battle.objects.select_for_update().get(pk=pk)

def fresh(pk, user):
    return Response(ser(Battle.objects.prefetch_related("entries__video", "entries__user", "votes").select_related("creator", "opponent", "winner").get(pk=pk), user))

@api_view(["POST"])
def accept(request, pk):
    b = load(request, pk)
    if not b: return err("Not found.", 404)
    user = request.user
    with transaction.atomic():
        b = locked(pk)
        if b.status not in ("open", "pending"): return err("This challenge is no longer available.", 409)
        if b.challenge_expires_at <= timezone.now(): return err("This challenge has expired.", 409)
        if b.status == "pending" and b.opponent_id != user.id: return err("This challenge isn't for you.", 403)
        if b.creator_id == user.id: return err("You can't accept your own challenge.", 403)
        if b.creator_id in blocked_ids(user): return err("Not allowed.", 403)
        vid, e = valid_video(user, request.data.get("video"))
        if e: return err(e)
        BattleEntry.objects.create(battle=b, user=user, video=vid)
        now = timezone.now(); b.opponent = user; b.status = "active"; b.starts_at = now; b.ends_at = now + timedelta(hours=b.voting_hours); b.save()
    return fresh(pk, user)

@api_view(["POST"])
def decline(request, pk):
    b = load(request, pk)
    if not b: return err("Not found.", 404)
    with transaction.atomic():
        b = locked(pk)
        if b.status != "pending" or b.opponent_id != request.user.id: return err("Nothing to decline.", 403 if b.opponent_id != request.user.id else 409)
        b.status = "declined"; b.save()
    return fresh(pk, request.user)

@api_view(["POST"])
def withdraw(request, pk):
    b = load(request, pk)
    if not b: return err("Not found.", 404)
    user = request.user
    with transaction.atomic():
        b = locked(pk)
        if user.id not in (b.creator_id, b.opponent_id): return err("Not a participant.", 403)
        if b.status in ("open", "pending"): b.status = "cancelled"; b.save()
        elif b.status == "active":  # forfeit: the other participant wins immediately
            b.status, b.result, b.winner_id = "finished", "forfeit", b.opponent_id if user.id == b.creator_id else b.creator_id; b.save()
            for who in (b.creator, b.opponent): notify(who, "battle_result", key=f"battle_result:{b.id}", battle=b)
        else: return err("Battle already ended.", 409)
    return fresh(pk, user)

@api_view(["POST"])
def vote(request, pk):
    b = load(request, pk)
    if not b: return err("Not found.", 404)
    user = request.user
    try:
        with transaction.atomic():
            b = locked(pk); now = timezone.now()
            if b.status != "active" or not (b.starts_at <= now < b.ends_at): return err("Voting is not open.", 409)
            entries = list(b.entries.select_related("video"))
            if user.id in {e.user_id for e in entries}: return err("Participants can't vote in their own battle.", 403)
            if {e.user_id for e in entries} & blocked_ids(user): return err("Not allowed.", 403)
            if any(unavailable(e) for e in entries): return err("A video in this battle is unavailable; voting is paused.", 409)
            entry = next((e for e in entries if str(e.id) == str(request.data.get("entry"))), None)
            if not entry: return err("Invalid entry.")
            BattleVote.objects.create(battle=b, voter=user, entry=entry)
    except IntegrityError: return err("You already voted.", 409)
    return fresh(pk, user)

@api_view(["POST"])
def rematch(request, pk):
    b = load(request, pk)
    if not b: return err("Not found.", 404)
    user = request.user
    if b.status != "finished" or user.id not in (b.creator_id, b.opponent_id): return err("Rematch isn't available.", 403)
    other = b.opponent if user.id == b.creator_id else b.creator
    if not other or other.id in blocked_ids(user): return err("Rematch isn't available.", 403)
    if Battle.objects.filter(rematch_of=b, status__in=["pending", "active"]).exists(): return err("A rematch is already in progress.", 409)
    vid, e = valid_video(user, request.data.get("video"))
    if e: return err(e)
    with transaction.atomic():
        nb = Battle.objects.create(creator=user, opponent=other, title=b.title, blind=b.blind, voting_hours=b.voting_hours, status="pending",
                                   challenge_expires_at=timezone.now() + timedelta(hours=settings.BATTLE_CHALLENGE_EXPIRY_HOURS), rematch_of=b)
        BattleEntry.objects.create(battle=nb, user=user, video=vid)
        notify(other, "battle_invite", user, key=f"battle_invite:{nb.id}", battle=nb)
    return fresh(nb.pk, user)
