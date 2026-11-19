"""Parents and guardians, end to end.

* Registration step 1 really sends the invite it promises (and refuses the
  student's own address).
* The invite link opens one sign-up page: the e-mail is the invited one, the
  account is created verified, linked to the student and signed in. Used,
  revoked or expired links, a taken address and the two-guardian limit all
  refuse cleanly.
* A parent's home lists their children and lets them choose which to follow;
  the choice is remembered and never reaches beyond their own children.
* Progress and invoices open on that child, and never on another family's.
"""
from datetime import timedelta
from decimal import Decimal

from allauth.account.models import EmailAddress
from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Invitation, ParentLink, Person
from core.scoping import PARENT_CHILD_SESSION_KEY

User = get_user_model()
LOCMEM_CACHE = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}}
STRONG = 'Blue-Kettle-Sings-42'


def make_user(email, user_type='student', *, first='', last='', onboarded=True, verified=True):
    """A user with a verified e-mail and a Person of ``user_type``, onboarded unless told otherwise."""
    user = User.objects.create_user(username=email, email=email, password=STRONG)
    EmailAddress.objects.create(user=user, email=email, verified=verified, primary=True)
    Person.objects.filter(user=user).update(user_type=user_type, first_name=first, last_name=last,
                                            registered=onboarded, profile_status=onboarded)
    return User.objects.get(pk=user.pk)


@override_settings(CACHES=LOCMEM_CACHE, SECURE_SSL_REDIRECT=False,
                   WHATSAPP_VERIFY_AT_REGISTRATION=False, SITE_URL='https://lms.example.com',
                   EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
class _Base(TestCase):
    """Clean locmem cache, locmem mail and no WhatsApp verification for every test."""
    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)


class Step1InviteTests(_Base):
    """Registration step 1 sends the guardian invite it promises."""
    def setUp(self):
        super().setUp()
        self.student = make_user('kid@example.com', onboarded=False)
        self.client.force_login(self.student)
        self.url = reverse('accounts:register')

    def _post(self, guardian_email):
        """Post step 1 with ``guardian_email`` and the other required fields."""
        return self.client.post(self.url, {
            'basics-first_name': 'Sam', 'basics-last_name': 'Student',
            'basics-guardian_relationship': 'parent', 'basics-guardian_email': guardian_email,
            'contact-phone_country': 'ZA', 'contact-primary_phone': '082 123 4567',
            'contact-city': 'Durban',
            'consent-privacy_policy_accepted': 'on', 'consent-data_processing_accepted': 'on',
        })

    def test_a_guardian_email_creates_and_emails_an_invite(self):
        response = self._post('Mum@Example.com')
        self.assertRedirects(response, reverse('accounts:register-course'), fetch_redirect_response=False)
        invite = Invitation.objects.get()
        self.assertEqual(invite.role, Invitation.ROLE_PARENT)
        self.assertEqual(invite.email, 'mum@example.com')
        self.assertEqual(invite.student.user, self.student)
        self.assertEqual(invite.invited_by, self.student)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ['mum@example.com'])
        link = reverse('accounts:accept-invite', args=[invite.token])
        body = mail.outbox[0].body + ''.join(str(a[0]) for a in getattr(mail.outbox[0], 'alternatives', []))
        self.assertIn(link, body)

    def test_resaving_step1_does_not_send_the_invite_twice(self):
        self._post('mum@example.com')
        self._post('mum@example.com')
        self.assertEqual(Invitation.objects.count(), 1)
        self.assertEqual(len(mail.outbox), 1)

    def test_changing_the_address_revokes_the_old_invite(self):
        self._post('mum@example.com')
        self._post('dad@example.com')
        self.assertEqual(Invitation.objects.get(email='mum@example.com').status, Invitation.STATUS_REVOKED)
        self.assertEqual(Invitation.objects.get(email='dad@example.com').status, Invitation.STATUS_PENDING)

    def test_the_students_own_email_is_refused(self):
        response = self._post('KID@example.com')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'That is your own e-mail address')
        self.assertFalse(Invitation.objects.exists())
        self.assertEqual(len(mail.outbox), 0)

    def test_no_guardian_email_no_invite(self):
        self._post('')
        self.assertFalse(Invitation.objects.exists())
        self.assertEqual(len(mail.outbox), 0)


