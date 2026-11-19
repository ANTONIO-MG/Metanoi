from django.urls import path

from . import views_whatsapp as views

urlpatterns = [
    path('whatsapp/', views.inbox_view, name='whatsapp'),
    path('whatsapp/<int:pk>/', views.inbox_view, name='whatsapp-thread'),
    path('whatsapp/<int:pk>/poll/', views.poll, name='whatsapp-poll'),
    path('whatsapp/<int:pk>/send/', views.send, name='whatsapp-send'),
    path('whatsapp/<int:pk>/action/', views.action, name='whatsapp-action'),
    path('whatsapp/start/<int:person_id>/', views.start, name='whatsapp-start'),
    path('whatsapp/media/<int:message_id>/', views.media, name='whatsapp-media'),
]
