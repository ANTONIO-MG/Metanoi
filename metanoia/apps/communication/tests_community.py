"""The Metanoia Community: joining, what a member may reach, communities,
posting limits, tagging, sharing, reporting/moderation, Message Metanoia and
becoming a student."""

from allauth.account.models import EmailAddress
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.accounts.models import Person
from apps.accounts.tests import captcha_test_mode
from apps.learning.models import Institution, Programme, ProgrammeEnrolment

from . import community as cm
from .models import ChatGroup, Community, Feed, FeedMention, FeedReport, Notification

User = get_user_model()
LOCMEM_CACHE = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}}
PASSWORD = 'Correct-Horse-9!'


def make_user(email, role='student', *, first='Test', last='Person', **extra):
    """Create a registered user with a profile of the given ``role`` and name."""
    user = User.objects.create_user(username=email, email=email, password=PASSWORD, **extra)
    Person.objects.filter(user=user).update(
        user_type=role, first_name=first, last_name=last, registered=True, profile_status=True)
    user.refresh_from_db()
    EmailAddress.objects.update_or_create(user=user, email=email,
                                          defaults={'verified': True, 'primary': True})
    return user


@override_settings(CACHES=LOCMEM_CACHE, SECURE_SSL_REDIRECT=False)
class CommunityTestCase(TestCase):
    """Shared fixture: institutions, programmes, their communities and a member, a student and staff."""
    @classmethod
    def setUpTestData(cls):
        cls.milpark = Institution.objects.create(code='MILPARK', name='Milpark Education')
        cls.unisa = Institution.objects.create(code='UNISA', name='UNISA')
        cls.pgda = Programme.objects.create(institution=cls.milpark, code='PGDA', name='PGDA')
        cls.unisa_pgda = Programme.objects.create(institution=cls.unisa, code='UPGDA', name='PGDA')
        cls.default = Community.default()
        cls.pgda_c = Community.for_programme(cls.pgda)
        cls.unisa_c = Community.for_programme(cls.unisa_pgda)

        cls.staff = make_user('staff@metanoia.test', 'staff', first='Sam', last='Staff')
        cls.student = make_user('stu@metanoia.test', 'student', first='Anesu', last='Sibanda')
        ProgrammeEnrolment.objects.create(person=cls.student.profile, programme=cls.pgda)
        cls.other_student = make_user('other@metanoia.test', 'student', first='Una', last='Isa')
        ProgrammeEnrolment.objects.create(person=cls.other_student.profile, programme=cls.unisa_pgda)
        cls.member = make_user('member@metanoia.test', 'community', first='Cara', last='Member')
        Person.objects.filter(user=cls.member).update(community_institution=cls.milpark,
                                                      community_programme=cls.pgda)
        EmailAddress.objects.filter(user=cls.member).update(verified=False)
        cls.member.refresh_from_db()

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)