class ParentSignupTests(_Base):
    """The invite link's one-page sign-up."""
    def setUp(self):
        super().setUp()
        self.student = make_user('kid@example.com', first='Sam', last='Student')
        self.invite = Invitation.objects.create(role=Invitation.ROLE_PARENT, email='mum@example.com',
                                                student=self.student.profile, invited_by=self.student)
        self.url = reverse('accounts:accept-invite', args=[self.invite.token])

    def _data(self, **over):
        """A valid sign-up form, with ``over`` replacing fields."""
        data = {'first_name': 'Mary', 'last_name': 'Student', 'phone': '082 555 1234',
                'relationship': 'Mother', 'password1': STRONG, 'password2': STRONG, 'terms': 'on'}
        data.update(over)
        return data

    def test_the_page_shows_the_invited_email_read_only(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'accounts/parent-signup.html')
        self.assertContains(response, 'value="mum@example.com"')
        self.assertContains(response, 'readonly')
        self.assertNotContains(response, 'name="email"')
        self.assertContains(response, 'Sam Student')

    def test_signing_up_creates_a_verified_linked_parent_and_signs_them_in(self):
        """The parent is created verified, linked, signed in — and lands straight in their home,
        with no generic sign-up and no registration wizard.
        """
        response = self.client.post(self.url, self._data())
        self.assertRedirects(response, reverse('myhub:index'), fetch_redirect_response=False)
        parent = User.objects.get(email='mum@example.com')
        person = parent.profile
        self.assertEqual(person.user_type, 'parent')
        self.assertEqual((person.first_name, person.last_name), ('Mary', 'Student'))
        self.assertEqual(person.phone, '+27 825551234')
        self.assertTrue(person.registered and person.profile_status)
        self.assertTrue(person.onboarding_complete)
        self.assertTrue(EmailAddress.objects.get(user=parent).verified)
        self.assertTrue(parent.check_password(STRONG))
        link = ParentLink.objects.get(parent=parent)
        self.assertEqual((link.student, link.relationship), (self.student, 'Mother'))
        self.invite.refresh_from_db()
        self.assertEqual(self.invite.status, Invitation.STATUS_ACCEPTED)
        self.assertEqual(self.invite.accepted_by, parent)
        self.assertEqual(int(self.client.session['_auth_user_id']), parent.pk)
        home = self.client.get(reverse('myhub:index'))
        self.assertEqual(home.status_code, 200)
        self.assertTemplateUsed(home, 'myhub/parent-home.html')

    def test_a_posted_email_is_ignored(self):
        self.client.post(self.url, self._data(email='attacker@example.com'))
        self.assertFalse(User.objects.filter(email='attacker@example.com').exists())
        self.assertTrue(User.objects.filter(email='mum@example.com').exists())

    def test_invalid_details_are_refused(self):
        """Missing names or phone, mismatched or too-weak passwords and unticked terms are refused."""
        cases = [
            self._data(first_name=''),
            self._data(phone=''),
            self._data(password2='Something-Else-77'),
            self._data(password1='12345678', password2='12345678'),
            self._data(terms=''),
        ]
        for data in cases:
            if not data.get('terms'):
                data.pop('terms')
            response = self.client.post(self.url, data)
            self.assertEqual(response.status_code, 200, data)
            self.assertTrue(response.context['form'].errors, data)
        self.assertFalse(User.objects.filter(email='mum@example.com').exists())
        self.invite.refresh_from_db()
        self.assertEqual(self.invite.status, Invitation.STATUS_PENDING)

    def test_a_used_link_no_longer_works(self):
        self.client.post(self.url, self._data())
        self.client.logout()
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 410)
        response = self.client.post(self.url, self._data(first_name='Again'))
        self.assertEqual(response.status_code, 410)
        self.assertEqual(User.objects.filter(email='mum@example.com').count(), 1)

    def test_a_revoked_link_no_longer_works(self):
        Invitation.objects.filter(pk=self.invite.pk).update(status=Invitation.STATUS_REVOKED)
        self.assertEqual(self.client.get(self.url).status_code, 410)

    def test_an_expired_link_no_longer_works(self):
        Invitation.objects.filter(pk=self.invite.pk).update(
            created_at=timezone.now() - timedelta(days=Invitation.EXPIRY_DAYS + 1))
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 410)
        self.assertContains(response, 'expired', status_code=410)
        self.client.post(self.url, self._data())
        self.assertFalse(User.objects.filter(email='mum@example.com').exists())

    def test_an_unknown_token_is_refused(self):
        import uuid
        response = self.client.get(reverse('accounts:accept-invite', args=[uuid.uuid4()]))
        self.assertEqual(response.status_code, 410)

    def test_the_guardian_limit_is_enforced(self):
        for i in range(ParentLink.MAX_PER_STUDENT):
            ParentLink.objects.create(parent=make_user(f'g{i}@example.com', 'parent'), student=self.student)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 409)
        self.assertContains(response, 'maximum', status_code=409)
        response = self.client.post(self.url, self._data())
        self.assertEqual(response.status_code, 409)
        self.assertFalse(User.objects.filter(email='mum@example.com').exists())
        self.assertEqual(ParentLink.objects.filter(student=self.student).count(), ParentLink.MAX_PER_STUDENT)

    def test_an_address_that_already_has_an_account_cannot_sign_up_again(self):
        make_user('mum@example.com', 'student')
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 409)
        response = self.client.post(self.url, self._data())
        self.assertEqual(response.status_code, 409)
        self.assertFalse(ParentLink.objects.exists())

    def test_an_existing_parent_signs_in_and_the_link_adds_the_child(self):
        mum = make_user('mum@example.com', 'parent')
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 409)
        self.assertContains(response, '?next=', status_code=409)
        self.client.force_login(mum)
        response = self.client.get(self.url)
        self.assertRedirects(response, reverse('myhub:index'), fetch_redirect_response=False)
        self.assertTrue(ParentLink.objects.filter(parent=mum, student=self.student).exists())
        self.invite.refresh_from_db()
        self.assertEqual(self.invite.status, Invitation.STATUS_ACCEPTED)

    def test_somebody_else_signed_in_cannot_use_the_link(self):
        self.client.force_login(self.student)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 409)
        self.assertFalse(ParentLink.objects.exists())


