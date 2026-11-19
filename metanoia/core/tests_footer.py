"""Site footers: no social icons, and the developer credit after the copyright."""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

CREDIT = 'Learner management system by R&amp;G Tech (Gerson Antonio)'


class FooterTests(TestCase):
    def setUp(self):
        user = get_user_model().objects.create_user('learner', 'learner@example.com', 'pw12345!')
        person = user.profile
        person.user_type, person.registered, person.profile_status = 'student', True, True
        person.save()
        self.client.force_login(user)

    def test_app_footer_has_credit_after_copyright_and_no_socials(self):
        html = self.client.get(reverse('shop:storefront')).content.decode()
        footer = html[html.index('<div class="footer">'):]
        footer = footer[:footer.index('</div>\n</div>') + 12]
        self.assertIn('All rights reserved. <span class="built-by">' + CREDIT, footer)
        for icon in ('bi-linkedin', 'bi-youtube', 'bi-whatsapp', 'bi-facebook', 'bi-instagram'):
            self.assertNotIn(icon, footer)
        self.assertNotIn('linkedin.com', footer)

    def test_public_and_auth_footers_carry_the_credit(self):
        self.client.logout()
        for name in ('pages:terms-of-service', 'account_login'):
            html = self.client.get(reverse(name), follow=True).content.decode()
            self.assertIn(CREDIT, html, name)
            self.assertNotIn('Created by', html, name)
