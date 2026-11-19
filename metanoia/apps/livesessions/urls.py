"""Routes for the Metanoia calendar and live sessions (mounted at ``/calendar/``).

Notable routes:

* ``availability/`` -- the "when can I put this?" free-slot finder, open to
  anyone who can schedule.
* ``feed/<token>.ics`` -- the subscribable feed that Google, Outlook and Apple
  poll. It is unauthenticated by necessity (the token is the credential) and is
  therefore exempted from the site-wide login gate in
  :mod:`apps.accounts.middleware`.
* ``connections/...`` -- connected calendars; the forms themselves live on the
  user's settings page.
* ``sessions/<pk>/join/`` -- the identified hand-off into the live room, and
  the only route students take into a session.
* ``sessions/<pk>/transcript/`` and ``summary/`` -- the transcript and summary
  served on our own domain; students are never handed a SharePoint link,
  because it lands on a Microsoft sign-in page.
* ``sessions/<pk>/calendar.ics`` and ``share/`` -- one session as an ``.ics``
  download, and a re-send of the joining link.
* ``recordings/manage/`` -- the YouTube recordings scan/map screen
  (admin/staff only).
"""

from django.urls import path

from . import views

app_name = 'livesessions'

urlpatterns = [
    path('', views.calendar, name='calendar'),
    path('day/<str:on>/', views.day, name='day'),
    path('availability/', views.availability, name='availability'),
    path('feed.json', views.feed, name='feed'),
    path('metanoia-calendar.ics', views.ics, name='ics'),
    path('feed/<uuid:token>.ics', views.personal_feed, name='personal-feed'),

    path('connections/add/', views.connect_calendar, name='connect-calendar'),
    path('connections/<int:pk>/refresh/', views.refresh_calendar, name='refresh-calendar'),
    path('connections/<int:pk>/remove/', views.disconnect_calendar, name='disconnect-calendar'),
    path('connections/reset-link/', views.rotate_calendar_token, name='rotate-calendar-token'),

    path('sessions/new/', views.session_create, name='session-create'),
    path('sessions/people/', views.session_people_search, name='session-people'),
    path('sessions/past/', views.past_sessions, name='past-sessions'),
    path('sessions/<int:pk>/', views.session_detail, name='session-detail'),
    path('sessions/<int:pk>/join/', views.session_join, name='session-join'),
    path('sessions/<int:pk>/video/', views.session_video, name='session-video'),
    path('sessions/<int:pk>/transcript/', views.session_transcript, name='session-transcript'),
    path('sessions/<int:pk>/summary/', views.session_summary, name='session-summary'),
    path('sessions/<int:pk>/calendar.ics', views.session_ics, name='session-ics'),
    path('sessions/<int:pk>/share/', views.session_share, name='session-share'),
    path('sessions/<int:pk>/register/', views.session_register, name='session-register'),
    path('sessions/<int:pk>/register/resolve/', views.resolve_attendee, name='resolve-attendee'),
    path('sessions/<int:pk>/edit/', views.session_edit, name='session-edit'),
    path('sessions/<int:pk>/cancel/', views.session_cancel, name='session-cancel'),

    path('settings/', views.settings_page, name='settings'),

    path('recordings/manage/', views.youtube_admin, name='youtube-admin'),
]
