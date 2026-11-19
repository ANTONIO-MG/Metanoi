"""Deleting an account must work, and leave an audit row behind."""
from django.contrib.auth import get_user_model
from django.test import TestCase

from .models import ActivityLog

User = get_user_model()


class UserDeleteTests(TestCase):
    """Deleting a user writes a ``delete`` audit row and does not roll back."""
    def test_delete_with_the_profile_already_loaded(self):
        """Delete a user whose ``profile`` is already cached.

        The Django admin (and any code that shows the person's name first) has
        ``user.profile`` cached when it deletes — that used to make the audit row point
        at the Person being deleted, and the delete rolled back.
        """
        user = User.objects.create_user('gone@example.com', 'gone@example.com', 'x-Pass-9!')
        self.assertIsNotNone(user.profile)
        user.delete()
        self.assertFalse(User.objects.filter(email='gone@example.com').exists())
        log = ActivityLog.objects.filter(action='delete').latest('timestamp')
        self.assertIsNone(log.target_user)
        self.assertIn('gone@example.com', log.description)