class SharedLinkTests(_Base):
    """``/community/parents/<token>/`` — the student's shareable link."""

    def setUp(self):
        super().setUp()
        self.student = make_user('kid@example.com', first='Sam', last='Student')
        self.url = reverse('accounts:parent-register', args=[self.student.profile.parent_invite_token])

    def test_signing_up_links_but_waits_for_email_verification(self):
        response = self.client.post(self.url, {
            'email': 'dad@example.com', 'first_name': 'Don', 'last_name': 'Student',
            'phone': '0825550000', 'relationship': 'Father',
            'password1': STRONG, 'password2': STRONG, 'terms': 'on'})
        self.assertRedirects(response, reverse('myhub:page-login'), fetch_redirect_response=False)
        dad = User.objects.get(email='dad@example.com')
        self.assertEqual(dad.profile.user_type, 'parent')
        self.assertFalse(EmailAddress.objects.get(user=dad).verified)
        self.assertTrue(ParentLink.objects.filter(parent=dad, student=self.student).exists())
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_a_signed_in_student_cannot_link_themselves(self):
        other = make_user('other@example.com')
        self.client.force_login(other)
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 409)
        self.assertFalse(ParentLink.objects.exists())


class ChildSelectionTests(_Base):
    """A parent choosing which child to follow, and what that opens."""
    def setUp(self):
        super().setUp()
        self.anna = make_user('anna@example.com', first='Anna', last='Kid')
        self.ben = make_user('ben@example.com', first='Ben', last='Kid')
        self.stranger = make_user('zed@example.com', first='Zed', last='Other')
        self.parent = make_user('mum@example.com', 'parent', first='Mary', last='Kid')
        ParentLink.objects.create(parent=self.parent, student=self.anna)
        ParentLink.objects.create(parent=self.parent, student=self.ben)
        self.other_parent = make_user('pa@example.com', 'parent')
        ParentLink.objects.create(parent=self.other_parent, student=self.stranger)
        self.client.force_login(self.parent)

    def _select(self, student):
        """Post the child picker for ``student``."""
        return self.client.post(reverse('accounts:parent-select-child'), {'student': student.pk})

    def test_home_lists_the_children_not_the_student_dashboard(self):
        """The parent home lists their children (and the navbar has a Progress tab), not the student dashboard."""
        response = self.client.get(reverse('myhub:index'))
        self.assertTemplateUsed(response, 'myhub/parent-home.html')
        self.assertTemplateNotUsed(response, 'myhub/index.html')
        names = [c['name'] for c in response.context['cards']]
        self.assertEqual(names, ['Anna Kid', 'Ben Kid'])
        self.assertNotContains(response, 'Zed Other')
        self.assertNotContains(response, 'Register now')
        self.assertNotContains(response, 'where your studies stand')
        self.assertContains(response, reverse('reports:my-progress'))

    def test_the_classic_view_is_not_a_way_round_it(self):
        response = self.client.get(reverse('myhub:index') + '?view=classic')
        self.assertTemplateUsed(response, 'myhub/parent-home.html')

    def test_choosing_a_child_is_remembered(self):
        response = self._select(self.ben)
        self.assertRedirects(response, reverse('myhub:index'), fetch_redirect_response=False)
        self.assertEqual(self.client.session[PARENT_CHILD_SESSION_KEY], self.ben.pk)
        home = self.client.get(reverse('myhub:index'))
        self.assertEqual(home.context['current']['user'], self.ben)
        progress = self.client.get(reverse('reports:my-progress'))
        self.assertEqual(progress.context['target'], self.ben)
        self._select(self.anna)
        self.assertEqual(self.client.get(reverse('reports:my-progress')).context['target'], self.anna)

    def test_another_familys_child_cannot_be_chosen(self):
        self._select(self.ben)
        self._select(self.stranger)
        self.assertEqual(self.client.session[PARENT_CHILD_SESSION_KEY], self.ben.pk)
        self.assertEqual(self.client.get(reverse('reports:my-progress')).context['target'], self.ben)

    def test_a_child_unlinked_since_is_forgotten(self):
        self._select(self.ben)
        ParentLink.objects.filter(parent=self.parent, student=self.ben).delete()
        self.assertEqual(self.client.get(reverse('reports:my-progress')).context['target'], self.anna)

    def test_progress_of_an_unlinked_student_is_a_404(self):
        response = self.client.get(reverse('reports:my-progress'), {'student': self.stranger.pk})
        self.assertEqual(response.status_code, 404)

    def test_choosing_is_for_parents_only(self):
        self.client.force_login(self.anna)
        self.client.post(reverse('accounts:parent-select-child'), {'student': self.ben.pk})
        self.assertNotIn(PARENT_CHILD_SESSION_KEY, self.client.session)

    def test_parents_reach_what_community_members_reach(self):
        for url in ('/social/', reverse('communication:message-metanoia'), '/shop/',
                    '/communication/notifications/', reverse('finance:my-finances'),
                    reverse('finance:statement'), reverse('finance:payment-history')):
            response = self.client.get(url)
            self.assertNotEqual(response.get('Location', ''), '/myhub/', url)
            self.assertIn(response.status_code, (200, 302), url)