class JoinTests(CommunityTestCase):
    """Joining the community through ``/join/``."""
    def payload(self, **over):
        """Return a valid join form submission, with ``over`` replacing fields."""
        data = {'first_name': 'Nia', 'last_name': 'New', 'email': 'nia@example.com',
                'phone': '082 123 4567', 'institution': self.milpark.pk, 'programme': self.pgda.pk,
                'password': 'abc', 'terms': 'on', 'captcha_0': 'PASSED', 'captcha_1': 'PASSED'}
        data.update(over)
        return data

    @captcha_test_mode()
    def test_join_creates_member_and_signs_in(self):
        """Joining creates a signed-in member and the community page opens.

        A member may use a simple password: no password rules apply.
        """
        r = self.client.post(reverse('community-join'), self.payload())
        self.assertRedirects(r, reverse('pages:dashboard'), fetch_redirect_response=False)
        user = User.objects.get(email='nia@example.com')
        p = user.profile
        self.assertEqual(p.user_type, 'community')
        self.assertEqual(p.community_programme, self.pgda)
        self.assertTrue(p.registered and p.profile_status)
        self.assertTrue(user.check_password('abc'))
        self.assertFalse(EmailAddress.objects.get(user=user).verified)
        self.assertEqual(int(self.client.session['_auth_user_id']), user.pk)
        self.assertEqual(self.client.get(reverse('pages:dashboard')).status_code, 200)

    @captcha_test_mode()
    def test_existing_email_is_never_signed_in(self):
        r = self.client.post(reverse('community-join'), self.payload(email='stu@metanoia.test'))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'already an account')
        self.assertNotIn('_auth_user_id', self.client.session)

    @captcha_test_mode()
    def test_programme_must_belong_to_institution(self):
        r = self.client.post(reverse('community-join'), self.payload(institution=self.unisa.pk))
        self.assertEqual(r.status_code, 200)
        self.assertFalse(User.objects.filter(email='nia@example.com').exists())

    def test_member_signs_in_without_verified_email(self):
        r = self.client.post(reverse('myhub:page-login'),
                             {'email': self.member.email, 'password': PASSWORD})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(int(self.client.session['_auth_user_id']), self.member.pk)

    def test_login_and_signup_pages_offer_join(self):
        for name in ('myhub:page-login', 'myhub:page-register'):
            self.assertContains(self.client.get(reverse(name)), reverse('community-join'))


class MemberAccessTests(CommunityTestCase):
    """What a community member (a ``/join/`` account) may and may not reach."""
    def setUp(self):
        super().setUp()
        self.client.force_login(self.member)

    def test_allowed_pages(self):
        for url in (reverse('pages:dashboard'), '/shop/', '/communication/notifications/',
                    reverse('accounts:become-student')):
            r = self.client.get(url)
            self.assertEqual(r.status_code, 200, url)

    def test_student_pages_send_member_back_to_community(self):
        for url in ('/learning/modules/', '/assessments/', '/reports/', '/calendar/'):
            r = self.client.get(url)
            self.assertRedirects(r, reverse('pages:dashboard'), fetch_redirect_response=False)

    def test_home_lands_in_community(self):
        r = self.client.get('/myhub/')
        self.assertRedirects(r, reverse('pages:dashboard'), fetch_redirect_response=False)

    def test_sidebar_is_the_community_menu(self):
        r = self.client.get(reverse('pages:dashboard'))
        self.assertContains(r, 'Message Metanoia')
        self.assertContains(r, 'Become a student')
        self.assertNotContains(r, 'My Modules')

    def test_member_cannot_direct_message_a_student(self):
        r = self.client.get(reverse('communication:chat-direct', args=[self.student.pk]))
        self.assertEqual(r.status_code, 404)

    def test_message_metanoia_opens_a_thread_with_the_team(self):
        r = self.client.get(reverse('communication:message-metanoia'))
        group = ChatGroup.objects.get(kind=ChatGroup.KIND_SUPPORT, created_by=self.member)
        self.assertRedirects(r, reverse('communication:chat-room', args=[group.pk]),
                             fetch_redirect_response=False)
        members = set(group.memberships.values_list('user_id', flat=True))
        self.assertIn(self.staff.pk, members)
        self.assertNotIn(self.student.pk, members)
        self.assertEqual(self.client.get(r.url).status_code, 200)

    def test_person_card_needs_a_shared_context(self):
        """A /join/ account cannot walk the card ids: a student it has never
        seen is a 404 (apps.accounts.visibility)."""
        r = self.client.get(reverse('accounts:person-card-user', args=[self.student.pk]) + '?fragment=1')
        self.assertEqual(r.status_code, 404)

    def test_person_card_of_a_post_author_shows_name_only(self):
        """``show_email`` is off by default, so only the name shows."""
        Feed.objects.create(author=self.student, body='hello', community=self.pgda_c)
        r = self.client.get(reverse('accounts:person-card-user', args=[self.student.pk]) + '?fragment=1')
        self.assertContains(r, 'Anesu Sibanda')
        self.assertNotContains(r, 'stu@metanoia.test')
        self.assertNotContains(r, 'Full profile')
        from apps.accounts.models import UserSettings
        prefs = UserSettings.for_user(self.student)
        prefs.show_email = True
        prefs.profile_visibility = UserSettings.VISIBILITY_PUBLIC
        prefs.save()
        r = self.client.get(reverse('accounts:person-card-user', args=[self.student.pk]) + '?fragment=1')
        self.assertContains(r, 'stu@metanoia.test')


