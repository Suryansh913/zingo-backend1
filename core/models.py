import os, uuid
from datetime import date
from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils import timezone
from django.db.models import Q, F

class User(AbstractUser):
    bio = models.CharField(max_length=300, blank=True)
    date_of_birth = models.DateField(null=True, blank=True)
    city = models.CharField(max_length=80, blank=True)
    is_private = models.BooleanField(default=False)
    interests = models.CharField(max_length=250, blank=True)  # comma-separated tags
    language = models.CharField(max_length=10, blank=True)
    age_verified = models.BooleanField(default=False)  # set ONLY by a real age-assurance provider/admin; never by the user
    match_consent_version = models.PositiveSmallIntegerField(default=0)
    match_banned_until = models.DateTimeField(null=True, blank=True)
    def age(self):
        if not self.date_of_birth: return None
        t = date.today(); d = self.date_of_birth
        return t.year - d.year - ((t.month, t.day) < (d.month, d.day))

U = settings.AUTH_USER_MODEL

class Follow(models.Model):
    follower = models.ForeignKey(U, on_delete=models.CASCADE, related_name="following_set")
    followee = models.ForeignKey(U, on_delete=models.CASCADE, related_name="follower_set")
    created = models.DateTimeField(auto_now_add=True)
    class Meta:
        constraints = [models.UniqueConstraint(fields=["follower", "followee"], name="uniq_follow"),
                       models.CheckConstraint(condition=~Q(follower=F("followee")), name="no_self_follow")]

class Block(models.Model):
    blocker = models.ForeignKey(U, on_delete=models.CASCADE, related_name="blocks")
    blocked = models.ForeignKey(U, on_delete=models.CASCADE, related_name="blocked_by")
    class Meta:
        constraints = [models.UniqueConstraint(fields=["blocker", "blocked"], name="uniq_block")]

def _rand_path(folder):
    def f(instance, filename):  # random names so filenames can't identify creators (blind battles)
        return f"{folder}/{uuid.uuid4().hex}{os.path.splitext(filename)[1].lower()[:6]}"
    return f
def video_path(i, fn): return _rand_path("videos")(i, fn)
def thumb_path(i, fn): return _rand_path("thumbs")(i, fn)

class Video(models.Model):
    MOODS = ["happy", "chill", "romantic", "sad", "motivated", "funny", "adventure", "study", "party", "random"]
    owner = models.ForeignKey(U, on_delete=models.CASCADE, related_name="videos")
    file = models.FileField(upload_to=video_path)
    thumbnail = models.ImageField(upload_to=thumb_path, blank=True)
    caption = models.CharField(max_length=300, blank=True)
    hashtags = models.CharField(max_length=200, blank=True)
    mood = models.CharField(max_length=20, blank=True, choices=[(m, m) for m in MOODS])
    visibility = models.CharField(max_length=10, default="public", choices=[("public", "public"), ("followers", "followers"), ("private", "private")])
    status = models.CharField(max_length=10, default="active", choices=[("active", "active"), ("removed", "removed")])
    created = models.DateTimeField(auto_now_add=True, db_index=True)
    class Meta: indexes = [models.Index(fields=["status", "visibility", "-created"])]

class Like(models.Model):
    user = models.ForeignKey(U, on_delete=models.CASCADE)
    video = models.ForeignKey(Video, on_delete=models.CASCADE, related_name="likes")
    class Meta: constraints = [models.UniqueConstraint(fields=["user", "video"], name="uniq_like")]

class Comment(models.Model):
    user = models.ForeignKey(U, on_delete=models.CASCADE)
    video = models.ForeignKey(Video, on_delete=models.CASCADE, related_name="comments")
    parent = models.ForeignKey("self", null=True, blank=True, on_delete=models.CASCADE, related_name="replies")
    text = models.CharField(max_length=500)
    created = models.DateTimeField(auto_now_add=True)

class WatchHistory(models.Model):
    user = models.ForeignKey(U, on_delete=models.CASCADE)
    video = models.ForeignKey(Video, on_delete=models.CASCADE)
    seconds = models.FloatField(default=0); completed = models.BooleanField(default=False)
    updated = models.DateTimeField(auto_now=True)
    class Meta: constraints = [models.UniqueConstraint(fields=["user", "video"], name="uniq_watch")]

