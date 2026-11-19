"""Developer diagnostics routes (mounted at ``/diagnostics/``, developers only)."""

from django.urls import path

from . import views

app_name = 'diagnostics'

urlpatterns = [
    path('', views.log, name='log'),
    path('dictionary/', views.dictionary, name='dictionary'),
    path('dictionary.json', views.dictionary_json, name='dictionary-json'),
    path('export/', views.export_log, name='export'),
    path('system/', views.system, name='system'),
    path('system/live/', views.system_live, name='system-live'),
    path('<int:pk>/', views.event_detail, name='event'),
]