class ParentFinanceScopeTests(_Base):
    """Invoices and payment pages open on the followed child, never another family's."""
    def setUp(self):
        super().setUp()
        from apps.finance.models import Invoice, InvoiceItem
        self.anna = make_user('anna@example.com', first='Anna', last='Kid')
        self.ben = make_user('ben@example.com', first='Ben', last='Kid')
        self.stranger = make_user('zed@example.com', first='Zed', last='Other')
        self.parent = make_user('mum@example.com', 'parent')
        ParentLink.objects.create(parent=self.parent, student=self.anna)
        ParentLink.objects.create(parent=self.parent, student=self.ben)

        def invoice(user):
            """A sent R100 invoice for ``user``."""
            inv = Invoice.objects.create(customer=user, created_by=user, status=Invoice.STATUS_SENT)
            InvoiceItem.objects.create(invoice=inv, description='Fees', quantity=1,
                                       unit_price=Decimal('100'))
            inv.recalc_total()
            inv.refresh_status()
            return inv
        self.anna_inv, self.ben_inv, self.stranger_inv = invoice(self.anna), invoice(self.ben), invoice(self.stranger)
        self.client.force_login(self.parent)

    def _listed(self, tab='all'):
        """The invoice ids listed on the invoices page's ``tab``."""
        response = self.client.get(reverse('finance:invoices'), {'tab': tab})
        self.assertEqual(response.status_code, 200)
        return {inv.pk for inv in response.context['invoices']}

    def test_the_list_shows_the_followed_childs_invoices_only(self):
        self.assertEqual(self._listed(), {self.anna_inv.pk})
        self.client.post(reverse('accounts:parent-select-child'), {'student': self.ben.pk})
        self.assertEqual(self._listed(), {self.ben_inv.pk})

    def test_a_childs_invoice_opens_and_another_familys_does_not(self):
        for inv in (self.anna_inv, self.ben_inv):
            self.assertEqual(self.client.get(reverse('finance:invoice-detail', args=[inv.pk])).status_code, 200)
        response = self.client.get(reverse('finance:invoice-detail', args=[self.stranger_inv.pk]))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.get(reverse('finance:invoice-pdf', args=[self.stranger_inv.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse('finance:receipt', args=[self.stranger_inv.pk])).status_code, 302)

    def test_asking_for_another_familys_child_shows_the_parents_own(self):
        response = self.client.get(reverse('finance:invoices'), {'tab': 'all', 'student': self.stranger.pk})
        self.assertNotIn(self.stranger_inv.pk, {inv.pk for inv in response.context['invoices']})

    def test_payment_history_and_due_are_scoped(self):
        response = self.client.get(reverse('finance:payment-history'))
        self.assertEqual({inv.pk for inv in response.context['upcoming']}, {self.anna_inv.pk})
        response = self.client.get(reverse('finance:due-payments'))
        self.assertEqual({inv.pk for inv in response.context['invoices']}, {self.anna_inv.pk})

    def test_a_student_still_sees_only_their_own(self):
        self.client.force_login(self.anna)
        self.assertEqual(self._listed(), {self.anna_inv.pk})
        self.client.get(reverse('finance:invoices'), {'student': self.ben.pk})
        self.assertEqual(self.client.get(reverse('finance:invoice-detail', args=[self.ben_inv.pk])).status_code, 302)