class CommunityMembershipTests(CommunityTestCase):
    """Which communities people belong to, and that programme posts stay inside theirs."""
    def test_who_belongs_where(self):
        self.assertEqual(set(cm.community_ids_for(self.student)), {self.default.pk, self.pgda_c.pk})
        self.assertEqual(set(cm.community_ids_for(self.member)), {self.default.pk, self.pgda_c.pk})
        self.assertEqual(set(cm.community_ids_for(self.other_student)), {self.default.pk, self.unisa_c.pk})
        self.assertTrue({self.pgda_c.pk, self.unisa_c.pk} <= set(cm.community_ids_for(self.staff)))

    def test_new_programme_gets_a_community(self):
        prog = Programme.objects.create(institution=self.milpark, code='BCTA', name='BCTA')
        self.assertTrue(Community.objects.filter(programme=prog).exists())

    def test_programme_posts_stay_in_their_community(self):
        """An outsider can neither see nor like or comment on a programme post."""
        post = Feed.objects.create(author=self.student, body='PGDA only', community=self.pgda_c)
        self.assertIn(post, Feed.visible_to(self.member))
        self.assertIn(post, Feed.visible_to(self.staff))
        self.assertNotIn(post, Feed.visible_to(self.other_student))
        self.client.force_login(self.other_student)
        r = self.client.post(reverse('communication:feed-like', args=[post.pk]))
        self.assertEqual(r.status_code, 404)

    def test_one_community_for_everyone(self):
        post = Feed.objects.create(author=self.member, body='Hello all', community=self.default)
        for user in (self.student, self.other_student, self.staff):
            self.assertIn(post, Feed.visible_to(user))


