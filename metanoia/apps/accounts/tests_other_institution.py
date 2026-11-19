"""Registration's "Choose" step and /join/: the one-on-one path, and "Other
institution" — which opens boxes for the institution and the cohort or year,
required then, and stored on the person."""
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import OneOnOneRequest, Person
from apps.accounts.tests import captcha_test_mode
from apps.accounts.tests_one_on_one import _registrant, _team
from core.testing import make_module, make_programme

User = get_user_model()


class OtherInstitutionSetup(TestCase):
    """A listed institution (Milpark, PGDA) and the "Other institution" row with a curated programme."""
    @classmethod
    def setUpTestData(cls):
        cls.pgda = make_programme('MILPARK', 'PGDA')
        cls.milpark = cls.pgda.institution
        cls.curated = make_programme('OTHER', 'CURATED')
        cls.other = cls.curated.institution
        cls.other.name = 'Other institution'
        cls.other.save()
        cls.fr = make_module('Financial Reporting', 'FREP', programme=cls.pgda, price=500)
        cls.curated_fr = make_module('Curated Financial Reporting', 'FR', programme=cls.curated, price=500)


@mock.patch('apps.communication.welcome.send_welcome_pack', lambda *a, **k: None)
class ChooseStepTests(OtherInstitutionSetup):
    """Registration step 2 with "Other institution": the page, the programme path and the one-on-one path."""
    def setUp(self):
        _team('admin@example.com')
        self.user, self.person = _registrant('nomsa@example.com')
        self.client.force_login(self.user)
        self.url = reverse('accounts:register-course')

    def post(self, **extra):
        """Post step 2 choosing the Other institution's curated programme, with ``extra`` overrides."""
        data = {'institution_id': self.other.pk, 'programme_id': self.curated.pk,
                'module_ids': [self.curated_fr.pk], 'cohort_id': 'year'}
        data.update(extra)
        return self.client.post(self.url, data)

    def test_institution_label_is_linked_to_its_select(self):
        r = self.client.get(self.url)
        self.assertContains(r, '<label class="form-label text-sm fw-bold" for="institutionSelect">Institution</label>',
                            html=True)

    def test_page_offers_both_ways_to_register(self):
        """Step 2 offers both ways to register, with the "Other" boxes present but closed until Other is picked."""
        r = self.client.get(self.url)
        self.assertContains(r, 'Programme &amp; modules')
        self.assertContains(r, 'One-on-one coaching')
        self.assertContains(r, 'R 300')
        self.assertEqual(r.context['reg_path'], 'programme')
        self.assertContains(r, 'name="other_institution"')
        self.assertContains(r, 'name="other_cohort"')
        r = self.client.get(self.url + '?path=oneonone')
        self.assertEqual(r.context['reg_path'], 'oneonone')
        self.assertContains(r, 'name="o2o-institution_choice"')

    def test_other_is_flagged_for_the_picker(self):
        data = self.client.get(self.url).context['institutions_data']
        self.assertEqual({d['code']: d['other'] for d in data}, {'MILPARK': False, 'OTHER': True})

    def test_other_requires_institution_and_cohort(self):
        r = self.post()
        self.assertEqual(r.status_code, 400)
        self.assertEqual(set(r.context['other']['errors']), {'institution', 'cohort'})
        self.assertContains(r, 'Required when you choose', status_code=400)
        self.assertContains(r, 'alert-danger', status_code=400)
        self.assertNotIn('reg', self.client.session)
        r = self.post(other_institution='University of Johannesburg')
        self.assertEqual(set(r.context['other']['errors']), {'cohort'})

    def test_other_is_stored_on_the_person(self):
        r = self.post(other_institution='  University of Johannesburg ', other_cohort='2027 intake')
        self.assertRedirects(r, reverse('accounts:register-review'), fetch_redirect_response=False)
        self.person.refresh_from_db()
        self.assertEqual((self.person.other_institution, self.person.other_cohort),
                         ('University of Johannesburg', '2027 intake'))
        reg = self.client.session['reg']
        self.assertEqual((reg['other_institution'], reg['other_cohort']),
                         ('University of Johannesburg', '2027 intake'))

    def test_listed_institution_ignores_the_boxes(self):
        r = self.post(institution_id=self.milpark.pk, programme_id=self.pgda.pk,
                      module_ids=[self.fr.pk], other_institution='Ignored')
        self.assertRedirects(r, reverse('accounts:register-review'), fetch_redirect_response=False)
        self.person.refresh_from_db()
        self.assertEqual(self.person.other_institution, '')

    def test_errors_are_shown_as_errors_not_info(self):
        r = self.post()
        self.assertContains(r, 'alert-danger', status_code=400)
        self.assertNotContains(r, 'app-alert alert alert-info', status_code=400)

    def o2o(self, **extra):
        """Post the one-on-one request choosing the Other institution, with ``extra`` overrides."""
        data = {'o2o-institution_choice': self.other.pk, 'o2o-programme': 'BCom Accounting',
                'o2o-start_date': '2027-01-15', 'o2o-end_date': '2027-06-30'}
        data.update({f'o2o-{k}': v for k, v in extra.items()})
        return self.client.post(reverse('accounts:register-one-on-one'), data)

    def test_one_on_one_other_requires_both_boxes(self):
        r = self.o2o()
        self.assertEqual(r.status_code, 400)
        form = r.context['one_on_one_form']
        self.assertIn('other_institution', form.errors)
        self.assertIn('other_cohort', form.errors)
        self.assertEqual(r.context['reg_path'], 'oneonone')
        self.assertFalse(OneOnOneRequest.objects.exists())

    def test_one_on_one_other_is_recorded(self):
        r = self.o2o(other_institution='University of Johannesburg', other_cohort='3rd year')
        self.assertRedirects(r, reverse('accounts:register-complete') + '?mode=oneonone-request',
                             fetch_redirect_response=False)
        req = OneOnOneRequest.objects.get()
        self.assertEqual((req.institution, req.cohort, req.programme),
                         ('University of Johannesburg', '3rd year', 'BCom Accounting'))
        self.person.refresh_from_db()
        self.assertEqual((self.person.other_institution, self.person.other_cohort),
                         ('University of Johannesburg', '3rd year'))
        self.assertTrue(self.person.registered)

    def test_one_on_one_listed_institution_uses_its_name(self):
        self.o2o(**{'institution_choice': self.milpark.pk})
        req = OneOnOneRequest.objects.get()
        self.assertEqual((req.institution, req.cohort), (self.milpark.name, ''))

    def test_one_on_one_needs_an_institution(self):
        r = self.o2o(**{'institution_choice': ''})
        self.assertEqual(r.status_code, 400)
        self.assertIn('institution_choice', r.context['one_on_one_form'].errors)


