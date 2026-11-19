from django.urls import path

from . import views

app_name = 'mailbox'

urlpatterns = [
    path('', views.mailbox, name='inbox'),
    path('list/', views.list_fragment, name='list'),
    path('m/<int:pk>/', views.read_fragment, name='read'),
    path('m/<int:pk>/body/', views.body, name='body'),
    path('m/<int:pk>/source/', views.source, name='source'),
    path('m/<int:pk>/a/<int:att>/', views.attachment, name='attachment'),
    path('m/<int:pk>/respond/<str:mode>/', views.respond, name='respond'),
    path('action/', views.action, name='action'),
    path('folders/new/', views.folder_create, name='folder-create'),
    path('sync/', views.sync_now, name='sync'),
    path('poll/', views.poll, name='poll'),
    path('compose/', views.compose, name='compose'),
    path('draft/new/', views.draft_new, name='draft-new'),
    path('draft/<int:pk>/', views.draft_pane, name='draft'),
    path('draft/<int:pk>/save/', views.draft_save, name='draft-save'),
    path('draft/<int:pk>/attach/', views.draft_attach, name='draft-attach'),
    path('draft/<int:pk>/attach/<int:att>/', views.draft_attachment_download, name='draft-attachment'),
    path('draft/<int:pk>/attach/<int:att>/delete/', views.draft_attach_delete, name='draft-attach-delete'),
    path('draft/<int:pk>/send/', views.draft_send, name='draft-send'),
    path('draft/<int:pk>/discard/', views.draft_discard, name='draft-discard'),
    path('drafts/outbox/', views.drafts_outbox, name='drafts-outbox'),
    path('sig/<str:token>.png', views.signature_avatar, name='signature-avatar'),
    path('contacts/', views.contacts, name='contacts'),
    path('settings/', views.settings_page, name='settings'),
]