class PostingTests(CommunityTestCase):
    """Posting to communities, member rate limits and the photos-only attachment rule."""
    def test_post_to_a_chosen_community(self):
        self.client.force_login(self.student)
        r = self.client.post(reverse('communication:feed-create'),
                             {'body': 'Study group tonight?', 'community': self.pgda_c.slug})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(Feed.objects.get(body='Study group tonight?').community, self.pgda_c)

    def test_cannot_post_to_a_community_you_are_not_in(self):
        self.client.force_login(self.student)
        self.client.post(reverse('communication:feed-create'),
                         {'body': 'Sneaky', 'community': self.unisa_c.slug})
        self.assertFalse(Feed.objects.filter(body='Sneaky').exists())

    def test_member_post_rate_limit(self):
        self.client.force_login(self.member)
        for i in range(7):
            self.client.post(reverse('communication:feed-create'), {'body': f'post {i}'})
        self.assertEqual(Feed.objects.filter(author=self.member).count(), 5)

    def test_students_are_not_rate_limited(self):
        self.client.force_login(self.student)
        for i in range(7):
            self.client.post(reverse('communication:feed-create'), {'body': f'post {i}'})
        self.assertEqual(Feed.objects.filter(author=self.student).count(), 7)

    def test_member_comment_rate_limit(self):
        post = Feed.objects.create(author=self.student, body='Hi', community=self.default)
        self.client.force_login(self.member)
        for i in range(22):
            self.client.post(reverse('communication:feed-comment', args=[post.pk]), {'body': f'c{i}'},
                             HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(post.comments.filter(author=self.member).count(), 20)

    def test_member_may_attach_photos_only(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        self.client.force_login(self.member)
        doc = SimpleUploadedFile('notes.pdf', b'%PDF-1.4', content_type='application/pdf')
        self.client.post(reverse('communication:feed-create'), {'body': 'my notes', 'media': [doc]})
        self.assertFalse(Feed.objects.filter(body='my notes').exists())


class MentionTests(CommunityTestCase):
    """Tagging people in posts, scoped to the community."""
    def test_tagging_notifies_the_person(self):
        self.client.force_login(self.member)
        self.client.post(reverse('communication:feed-create'), {
            'body': 'Thanks @Anesu Sibanda for the notes!', 'community': self.pgda_c.slug,
            'mentions': [self.student.pk]})
        post = Feed.objects.get(author=self.member)
        self.assertTrue(FeedMention.objects.filter(post=post, user=self.student).exists())
        self.assertTrue(Notification.objects.filter(recipient=self.student,
                                                    title__icontains='mentioned you').exists())

    def test_cannot_tag_someone_outside_the_community(self):
        self.client.force_login(self.member)
        self.client.post(reverse('communication:feed-create'), {
            'body': 'Hey @Una Isa', 'community': self.pgda_c.slug, 'mentions': [self.other_student.pk]})
        self.assertFalse(FeedMention.objects.exists())

    def test_people_search_is_scoped_to_the_community(self):
        self.client.force_login(self.member)
        r = self.client.get(reverse('communication:community-mentions'), {'c': self.pgda_c.slug, 'q': ''})
        ids = {p['id'] for p in r.json()['results']}
        self.assertIn(self.student.pk, ids)
        self.assertNotIn(self.other_student.pk, ids)

    def test_mention_is_highlighted_in_the_feed(self):
        post = Feed.objects.create(author=self.member, body='Hi @Anesu Sibanda', community=self.default)
        FeedMention.objects.create(post=post, user=self.student)
        self.client.force_login(self.student)
        r = self.client.get(reverse('pages:dashboard'))
        self.assertContains(r, '<span class="feed-mention">@Anesu Sibanda</span>', html=False)


class ShareTests(CommunityTestCase):
    """Sharing a post into another community you belong to."""
    def test_share_into_another_community(self):
        post = Feed.objects.create(author=self.staff, body='Free webinar', community=self.default)
        self.client.force_login(self.student)
        self.client.post(reverse('communication:feed-share', args=[post.pk]),
                         {'community': self.pgda_c.slug, 'body': 'Worth it'})
        share = Feed.objects.get(shared_from=post)
        self.assertEqual(share.community, self.pgda_c)
        self.assertEqual(share.body, 'Worth it')

    def test_cannot_share_into_a_community_you_are_not_in(self):
        post = Feed.objects.create(author=self.staff, body='Free webinar', community=self.default)
        self.client.force_login(self.student)
        self.client.post(reverse('communication:feed-share', args=[post.pk]),
                         {'community': self.unisa_c.slug})
        self.assertFalse(Feed.objects.filter(shared_from=post).exists())


class ModerationTests(CommunityTestCase):
    """Reports, automatic hiding and staff moderation of community posts."""
    def setUp(self):
        super().setUp()
        self.post = Feed.objects.create(author=self.member, body='Buy followers!', community=self.default)

    def report(self, user):
        """Report the post as spam as ``user``."""
        self.client.force_login(user)
        return self.client.post(reverse('communication:feed-report', args=[self.post.pk]),
                                {'reason': 'spam'})

    def test_three_reports_hide_the_post(self):
        extra = make_user('third@metanoia.test', 'student')
        for user in (self.student, self.other_student, extra):
            self.report(user)
        self.post.refresh_from_db()
        self.assertTrue(self.post.is_removed)
        self.assertNotIn(self.post, Feed.visible_to(self.student))
        self.assertIn(self.post, Feed.visible_to(self.staff))

    def test_one_report_per_person(self):
        self.report(self.student)
        self.report(self.student)
        self.assertEqual(FeedReport.objects.filter(post=self.post).count(), 1)

    def test_staff_remove_and_restore(self):
        """The old address leads to the "Community posts" tab of the one Moderation page."""
        self.report(self.student)
        self.client.force_login(self.staff)
        self.client.post(reverse('communication:feed-remove', args=[self.post.pk]), {'reason': 'Spam'})
        self.post.refresh_from_db()
        self.assertTrue(self.post.is_removed)
        self.assertEqual(self.post.reports.get().status, FeedReport.STATUS_REMOVED)
        resp = self.client.get(reverse('communication:community-moderation'))
        self.assertRedirects(resp, reverse('staffdesk:ops-moderation') + '?tab=community',
                             fetch_redirect_response=False)
        resp = self.client.get(reverse('staffdesk:ops-moderation'), {'tab': 'community'})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Recently removed')
        self.assertContains(resp, self.post.body[:30])
        self.client.post(reverse('communication:feed-restore', args=[self.post.pk]))
        self.post.refresh_from_db()
        self.assertFalse(self.post.is_removed)

    def test_students_cannot_remove_posts(self):
        self.client.force_login(self.student)
        self.client.post(reverse('communication:feed-remove', args=[self.post.pk]))
        self.post.refresh_from_db()
        self.assertFalse(self.post.is_removed)

    def test_author_can_delete_own_post(self):
        self.client.force_login(self.member)
        self.client.post(reverse('communication:feed-delete', args=[self.post.pk]))
        self.assertFalse(Feed.objects.filter(pk=self.post.pk).exists())


class BecomeStudentTests(CommunityTestCase):
    """A member upgrading to a student account."""
    def test_full_upgrade(self):
        """The upgrade path, step by step.

        The password page stays locked until the e-mail is confirmed (allauth's
        ``email_confirmed`` signal); then everything leads to choosing the new
        password, after which the normal registration takes over.
        """
        self.client.force_login(self.member)
        r = self.client.post(reverse('accounts:become-student'))
        self.assertEqual(r.status_code, 200)
        person = Person.objects.get(user=self.member)
        self.assertIsNotNone(person.upgrade_requested_at)
        r = self.client.get(reverse('accounts:become-student-password'))
        self.assertRedirects(r, reverse('accounts:become-student'), fetch_redirect_response=False)

        from allauth.account.signals import email_confirmed
        address = EmailAddress.objects.get(user=self.member)
        address.verified = True
        address.save()
        email_confirmed.send(sender=EmailAddress, request=None, email_address=address)

        r = self.client.get(reverse('pages:dashboard'))
        self.assertRedirects(r, reverse('accounts:become-student-password'), fetch_redirect_response=False)
        r = self.client.post(reverse('accounts:become-student-password'),
                             {'new_password1': 'Student-Pass-2026!', 'new_password2': 'Student-Pass-2026!'})
        self.assertRedirects(r, reverse('accounts:register'), fetch_redirect_response=False)
        person.refresh_from_db()
        self.member.refresh_from_db()
        self.assertEqual(person.user_type, 'student')
        self.assertFalse(person.registered)
        self.assertTrue(self.member.check_password('Student-Pass-2026!'))
        self.assertEqual(self.client.get(reverse('accounts:register')).status_code, 200)

    def test_new_password_must_pass_the_rules(self):
        Person.objects.filter(user=self.member).update(
            upgrade_requested_at='2026-11-01T00:00Z', upgrade_verified_at='2026-11-01T00:00Z')
        self.client.force_login(self.member)
        self.client.post(reverse('accounts:become-student-password'),
                         {'new_password1': 'abc', 'new_password2': 'abc'})
        self.assertEqual(Person.objects.get(user=self.member).user_type, 'community')
