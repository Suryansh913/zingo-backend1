from datetime import date
from rest_framework import serializers
from django.conf import settings
from .models import *

class RegisterSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, min_length=8)
    class Meta:
        model = User; fields = ["username", "password", "email", "date_of_birth"]
    def validate_date_of_birth(self, v):
        if v and v > date.today(): raise serializers.ValidationError("Invalid date.")
        return v
    def create(self, d):
        return User.objects.create_user(**d)

class PublicUserSerializer(serializers.ModelSerializer):
    followers = serializers.SerializerMethodField(); following = serializers.SerializerMethodField()
    class Meta:  # no email / DOB / exact location exposed
        model = User; fields = ["id", "username", "bio", "city", "is_private", "followers", "following"]
    def get_followers(self, o): return o.follower_set.count()
    def get_following(self, o): return o.following_set.count()

class VideoSerializer(serializers.ModelSerializer):
    owner = serializers.CharField(source="owner.username", read_only=True)
    like_count = serializers.IntegerField(read_only=True, default=0); comment_count = serializers.IntegerField(read_only=True, default=0)
    liked = serializers.SerializerMethodField(); owner_following = serializers.SerializerMethodField(); story_root = serializers.SerializerMethodField()
    def get_liked(self, o): return getattr(o, "liked", False)
    def get_owner_following(self, o): return getattr(o, "owner_following", False)
    def get_story_root(self, o): return getattr(o, "story_root", None)
    class Meta:
        model = Video
        fields = ["liked", "owner_following", "story_root", "id", "owner", "file", "thumbnail", "caption", "hashtags", "mood", "visibility", "created", "like_count", "comment_count"]
        read_only_fields = ["created"]
    def validate_file(self, f):
        from pathlib import Path
        from .media import sniff_video
        if not (f.content_type or "").startswith("video/"): raise serializers.ValidationError("Must be a video file.")
        if f.size > settings.MAX_VIDEO_MB * 1024 * 1024: raise serializers.ValidationError(f"File too large (max {settings.MAX_VIDEO_MB} MB).")
        if settings.VIDEO_VERIFY_SIGNATURE:   # the declared type is client-supplied, so check the real bytes and extension too
            if Path(f.name).suffix.lower() not in settings.VIDEO_EXTENSIONS: raise serializers.ValidationError("Unsupported file type. Use MP4 (H.264), MOV or WebM.")
            f.seek(0); head = f.read(16); f.seek(0)
            if sniff_video(head) is None: raise serializers.ValidationError("This file isn't a valid MP4, MOV or WebM video.")
        return f

class CommentSerializer(serializers.ModelSerializer):
    user = serializers.CharField(source="user.username", read_only=True)
    class Meta: model = Comment; fields = ["id", "user", "text", "parent", "created"]

class OptionSerializer(serializers.ModelSerializer):
    votes = serializers.IntegerField(source="vote_total", read_only=True); percent = serializers.FloatField(read_only=True)
    class Meta: model = StoryOption; fields = ["id", "label", "next_node", "votes", "percent"]
