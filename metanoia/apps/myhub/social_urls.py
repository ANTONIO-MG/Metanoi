"""URL routing for the public / social pages (mounted at ``/social/`` under the
``pages`` namespace).

These were reached through a single ``<str:template_name>/`` wildcard that
rendered any file in ``templates/pages/``. That made it impossible to tell a real
page from a leftover of the Social theme — a filename scan could not see which
were linked, and half of them were demo content (photography workshops, "how much
should I offer the sellers?"). They are named routes now: what is here is what
exists. The legal documents each have a direct, shareable link.
"""

from django.urls import path

from . import views

app_name = 'pages'

urlpatterns = [
    path('', views.social_dashboard, name='dashboard'),
    path('landing/', views.page_landing, name='landing'),
    path('privacy-and-terms/', views.page_privacy_terms, name='privacy-and-terms'),
    path('terms-and-conditions/', views.page_terms_and_conditions, name='terms-and-conditions'),
    path('terms-of-service/', views.page_terms_of_service, name='terms-of-service'),
    path('refund-policy/', views.page_refund_policy, name='refund-policy'),
    path('shipping-policy/', views.page_shipping_policy, name='shipping-policy'),
]