class Report(models.Model):
    reporter = models.ForeignKey(U, on_delete=models.CASCADE)
    video = models.ForeignKey(Video, null=True, blank=True, on_delete=models.CASCADE)
    user = models.ForeignKey(U, null=True, blank=True, on_delete=models.CASCADE, related_name="reports_against")
    reason = models.CharField(max_length=300)
    resolved = models.BooleanField(default=False)
    message = models.ForeignKey("Message", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    session = models.ForeignKey("MatchSession", null=True, blank=True, on_delete=models.SET_NULL, related_name="reports")
    category = models.CharField(max_length=20, blank=True)
    created = models.DateTimeField(auto_now_add=True)

# ---- WHATNEXT ----
class Story(models.Model):
    owner = models.ForeignKey(U, on_delete=models.CASCADE)
    title = models.CharField(max_length=120)
    created = models.DateTimeField(auto_now_add=True)

class StoryNode(models.Model):
    story = models.ForeignKey(Story, on_delete=models.CASCADE, related_name="nodes")
    video = models.ForeignKey(Video, null=True, on_delete=models.SET_NULL)  # SET_NULL: deleted video => unavailable branch
    prompt = models.CharField(max_length=200, blank=True)
    closes_at = models.DateTimeField(null=True, blank=True)
    is_root = models.BooleanField(default=False)
    result_notified = models.BooleanField(default=False)

class StoryOption(models.Model):
    node = models.ForeignKey(StoryNode, on_delete=models.CASCADE, related_name="options")
    label = models.CharField(max_length=100)
    next_node = models.ForeignKey(StoryNode, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

class StoryVote(models.Model):
    user = models.ForeignKey(U, on_delete=models.CASCADE)
    node = models.ForeignKey(StoryNode, on_delete=models.CASCADE, related_name="votes")
    option = models.ForeignKey(StoryOption, on_delete=models.CASCADE, related_name="votes")
    class Meta: constraints = [models.UniqueConstraint(fields=["user", "node"], name="one_vote_per_node")]


# ---- BATTLES ----
class Battle(models.Model):
    STATUS = ["open", "pending", "active", "finished", "declined", "cancelled", "expired"]
    creator = models.ForeignKey(U, on_delete=models.CASCADE, related_name="battles_created")
    opponent = models.ForeignKey(U, null=True, blank=True, on_delete=models.SET_NULL, related_name="battles_invited")
    title = models.CharField(max_length=100, blank=True)
    status = models.CharField(max_length=10, default="pending", choices=[(x, x) for x in STATUS], db_index=True)
    blind = models.BooleanField(default=False)
    voting_hours = models.PositiveIntegerField(default=24)
    challenge_expires_at = models.DateTimeField()
    starts_at = models.DateTimeField(null=True, blank=True)
    ends_at = models.DateTimeField(null=True, blank=True)
    result = models.CharField(max_length=10, blank=True, choices=[(x, x) for x in ("winner", "tie", "forfeit", "void")])
    winner = models.ForeignKey(U, null=True, blank=True, on_delete=models.SET_NULL, related_name="battles_won")
    rematch_of = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL, related_name="rematches")
    created = models.DateTimeField(auto_now_add=True, db_index=True)
    class Meta:
        constraints = [models.CheckConstraint(condition=~Q(creator=F("opponent")), name="battle_no_self")]
        indexes = [models.Index(fields=["status", "-created"])]

class BattleEntry(models.Model):
    battle = models.ForeignKey(Battle, on_delete=models.CASCADE, related_name="entries")
    user = models.ForeignKey(U, on_delete=models.CASCADE)
    video = models.ForeignKey(Video, null=True, on_delete=models.SET_NULL)  # SET_NULL: deleted video => forfeit at settle
    class Meta: constraints = [models.UniqueConstraint(fields=["battle", "user"], name="uniq_entry_per_user")]

class BattleVote(models.Model):
    battle = models.ForeignKey(Battle, on_delete=models.CASCADE, related_name="votes")
    voter = models.ForeignKey(U, on_delete=models.CASCADE)
    entry = models.ForeignKey(BattleEntry, on_delete=models.CASCADE, related_name="votes")
    created = models.DateTimeField(auto_now_add=True)
    class Meta: constraints = [models.UniqueConstraint(fields=["battle", "voter"], name="one_vote_per_battle")]


# ---- CHAT ----
class Conversation(models.Model):
    pair_key = models.CharField(max_length=40, unique=True)  # "minUserId:maxUserId" => one conversation per pair
    origin = models.CharField(max_length=10, default="direct")  # "date" = created by a ZINGO Date match; only accessible while that match is active
    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(default=timezone.now, db_index=True)
    last_message = models.ForeignKey("Message", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

class ConversationParticipant(models.Model):
    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, related_name="participants")
    user = models.ForeignKey(U, on_delete=models.CASCADE, related_name="conversations")
    last_read_id = models.PositiveBigIntegerField(default=0)
    class Meta: constraints = [models.UniqueConstraint(fields=["conversation", "user"], name="uniq_participant")]

class Message(models.Model):
    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, related_name="messages")
    sender = models.ForeignKey(U, on_delete=models.CASCADE, related_name="+")
    kind = models.CharField(max_length=10, default="text", choices=[("text", "text"), ("video", "video")])
    text = models.CharField(max_length=1000, blank=True)
    video = models.ForeignKey(Video, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    client_id = models.CharField(max_length=64, blank=True, default="")  # idempotency key: resend/reconnect never duplicates
    created = models.DateTimeField(auto_now_add=True)
    class Meta:
        indexes = [models.Index(fields=["conversation", "-id"])]
        constraints = [models.UniqueConstraint(fields=["conversation", "sender", "client_id"], condition=~Q(client_id=""), name="uniq_client_msg")]

# ---- NOTIFICATIONS ----
class Notification(models.Model):
    user = models.ForeignKey(U, on_delete=models.CASCADE, related_name="notifications")
    actor = models.ForeignKey(U, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    kind = models.CharField(max_length=20)
    video = models.ForeignKey(Video, null=True, blank=True, on_delete=models.CASCADE, related_name="+")
    conversation = models.ForeignKey(Conversation, null=True, blank=True, on_delete=models.CASCADE, related_name="+")
    battle = models.ForeignKey(Battle, null=True, blank=True, on_delete=models.CASCADE, related_name="+")
    story_node = models.IntegerField(null=True, blank=True)
    dedupe_key = models.CharField(max_length=80, blank=True, default="")
    read = models.BooleanField(default=False)
    created = models.DateTimeField(auto_now_add=True)
    class Meta:
        indexes = [models.Index(fields=["user", "read", "-id"])]
        constraints = [models.UniqueConstraint(fields=["user", "dedupe_key"], condition=~Q(dedupe_key=""), name="uniq_notif_event")]


# ---- ZINGO MATCH ----
class MatchTicket(models.Model):
    """Shared (DB) matchmaking queue entry; one per user. Not process-local, so multiple workers are safe."""
    user = models.OneToOneField(U, on_delete=models.CASCADE, related_name="match_ticket")
    mode = models.CharField(max_length=10)
    interests = models.CharField(max_length=250, blank=True)
    language = models.CharField(max_length=10, blank=True)
    created = models.DateTimeField(default=timezone.now, db_index=True)
    expires_at = models.DateTimeField(db_index=True)

class MatchSession(models.Model):
    STATUS = ["pending", "active", "ended", "declined", "expired"]
    user_a = models.ForeignKey(U, on_delete=models.CASCADE, related_name="+")  # caller (creates the WebRTC offer)
    user_b = models.ForeignKey(U, on_delete=models.CASCADE, related_name="+")  # callee
    mode = models.CharField(max_length=10)
    status = models.CharField(max_length=10, default="pending", choices=[(x, x) for x in STATUS], db_index=True)
    a_accepted = models.BooleanField(default=False); b_accepted = models.BooleanField(default=False)
    shared_interests = models.CharField(max_length=250, blank=True)
    accept_deadline = models.DateTimeField()
    created = models.DateTimeField(auto_now_add=True, db_index=True)
    started_at = models.DateTimeField(null=True, blank=True); ended_at = models.DateTimeField(null=True, blank=True)
    end_reason = models.CharField(max_length=16, blank=True)
    ended_by = models.ForeignKey(U, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    a_seen = models.DateTimeField(null=True, blank=True); b_seen = models.DateTimeField(null=True, blank=True)  # heartbeats
    class Meta:
        constraints = [models.CheckConstraint(condition=~Q(user_a=F("user_b")), name="match_no_self")]

class ActiveMatch(models.Model):
    """One row per user in a live session. UNIQUE(user) is the database-level guarantee that nobody is in two sessions."""
    user = models.OneToOneField(U, on_delete=models.CASCADE, related_name="active_match")
    session = models.ForeignKey(MatchSession, on_delete=models.CASCADE, related_name="slots")


# ---- ZINGO DATE ----
def date_photo_path(i, fn): return f"date/{uuid.uuid4().hex}.jpg"  # random name, always re-encoded JPEG (EXIF/GPS stripped)

class DatingProfile(models.Model):
    """Separate from the social profile: created as an inactive draft, discoverable only after the user explicitly activates it."""
    user = models.OneToOneField(U, on_delete=models.CASCADE, related_name="dating")
    bio = models.CharField(max_length=500, blank=True)
    gender = models.CharField(max_length=12, blank=True)
    interested_in = models.CharField(max_length=60, blank=True)   # packed ",woman,man," (delimiters make substring queries exact)
    interests = models.CharField(max_length=250, blank=True)      # comma-separated tags
    intention = models.CharField(max_length=12, blank=True)
    city = models.CharField(max_length=80, blank=True)            # broad city only; never coordinates
    min_age = models.PositiveSmallIntegerField(default=18); max_age = models.PositiveSmallIntegerField(default=99)
    is_active = models.BooleanField(default=False, db_index=True)
    activated_at = models.DateTimeField(null=True, blank=True)
    moderation_hold = models.BooleanField(default=False)          # set by repeated reports / staff; blocks discovery and activation
    created = models.DateTimeField(auto_now_add=True); updated = models.DateTimeField(auto_now=True)
    class Meta:
        constraints = [models.CheckConstraint(condition=Q(min_age__gte=18, max_age__lte=99) & Q(max_age__gte=F("min_age")), name="date_age_range")]
        indexes = [models.Index(fields=["is_active", "-id"])]

class DatingPhoto(models.Model):
    profile = models.ForeignKey(DatingProfile, on_delete=models.CASCADE, related_name="photos")
    image = models.ImageField(upload_to=date_photo_path)
    position = models.PositiveSmallIntegerField(default=0); created = models.DateTimeField(auto_now_add=True)
    class Meta: ordering = ["position", "id"]

class DatingLike(models.Model):
    from_user = models.ForeignKey(U, on_delete=models.CASCADE, related_name="+"); to_user = models.ForeignKey(U, on_delete=models.CASCADE, related_name="+")
    kind = models.CharField(max_length=4, choices=[("like", "like"), ("pass", "pass")]); created = models.DateTimeField(auto_now_add=True)
    class Meta:
        constraints = [models.UniqueConstraint(fields=["from_user", "to_user"], name="uniq_date_action"), models.CheckConstraint(condition=~Q(from_user=F("to_user")), name="date_no_self")]
        indexes = [models.Index(fields=["to_user", "kind"])]

class DateMatch(models.Model):
    """One row per pair, ever (unique pair_key). Unmatching/blocking sets active=False; the pair never re-enters discovery."""
    user_low = models.ForeignKey(U, on_delete=models.CASCADE, related_name="+"); user_high = models.ForeignKey(U, on_delete=models.CASCADE, related_name="+")
    pair_key = models.CharField(max_length=40, unique=True)   # same format as Conversation.pair_key
    active = models.BooleanField(default=True, db_index=True); created = models.DateTimeField(auto_now_add=True)
    unmatched_by = models.ForeignKey(U, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"); unmatched_at = models.DateTimeField(null=True, blank=True)
    class Meta: constraints = [models.CheckConstraint(condition=Q(user_low__lt=F("user_high")), name="date_match_ordered")]