class JoinOtherInstitutionTests(OtherInstitutionSetup):
    """Joining the Metanoia Community from an "Other institution"."""
    def payload(self, **over):
        """A valid join form for the Other institution, with ``over`` replacing fields."""
        data = {'first_name': 'Nia', 'last_name': 'New', 'email': 'nia@example.com',
                'phone': '082 123 4567', 'institution': self.other.pk, 'programme': '',
                'password': 'abc', 'terms': 'on', 'captcha_0': 'PASSED', 'captcha_1': 'PASSED'}
        data.update(over)
        return data

    def test_join_page_has_the_other_boxes(self):
        r = self.client.get(reverse('community-join'))
        self.assertContains(r, 'name="other_institution"')
        self.assertContains(r, 'name="other_cohort"')
        self.assertContains(r, 'data-other="1"')

    @captcha_test_mode()
    def test_other_requires_institution_and_cohort(self):
        r = self.client.post(reverse('community-join'), self.payload())
        self.assertEqual(r.status_code, 200)
        self.assertIn('other_institution', r.context['form'].errors)
        self.assertIn('other_cohort', r.context['form'].errors)
        self.assertFalse(User.objects.filter(email='nia@example.com').exists())

    @captcha_test_mode()
    def test_other_member_is_created_with_what_they_typed(self):
        r = self.client.post(reverse('community-join'), self.payload(
            other_institution='University of Johannesburg', other_cohort='2027'))
        self.assertRedirects(r, reverse('pages:dashboard'), fetch_redirect_response=False)
        p = Person.objects.get(user__email='nia@example.com')
        self.assertEqual((p.community_institution, p.community_programme), (self.other, None))
        self.assertEqual((p.other_institution, p.other_cohort), ('University of Johannesburg', '2027'))

    @captcha_test_mode()
    def test_listed_institution_still_needs_a_programme(self):
        r = self.client.post(reverse('community-join'), self.payload(institution=self.milpark.pk))
        self.assertIn('programme', r.context['form'].errors)
        self.assertFalse(User.objects.filter(email='nia@example.com').exists())
        r = self.client.post(reverse('community-join'), self.payload(
            institution=self.milpark.pk, programme=self.pgda.pk, other_institution='Ignored'))
        p = Person.objects.get(user__email='nia@example.com')
        self.assertEqual((p.community_programme, p.other_institution), (self.pgda, ''))
