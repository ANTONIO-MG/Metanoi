"""Who sees which community, and "Explore other communities".

The rule: everybody has the general Metanoia Community; a member of a programme
community has exactly that one on top — two in all — unless they switch on
"Explore other communities", which lets them browse the others and join the
ones they want. Enforced on the server: reading, posting, commenting, liking,
sharing and tagging all go through ``community.communities_for``.
"""
from django.urls import reverse

from apps.accounts.models import Person
from apps.learning.models import Programme, ProgrammeEnrolment

from . import community as cm
from .models import Community, CommunityMembership, Feed
from .tests_community import CommunityTestCase, make_user


class TwoCommunitiesTests(CommunityTestCase):
    """Without exploring: the general community plus your own, nothing else."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.ias = Programme.objects.create(institution=cls.unisa, code='IAS', name='IAS Prep')
        cls.ias_c = Community.for_programme(cls.ias)
        cls.unisa_post = Feed.objects.create(author=cls.other_student, body='UNISA only',
                                             community=cls.unisa_c)
        cls.general_post = Feed.objects.create(author=cls.other_student, body='Hello everyone',
                                               community=cls.default)

    def test_member_has_exactly_two(self):
        self.assertEqual(set(cm.community_ids_for(self.member)), {self.default.pk, self.pgda_c.pk})

    def test_student_has_exactly_two_even_with_an_old_join_choice(self):
        """Joined picking UNISA, later enrolled at Milpark: the old choice is a record, not a third community."""
        Person.objects.filter(user=self.student).update(community_programme=self.unisa_pgda)
        self.assertEqual(set(cm.community_ids_for(self.student)), {self.default.pk, self.pgda_c.pk})

    def test_unenrolled_student_falls_back_to_their_join_choice(self):
        lone = make_user('lone@metanoia.test', 'student')
        Person.objects.filter(user=lone).update(community_programme=self.unisa_pgda)
        self.assertEqual(set(cm.community_ids_for(lone)), {self.default.pk, self.unisa_c.pk})

    def test_member_without_programme_has_only_the_general_one(self):
        loner = make_user('other-inst@metanoia.test', 'community')
        self.assertEqual(cm.community_ids_for(loner), [self.default.pk])

    def test_staff_have_every_community_and_no_explore_card(self):
        self.assertTrue({self.default.pk, self.pgda_c.pk, self.unisa_c.pk, self.ias_c.pk}
                        <= set(cm.community_ids_for(self.staff)))
        self.client.force_login(self.staff)
        r = self.client.get(reverse('pages:dashboard'))
        self.assertNotContains(r, 'Explore other communities')

    def test_feed_chips_show_only_the_two(self):
        self.client.force_login(self.member)
        r = self.client.get(reverse('pages:dashboard'))
        self.assertEqual([c.pk for c in r.context['communities']], [self.default.pk, self.pgda_c.pk])
        self.assertNotContains(r, f'?c={self.unisa_c.slug}')
        self.assertNotContains(r, 'UNISA only')
        self.assertContains(r, 'Hello everyone')
        self.assertContains(r, 'Explore other communities')

    def test_other_community_by_url_shows_nothing_of_it(self):
        self.client.force_login(self.member)
        r = self.client.get(reverse('pages:dashboard') + f'?c={self.unisa_c.slug}')
        self.assertIsNone(r.context['active_community'])
        self.assertNotContains(r, 'UNISA only')
        self.assertContains(r, 'is not one of your communities')

    def test_cannot_read_or_react_to_another_communitys_post(self):
        self.assertFalse(Feed.visible_to(self.member).filter(pk=self.unisa_post.pk).exists())
        self.client.force_login(self.member)
        for name in ('communication:feed-like', 'communication:feed-comment', 'communication:feed-share'):
            r = self.client.post(reverse(name, args=[self.unisa_post.pk]), {'body': 'hi'})
            self.assertEqual(r.status_code, 404, name)
        self.assertFalse(self.unisa_post.comments.exists())

    def test_cannot_post_into_another_community(self):
        self.client.force_login(self.member)
        self.client.post(reverse('communication:feed-create'),
                         {'body': 'Sneaky', 'community': self.unisa_c.slug})
        self.assertFalse(Feed.objects.filter(body='Sneaky').exists())

    def test_cannot_share_into_another_community(self):
        self.client.force_login(self.member)
        self.client.post(reverse('communication:feed-share', args=[self.general_post.pk]),
                         {'community': self.unisa_c.slug})
        self.assertFalse(Feed.objects.filter(shared_from=self.general_post).exists())


class ExploreTests(CommunityTestCase):
    """Switching "Explore other communities" on and off, and joining or leaving communities."""
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.unisa_post = Feed.objects.create(author=cls.other_student, body='UNISA only',
                                             community=cls.unisa_c)

    def setUp(self):
        super().setUp()
        self.client.force_login(self.fresh(self.member))

    @staticmethod
    def fresh(user):
        """The account as the next request sees it (the class-level object
        keeps a cached profile from before the switch was flipped)."""
        return type(user).objects.select_related('profile').get(pk=user.pk)

    def explore(self, on=True):
        """Switch exploring on or off for the logged-in user."""
        return self.client.post(reverse('communication:community-explore'), {'on': '1' if on else '0'})

    def join(self, community):
        """Join ``community`` as the logged-in user."""
        return self.client.post(reverse('communication:join-community', args=[community.slug]))

    def test_cannot_join_without_exploring(self):
        self.join(self.unisa_c)
        self.assertFalse(CommunityMembership.objects.exists())
        self.assertNotIn(self.unisa_c.pk, cm.community_ids_for(self.fresh(self.member)))

    def test_switching_on_lists_the_others(self):
        """Exploring lists the other communities but does not open their posts — joining does."""
        self.explore()
        self.member.profile.refresh_from_db()
        self.assertTrue(self.member.profile.community_explore)
        self.assertEqual(list(cm.explorable_communities(self.fresh(self.member))), [self.unisa_c])
        r = self.client.get(reverse('pages:dashboard'))
        self.assertContains(r, reverse('communication:join-community', args=[self.unisa_c.slug]))
        self.assertNotContains(r, 'UNISA only')

    def test_join_then_read_and_post(self):
        self.explore()
        r = self.join(self.unisa_c)
        self.assertRedirects(r, cm.feed_url(self.unisa_c), fetch_redirect_response=False)
        self.assertEqual(set(cm.community_ids_for(self.fresh(self.member))),
                         {self.default.pk, self.pgda_c.pk, self.unisa_c.pk})
        self.assertTrue(Feed.visible_to(self.fresh(self.member)).filter(pk=self.unisa_post.pk).exists())
        self.client.post(reverse('communication:feed-create'),
                         {'body': 'Hello UNISA', 'community': self.unisa_c.slug})
        self.assertEqual(Feed.objects.get(body='Hello UNISA').community, self.unisa_c)
        self.assertIn(self.member, cm.members_of(self.unisa_c))

    def test_switching_off_goes_back_to_two(self):
        """Switching off leaves two communities, and the joins are remembered for the next time."""
        self.explore()
        self.join(self.unisa_c)
        self.explore(on=False)
        self.assertEqual(set(cm.community_ids_for(self.fresh(self.member))), {self.default.pk, self.pgda_c.pk})
        self.assertFalse(Feed.visible_to(self.fresh(self.member)).filter(pk=self.unisa_post.pk).exists())
        self.assertNotIn(self.member, cm.members_of(self.unisa_c))
        self.client.post(reverse('communication:feed-create'),
                         {'body': 'After opting back', 'community': self.unisa_c.slug})
        self.assertFalse(Feed.objects.filter(body='After opting back').exists())
        self.assertTrue(CommunityMembership.objects.filter(user=self.member,
                                                           community=self.unisa_c).exists())

    def test_leave_a_joined_community(self):
        self.explore()
        self.join(self.unisa_c)
        self.client.post(reverse('communication:leave-community', args=[self.unisa_c.slug]))
        self.assertNotIn(self.unisa_c.pk, cm.community_ids_for(self.fresh(self.member)))

    def test_cannot_join_own_or_general_or_inactive(self):
        self.explore()
        for community in (self.default, self.pgda_c):
            self.join(community)
        Community.objects.filter(pk=self.unisa_c.pk).update(is_active=False)
        self.join(self.unisa_c)
        self.assertFalse(CommunityMembership.objects.exists())

    def test_own_community_cannot_be_left(self):
        self.client.post(reverse('communication:leave-community', args=[self.pgda_c.slug]))
        self.assertIn(self.pgda_c.pk, cm.community_ids_for(self.fresh(self.member)))

    def test_students_explore_too(self):
        self.client.force_login(self.fresh(self.student))
        self.client.post(reverse('communication:community-explore'), {'on': '1'})
        self.client.post(reverse('communication:join-community', args=[self.unisa_c.slug]))
        self.assertIn(self.unisa_c.pk, cm.community_ids_for(self.fresh(self.student)))
        self.assertTrue(ProgrammeEnrolment.objects.filter(person=self.student.profile).count() == 1)
