"""Registration step 1: no device question, the guardian privacy notice, and a
profile picture that previews, survives a failed submit and replaces the old one."""

import io
import shutil
import tempfile

from allauth.account.models import EmailAddress
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from .models import Person

User = get_user_model()
MEDIA = tempfile.mkdtemp(prefix='metanoia-test-media-')


def png(name='me.png', colour=(11, 95, 198)):
    """A small solid-colour PNG upload."""
    from PIL import Image
    buf = io.BytesIO()
    Image.new('RGB', (40, 40), colour).save(buf, 'PNG')
    return SimpleUploadedFile(name, buf.getvalue(), content_type='image/png')


@override_settings(MEDIA_ROOT=MEDIA, SECURE_SSL_REDIRECT=False)
class Step1Tests(TestCase):
    """Registration step 1: the questions it asks and keeping an uploaded picture."""
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(MEDIA, ignore_errors=True)

    def setUp(self):
        self.user = User.objects.create_user('reg@example.com', 'reg@example.com', 'Correct-Horse-9!')
        EmailAddress.objects.create(user=self.user, email=self.user.email, verified=True, primary=True)
        Person.objects.filter(user=self.user).update(user_type='student', registered=False,
                                                     profile_status=False)
        self.client.force_login(self.user)
        self.url = reverse('accounts:register')

    def test_no_device_question(self):
        r = self.client.get(self.url)
        self.assertEqual(r.status_code, 200)
        self.assertNotContains(r, 'primary_device')
        self.assertNotContains(r, 'Primary learning device')

    def test_guardian_privacy_notice(self):
        r = self.client.get(self.url)
        self.assertContains(r, 'Privacy notice')
        self.assertContains(r, 'cannot read your messages')

    def test_picture_survives_a_failed_submit_and_shows_in_the_preview(self):
        """The picture survives a submit that fails on the missing contact fields, and shows in the preview."""
        r = self.client.post(self.url, {'basics-first_name': 'Reg', 'basics-last_name': 'Istrant',
                                        'basics-profile_picture': png()})
        self.assertEqual(r.status_code, 200)
        person = Person.objects.get(user=self.user)
        self.assertTrue(person.profile_picture)
        self.assertContains(r, person.profile_picture.url)
        self.assertContains(r, 'Your current picture is shown')

    def test_a_new_picture_replaces_and_deletes_the_old_one(self):
        self.client.post(self.url, {'basics-first_name': 'Reg', 'basics-profile_picture': png('a.png')})
        person = Person.objects.get(user=self.user)
        old_name = person.profile_picture.name
        storage = person.profile_picture.storage
        self.assertTrue(storage.exists(old_name))

        self.client.post(self.url, {'basics-first_name': 'Reg',
                                    'basics-profile_picture': png('b.png', (242, 169, 0))})
        person.refresh_from_db()
        self.assertNotEqual(person.profile_picture.name, old_name)
        self.assertTrue(storage.exists(person.profile_picture.name))
        self.assertFalse(storage.exists(old_name), 'the replaced picture must be deleted')

    def test_settings_page_replaces_the_picture_too(self):
        Person.objects.filter(user=self.user).update(registered=True, profile_status=True)
        self.client.post(reverse('accounts:settings'), {'form_name': 'profile', 'profile_picture': png('a.png')})
        person = Person.objects.get(user=self.user)
        old_name = person.profile_picture.name
        self.assertTrue(old_name)
        self.client.post(reverse('accounts:settings'), {'form_name': 'profile', 'profile_picture': png('b.png')})
        person.refresh_from_db()
        self.assertNotEqual(person.profile_picture.name, old_name)
        self.assertFalse(person.profile_picture.storage.exists(old_name))
