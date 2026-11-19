"""django-import-export resources for bulk student intake and roster export.

Keyed on **e-mail**: an existing address updates that person, a new one creates
the ``User`` and its ``Person``. Passwords are never imported.
"""

from django.contrib.auth import get_user_model
from import_export import fields, resources


from .models import Person

User = get_user_model()


class PersonResource(resources.ModelResource):
    """Import / export students (and any other Person) by e-mail address."""

    email = fields.Field(column_name='email', attribute='user__email', readonly=True)

    class Meta:
        """Rows are identified by ``email``; ``id`` is deliberately absent so a
        spreadsheet never has to carry internal primary keys.
        """
        model = Person
        import_id_fields = ('email',)
        fields = ('email', 'first_name', 'last_name', 'user_type', 'phone',
                  'gender', 'date_of_birth')
        export_order = fields
        skip_unchanged = True
        report_skipped = True

    def before_import_row(self, row, **kwargs):
        """Normalise the e-mail and ensure a ``User`` (and so its signal-made
        ``Person``) exists before the row is imported.

        No password is ever imported: a new account cannot be signed into until
        the person sets one via the password-reset e-mail.
        """
        email = (row.get('email') or '').strip().lower()
        if not email:
            raise ValueError('Every row needs an "email" column — it is how people are identified.')
        row['email'] = email
        user, created = User.objects.get_or_create(
            email=email,
            defaults={
                'username': email,
                'first_name': (row.get('first_name') or '').strip(),
                'last_name': (row.get('last_name') or '').strip(),
            },
        )
        if created:
            user.set_unusable_password()
            user.save(update_fields=['password'])

    def before_save_instance(self, instance, row, **kwargs):
        """A spreadsheet never changes a developer's role (only a developer does,
        on the members page — apps.accounts.developers), nor grants one."""
        if instance.pk and instance.is_developer:
            stored = Person.objects.filter(pk=instance.pk).values_list('user_type', flat=True).first()
            if stored and instance.user_type != stored:
                raise ValueError(f"{row.get('email')}: a developer's role cannot be changed by import.")
        super().before_save_instance(instance, row, **kwargs)

    def get_instance(self, instance_loader, row):
        """Look the profile up through its user's e-mail."""
        email = (row.get('email') or '').strip().lower()
        return Person.objects.filter(user__email=email).first()

    def dehydrate_email(self, person):
        """Export the account's e-mail address."""
        return person.user.email if person.user_id else ''
