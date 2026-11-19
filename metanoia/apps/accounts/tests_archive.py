"""Deleting an account in the advanced admin archives it; the purge deletes it.

What the client asked for: an account deleted in the (Django) advanced admin
is not gone at once. It is deactivated and stamped archived (who, when), it
disappears from the LMS — rosters, counts, finance totals — while staying
visible in the advanced admin, it can be restored for 5 days, and only then
does the scheduled purge delete it with everything tied to it.
"""

import tempfile
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.finance import models as finance
from apps.finance.context_processors import _build_finance_stats
from apps.scheduler.jobs import JOBS_BY_NAME

from . import archiving
from .models import ActivityLog, Person, UndoAction, UserSettings

User = get_user_model()


def _person(user, user_type, **extra):
    """Make ``user``'s Person a registered ``user_type``, with ``extra`` fields set."""
    person = Person.all_objects.get(user=user)
    person.user_type = user_type
    person.registered = True
    person.profile_status = True
    for key, value in extra.items():
        setattr(person, key, value)
    person.save()
    return person


class ArchiveOnDeleteTests(TestCase):
    """An admin "delete" archives; archived accounts vanish from the LMS, restore, and purge after five days."""
    def setUp(self):
        self.admin = User.objects.create_superuser('root', 'root@example.com', 'pw-12345!x')
        _person(self.admin, 'admin')
        self.student = User.objects.create_user('sipho', 'sipho@example.com', 'pw-12345!x')
        _person(self.student, 'student')
        self.other = User.objects.create_user('lindi', 'lindi@example.com', 'pw-12345!x')
        _person(self.other, 'student')
        self.client.force_login(self.admin)

    def _assert_archived(self, user):
        """The user row is still there but cannot sign in, is flagged closing by the admin, and is audited."""
        user.refresh_from_db()
        self.assertFalse(user.is_active)
        settings_row = UserSettings.objects.get(user=user)
        self.assertTrue(settings_row.is_closing)
        self.assertEqual(settings_row.closed_by, self.admin)
        expected = timezone.now() + timedelta(days=UserSettings.ADMIN_GRACE_DAYS)
        self.assertLess(abs((settings_row.purge_at - expected).total_seconds()), 120)
        self.assertTrue(ActivityLog.objects.filter(action='archive', target_user__user=user).exists())

    def test_single_delete_in_admin_archives_instead(self):
        url = reverse('admin:auth_user_delete', args=[self.student.pk])
        page = self.client.get(url)
        self.assertContains(page, 'archived')
        response = self.client.post(url, {'post': 'yes'}, follow=True)
        self.assertEqual(response.status_code, 200)
        self._assert_archived(self.student)
        self.assertContains(response, reverse('accounts:undo',
                                              args=[UndoAction.objects.get(kind='archive').pk]))

    def test_bulk_delete_selected_archives_instead(self):
        url = reverse('admin:auth_user_changelist')
        data = {'action': 'delete_selected', '_selected_action': [self.student.pk, self.other.pk]}
        confirm = self.client.post(url, data)
        self.assertContains(confirm, 'archived')
        self.client.post(url, {**data, 'post': 'yes'})
        self._assert_archived(self.student)
        self._assert_archived(self.other)

    def test_deleting_the_person_archives_the_account(self):
        person = Person.all_objects.get(user=self.student)
        self.client.post(reverse('admin:accounts_person_delete', args=[person.pk]), {'post': 'yes'})
        self._assert_archived(self.student)
        self.assertTrue(Person.all_objects.filter(pk=person.pk).exists())

    def test_archived_accounts_stay_visible_in_the_advanced_admin(self):
        archiving.archive_account(self.student, by=self.admin)
        page = self.client.get(reverse('admin:accounts_person_changelist') + '?archived=yes')
        self.assertContains(page, 'sipho@example.com')
        page = self.client.get(reverse('admin:auth_user_changelist') + '?archived=yes')
        self.assertContains(page, 'sipho@example.com')

    def test_cannot_archive_yourself(self):
        with self.assertRaises(ValueError):
            archiving.archive_account(self.admin, by=self.admin)

    def test_archived_accounts_leave_student_counts(self):
        """Archived students leave every count, while ``user.profile`` still works for code that holds the user."""
        from apps.analytics.services import student_summary
        from apps.staffdesk.dashboards import _ops_people
        from core.student_view import exclude_previews

        before_people = Person.objects.filter(user_type='student').count()
        before_summary = student_summary()['total']
        before_ops = _ops_people(timezone.now())['total']
        archiving.archive_account(self.student, by=self.admin)

        self.assertEqual(Person.objects.filter(user_type='student').count(), before_people - 1)
        self.assertFalse(Person.objects.filter(user=self.student).exists())
        self.assertEqual(student_summary()['total'], before_summary - 1)
        self.assertEqual(_ops_people(timezone.now())['total'], before_ops - 1)
        self.assertFalse(exclude_previews(User.objects.filter(pk=self.student.pk)).exists())
        self.assertEqual(User.objects.get(pk=self.student.pk).profile.user_type, 'student')

    def test_archived_accounts_leave_finance_totals(self):
        """Archived accounts leave the finance totals, while the books (``all_objects``) still hold the invoices."""
        from apps.staffdesk.dashboards import _ops_money, balance_of

        def bill(user, amount, paid):
            """An overdue invoice for ``user`` of ``amount``, with ``paid`` recorded against it."""
            invoice = finance.Invoice.objects.create(
                customer=user, status=finance.Invoice.STATUS_SENT,
                due_date=timezone.localdate() - timedelta(days=3))
            finance.InvoiceItem.objects.create(invoice=invoice, description='Fees', quantity=1,
                                               unit_price=Decimal(amount))
            if paid:
                finance.InvoicePayment.objects.create(invoice=invoice, amount=Decimal(paid))
            invoice.refresh_status()
            return invoice

        bill(self.student, '1000', '400')
        bill(self.other, '300', '100')
        request = RequestFactory().get('/')
        request.user = self.admin

        def totals():
            """The dashboard finance figures that must drop the archived account."""
            stats = _build_finance_stats(request)
            return (stats['total_invoiced'], stats['total_received'],
                    balance_of(finance.Invoice.objects.all()),
                    _ops_money()['collected_month'], finance.Invoice.objects.count())

        self.assertEqual(totals(), (Decimal('1300'), Decimal('500'), Decimal('800'),
                                    Decimal('500'), 2))
        archiving.archive_account(self.student, by=self.admin)
        self.assertEqual(totals(), (Decimal('300'), Decimal('100'), Decimal('200'),
                                    Decimal('100'), 1))
        self.assertEqual(finance.Invoice.all_objects.count(), 2)

    def test_restore_through_the_undo(self):
        action = archiving.archive_account(self.student, by=self.admin)
        response = self.client.post(reverse('accounts:undo', args=[action.pk]),
                                    {'reason': 'Deleted the wrong Sipho'})
        self.assertEqual(response.status_code, 302)
        self.student.refresh_from_db()
        self.assertTrue(self.student.is_active)
        self.assertFalse(UserSettings.objects.get(user=self.student).is_closing)
        self.assertTrue(Person.objects.filter(user=self.student).exists())
        self.assertTrue(ActivityLog.objects.filter(action='restore', target_user__user=self.student).exists())

    def test_restore_from_closed_accounts_page(self):
        """Reactivating from the Closed accounts page restores the account and spends the deletion's undo."""
        archiving.archive_account(self.student, by=self.admin)
        page = self.client.get(reverse('accounts:closed-accounts'))
        self.assertContains(page, 'sipho@example.com')
        self.client.post(reverse('accounts:reactivate-account', args=[self.student.pk]))
        self.student.refresh_from_db()
        self.assertTrue(self.student.is_active)
        self.assertTrue(Person.objects.filter(user=self.student).exists())
        self.assertIsNotNone(UndoAction.objects.get(kind='archive').undone_at)

    def test_restore_in_the_advanced_admin(self):
        archiving.archive_account(self.student, by=self.admin)
        self.client.post(reverse('admin:auth_user_changelist'),
                         {'action': 'restore_selected', '_selected_action': [self.student.pk]})
        self.student.refresh_from_db()
        self.assertTrue(self.student.is_active)

    def test_non_staff_cannot_restore(self):
        action = archiving.archive_account(self.student, by=self.admin)
        self.client.force_login(self.other)
        self.client.post(reverse('accounts:undo', args=[action.pk]), {'reason': 'me'})
        self.student.refresh_from_db()
        self.assertFalse(self.student.is_active)

    def test_purge_waits_five_days_then_deletes_everything(self):
        """Four days after archiving nothing happens; after six the account and everything tied to it
        are purged, and other accounts are untouched.
        """
        self.assertIn('purge-closed-accounts', JOBS_BY_NAME)
        invoice = finance.Invoice.objects.create(customer=self.student)
        person_pk = Person.all_objects.get(user=self.student).pk
        archiving.archive_account(self.student, by=self.admin)
        user_settings = UserSettings.objects.get(user=self.student)

        with tempfile.TemporaryDirectory() as tmp, override_settings(BASE_DIR=tmp, MEDIA_ROOT=tmp):
            UserSettings.objects.filter(pk=user_settings.pk).update(
                closed_at=timezone.now() - timedelta(days=4),
                purge_at=timezone.now() + timedelta(days=1))
            call_command('purge_closed_accounts', verbosity=0)
            self.assertTrue(User.objects.filter(pk=self.student.pk).exists())

            UserSettings.objects.filter(pk=user_settings.pk).update(
                closed_at=timezone.now() - timedelta(days=6),
                purge_at=timezone.now() - timedelta(days=1))
            call_command('purge_closed_accounts', verbosity=0)
        self.assertFalse(User.objects.filter(pk=self.student.pk).exists())
        self.assertFalse(Person.all_objects.filter(pk=person_pk).exists())
        self.assertFalse(finance.Invoice.all_objects.filter(pk=invoice.pk).exists())
        self.assertTrue(User.objects.filter(pk=self.other.pk, is_active=True).exists())
