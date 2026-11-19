"""URL routing for the MyHub dashboard (``/myhub/``, ``myhub`` namespace).

Routes: the global navbar search suggestions, the dashboard (``index``), the
events feed and event pages (the calendar itself lives at ``/calendar/``, in
:mod:`apps.livesessions`), and the auth/utility pages.

One name, one URL: never add a second route named ``index``. Django reverses
a duplicate name to the *last* pattern, which once sent parents into a
``ParentAccessMiddleware`` redirect loop.
"""

from django.urls import path

from . import search, views

app_name = 'myhub'

urlpatterns = [
    path('search/suggest/', search.suggest, name='search-suggest'),

    path('', views.index, name='index'),
    path('events.json', views.events_feed, name='events-feed'),
    path('events/<int:pk>/', views.event_detail, name='event-detail'),
    path('add-reminder/', views.add_reminder, name='add-reminder'),

    path('page-login/', views.page_login, name='page-login'),
    path('page-register/', views.page_register, name='page-register'),
    path('logout/', views.page_logout, name='logout'),
    path('page-forgot-password/', views.page_forgot_password, name='page-forgot-password'),
]
