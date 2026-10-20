from django.contrib import admin
from .models import *
for m in (Conversation, Message, Notification, Battle, BattleEntry, BattleVote, User, Video, Report, Story, StoryNode, StoryOption, Block, Comment): admin.site.register(m)

from django.utils import timezone as _tz
from datetime import timedelta as _td
@admin.register(MatchSession)
class MatchSessionAdmin(admin.ModelAdmin):
    list_display = ("id", "user_a", "user_b", "mode", "status", "end_reason", "created"); list_filter = ("status", "end_reason", "mode")
    readonly_fields = [f.name for f in MatchSession._meta.fields]  # evidence: no editing
@admin.action(description="Restrict selected users from Match for 7 days")
def restrict_match(modeladmin, request, qs): qs.update(match_banned_until=_tz.now() + _td(days=7))
@admin.action(description="Lift Match restriction")
def lift_match(modeladmin, request, qs): qs.update(match_banned_until=None)
from django.contrib.auth.admin import UserAdmin as _BaseUserAdmin
admin.site.unregister(User)
@admin.register(User)
class UserAdmin(_BaseUserAdmin):
    list_display = ("username", "is_staff", "age_verified", "match_banned_until", "date_joined")
    fieldsets = _BaseUserAdmin.fieldsets + (("ZINGO", {"fields": ("bio", "date_of_birth", "city", "is_private", "interests", "language", "age_verified", "match_consent_version", "match_banned_until")}),)
    actions = [restrict_match, lift_match]

@admin.action(description="Put selected dating profiles on moderation hold (hides them)")
def hold_profiles(modeladmin, request, qs): qs.update(moderation_hold=True, is_active=False)
@admin.action(description="Clear moderation hold (user must re-activate)")
def clear_hold(modeladmin, request, qs): qs.update(moderation_hold=False)
@admin.register(DatingProfile)
class DatingProfileAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "is_active", "moderation_hold", "city", "created"); list_filter = ("is_active", "moderation_hold"); search_fields = ("user__username",); actions = [hold_profiles, clear_hold]
admin.site.register(DateMatch)
