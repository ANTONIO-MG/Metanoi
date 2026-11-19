"""HTML routes for the communication app (mounted at ``/communication/`` under
the ``communication`` namespace).

Route groups, in order:

* **Support / help centre** — all users, role-scoped content.
* **Chat** and **direct-message connection requests** (the student ↔ student gate).
* **Meetings, calls and class sessions.** A group's Microsoft Teams video call
  (groups only — no 1:1 or audio calls): start (POST) and the member join
  hand-off.
* **Link preview cards** — JSON, per viewer, for ``static/js/link-preview.js``.
* **Discussions** — the Q&A-style forum.
* **WhatsApp webhook** (Meta Cloud API). Public and unauthenticated by
  necessity: it is signature-checked in the view and exempted from the
  site-wide login gate in :mod:`apps.accounts.middleware`.
* **Notifications and preferences.** The unsubscribe / e-mail preferences page
  reached from the link in an e-mail uses a signed token and needs no sign-in,
  so it is exempt from the login and onboarding gates. A poll endpoint lets the
  client play a sound when a new message or notification lands.
* **Activity feed composer** and the **Metanoia Community** actions (share,
  report, moderate, tag, Message Metanoia). "Explore other communities" lives
  under ``feed/`` so community members and parents (whose access middleware
  opens ``/communication/feed/``) can use it too.
* **The wall** — the social feed on every profile (person / course / module),
  backed by ``Discussion`` + ``DiscussionReply``; each action returns to the
  page it was fired from, so the wall works inline wherever it is rendered.
* **Announcements** (bulk notifications) — staff/admin.
* **Presence heartbeat** — automatic, time-based attendance (see
  :func:`services.record_presence_ping`). The manual register pages that used
  to live under ``attendance/`` were removed (2026-11-09); only this POST
  remains.
* **Study groups** (shared files and notes). They were "workspaces": the URL
  names stay, so every ``{% url %}`` keeps working, and the old paths redirect.
* **In-system e-mail** — admin / staff / educator.
"""

from django.urls import path
from django.views.generic import RedirectView

from . import email_prefs, support, views, whatsapp_views

app_name = 'communication'

urlpatterns = [
    path('support/', support.support, name='support'),

    path('chat/', views.chat_home, name='chat-home'),
    path('chat/direct/<int:user_id>/', views.chat_direct, name='chat-direct'),
    path('chat/<int:group_id>/', views.chat_room, name='chat-room'),

    path('connect/<int:user_id>/', views.connection_request, name='connection-request'),
    path('connect/<int:pk>/respond/', views.connection_respond, name='connection-respond'),

    path('meetings/', views.meetings_list, name='meetings'),
    path('meetings/new/', views.meeting_create, name='meeting-create'),
    path('meet/<slug:slug>/', views.meeting_room, name='meeting-room'),
    path('chat/<int:group_id>/call/', views.chat_call, name='chat-call'),
    path('chat/<int:group_id>/call/<int:meeting_id>/join/', views.chat_call_join, name='chat-call-join'),
    path('link-preview/', views.link_preview, name='link-preview'),

    path('discussions/', views.discussions, name='discussions'),
    path('discussions/new/', views.discussion_create, name='discussion-create'),
    path('discussions/<int:pk>/', views.discussion_detail, name='discussion-detail'),

    path('whatsapp/webhook/', whatsapp_views.webhook, name='whatsapp-webhook'),

    path('notifications/', views.notifications_list, name='notifications'),
    path('notifications/preferences/', views.notification_preferences, name='notification-preferences'),
    path('notifications/<int:pk>/', views.notification_detail, name='notification-detail'),

    path('email-preferences/<str:token>/', email_prefs.preferences, name='email-preferences'),
    path('email-preferences/<str:token>/one-click/', email_prefs.one_click,
         name='email-unsubscribe-one-click'),

    path('alerts/counts/', views.alert_counts, name='alert-counts'),
    path('alerts/bell/', views.alerts_bell, name='alerts-bell'),
    path('push/key/', views.push_key, name='push-key'),
    path('push/subscribe/', views.push_subscribe, name='push-subscribe'),
    path('push/unsubscribe/', views.push_unsubscribe, name='push-unsubscribe'),

    path('feed/post/', views.feed_create, name='feed-create'),
    path('feed/<int:pk>/like/', views.feed_like, name='feed-like'),
    path('feed/<int:pk>/comment/', views.feed_comment, name='feed-comment'),
    path('feed/<int:pk>/share/', views.feed_share, name='feed-share'),
    path('feed/<int:pk>/report/', views.feed_report, name='feed-report'),
    path('feed/<int:pk>/delete/', views.feed_delete, name='feed-delete'),
    path('feed/<int:pk>/remove/', views.feed_remove, name='feed-remove'),
    path('feed/<int:pk>/restore/', views.feed_restore, name='feed-restore'),
    path('feed/<int:pk>/dismiss/', views.feed_dismiss_reports, name='feed-dismiss'),
    path('feed/comment/<int:pk>/remove/', views.feed_comment_remove, name='feed-comment-remove'),
    path('feed/communities/explore/', views.community_explore, name='community-explore'),
    path('feed/communities/<slug:slug>/join/', views.community_join, name='join-community'),
    path('feed/communities/<slug:slug>/leave/', views.community_leave, name='leave-community'),
    path('community/moderation/', views.community_moderation, name='community-moderation'),
    path('community/mentions/', views.community_mentions, name='community-mentions'),
    path('message-metanoia/', views.message_metanoia, name='message-metanoia'),

    path('wall/post/', views.wall_post, name='wall-post'),
    path('wall/<int:pk>/like/', views.wall_like, name='wall-like'),
    path('wall/<int:pk>/comment/', views.wall_comment, name='wall-comment'),
    path('wall/comment/<int:pk>/like/', views.wall_comment_like, name='wall-comment-like'),

    path('announcements/', views.announcement_list, name='announcements'),
    path('announcements/new/', views.announcement_compose, name='announcement-compose'),
    path('announcements/audience/', views.announcement_audience_preview, name='announcement-audience'),
    path('announcements/people/', views.announcement_people_search, name='announcement-people'),
    path('announcements/<int:pk>/', views.announcement_detail, name='announcement-detail'),
    path('announcements/<int:pk>/edit/', views.announcement_compose, name='announcement-edit'),
    path('announcements/<int:pk>/action/', views.announcement_action, name='announcement-action'),

    path('attendance/<int:pk>/ping/', views.attendance_ping, name='attendance-ping'),

    path('study-groups/', views.workspaces, name='workspaces'),
    path('study-groups/<int:pk>/', views.workspace_detail, name='workspace-detail'),
    path('workspaces/', RedirectView.as_view(pattern_name='communication:workspaces', query_string=True,
                                             permanent=True)),
    path('workspaces/<int:pk>/', RedirectView.as_view(pattern_name='communication:workspace-detail',
                                                      query_string=True, permanent=True)),

    path('mail/', views.mail_inbox, name='mail-inbox'),
    path('mail/compose/', views.mail_compose, name='mail-compose'),
    path('mail/<int:pk>/', views.mail_read, name='mail-read'),
    path('mail/<int:pk>/action/', views.mail_action, name='mail-action'),
]
