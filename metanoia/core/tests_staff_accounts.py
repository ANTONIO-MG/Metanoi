"""Team accounts: STAFF_PROFILES details are applied, blanks never wipe."""

from datetime import date
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.accounts.models import PersonContact
from core import staff_accounts

PASSWORD = "Seed-test-pw-1"
ADMIN = "admin@metanoiagroup.co.za"


def _profiles(**details):
    """STAFF_PROFILES with the administrator's row filled in as given, the rest blank."""
    rows = {email: dict(row) for email, row in staff_accounts.STAFF_PROFILES.items()}
    rows[ADMIN] = {**staff_accounts._BLANK_PROFILE, **details}
    return rows


def _run(**details):
    """Run :func:`~core.staff_accounts.ensure_staff_admins` with the administrator's profile row patched in."""
    with mock.patch.object(staff_accounts, "STAFF_PROFILES", _profiles(**details)):
        return staff_accounts.ensure_staff_admins(PASSWORD, log=lambda m: None)


class StaffProfileSeedTests(TestCase):
    """:func:`~core.staff_accounts.ensure_staff_admins` creates the team and applies their details."""
    def _admin(self):
        """The administrator's :class:`~apps.accounts.models.Person`."""
        return get_user_model().objects.get(email=ADMIN).profile

    def test_creates_the_four_role_mailboxes_verified_and_onboarded(self):
        users = _run()
        self.assertEqual(sorted(u.email for u in users),
                         ["accounts@metanoiagroup.co.za", ADMIN, "info@metanoiagroup.co.za",
                          "learninghub@metanoiagroup.co.za"])
        from allauth.account.models import EmailAddress
        for user in users:
            self.assertTrue(EmailAddress.objects.filter(user=user, verified=True, primary=True).exists())
            self.assertTrue(user.profile.registered and user.profile.profile_status)
        admins = {u.email for u in users if u.is_superuser}
        self.assertEqual(admins, {e for e, *_ in staff_accounts.STAFF_ADMINS})

    def test_filled_details_are_written(self):
        _run(title="Mr", gender="male", date_of_birth="1980-05-17", phone="082 123 4567",
             whatsapp="+27 82 123 4567", suburb="Florida", city="Roodepoort", province="Gauteng",
             ms_upn="Admin@Example.onmicrosoft.com", primary_device="laptop")
        p = self._admin()
        self.assertEqual((p.title, p.gender, p.primary_device), ("mr", "male", "laptop"))
        self.assertEqual(p.date_of_birth, date(1980, 5, 17))
        self.assertEqual(p.phone, "+27 821234567")
        self.assertEqual(p.whatsapp_number, "27821234567")
        self.assertIsNotNone(p.whatsapp_verified_at)
        self.assertEqual(p.ms_upn, "admin@example.onmicrosoft.com")
        c = PersonContact.objects.get(person=p)
        self.assertEqual((c.suburb, c.city, c.province, c.primary_phone), ("Florida", "Roodepoort", "Gauteng", p.phone))

    def test_blanks_never_wipe_what_was_entered_on_the_site(self):
        """A re-run with every detail blank keeps what was entered on the site.

        The site writes names to the profile (``accounts.names`` syncs the login), so
        a surname set on the profile must also survive on the user.
        """
        blank_surname = [(ADMIN, "Metanoia", "", "admin.png")]
        with mock.patch.object(staff_accounts, "STAFF_ADMINS", blank_surname):
            _run()
            p = self._admin()
            p.title, p.phone, p.date_of_birth = "dr", "+27 831112222", date(1975, 1, 2)
            p.last_name = "Surname"
            p.save()
            _run()
        p = self._admin()
        self.assertEqual((p.title, p.phone, p.date_of_birth), ("dr", "+27 831112222", date(1975, 1, 2)))
        self.assertEqual(get_user_model().objects.get(email=ADMIN).last_name, "Surname")

    def test_bad_value_stops_with_the_field_named(self):
        with self.assertRaisesMessage(ValueError, "gender 'm'"):
            _run(gender="m")
        with self.assertRaisesMessage(ValueError, ADMIN):
            _run(date_of_birth="17/05/1980")

    def test_rerun_keeps_whatsapp_verification_time(self):
        _run(whatsapp="0821234567")
        first = self._admin().whatsapp_verified_at
        _run(whatsapp="0821234567")
        self.assertEqual(self._admin().whatsapp_verified_at, first)

    def test_every_account_gets_a_generated_initials_avatar(self):
        """No photograph ships: each picture is a square PNG drawn from the account's initials."""
        from PIL import Image

        _run()
        for email in ("admin@metanoiagroup.co.za", "learninghub@metanoiagroup.co.za"):
            person = get_user_model().objects.get(email=email).profile
            with person.profile_picture.open("rb") as fh:
                image = Image.open(fh)
                image.load()
            self.assertEqual(image.format, "PNG", email)
            self.assertEqual(image.size, (staff_accounts.AVATAR_SIZE, staff_accounts.AVATAR_SIZE))
        self.assertEqual(staff_accounts.initials("Metanoia", "Learning Hub"), "LH")
        self.assertEqual(staff_accounts.initials("Metanoia", "Admin"), "MA")

    def test_a_picture_uploaded_later_is_kept(self):
        _run()
        person = self._admin()
        name = person.profile_picture.name
        _run()
        self.assertEqual(self._admin().profile_picture.name, name)
