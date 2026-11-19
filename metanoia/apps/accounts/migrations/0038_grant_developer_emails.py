"""Make the accounts in settings.DEVELOPER_EMAILS developers (and administrators).

Only accounts that already exist are touched; none are created. An address
counts when it is the account's e-mail AND either verified in allauth or the
account is already an administrator / Django staff — so an unverified sign-up
for somebody else's address is not promoted. Accounts created later are handled
by apps.accounts.developers (on e-mail confirmation and at sign-in).
"""

from django.conf import settings
from django.db import migrations
from django.db.models import Q


def grant(apps, schema_editor):
    User = apps.get_model('auth', 'User')
    Person = apps.get_model('accounts', 'Person')
    EmailAddress = apps.get_model('account', 'EmailAddress')
    emails = {(e or '').strip().lower() for e in getattr(settings, 'DEVELOPER_EMAILS', ()) if e}
    if not emails:
        return
    match = Q()
    for email in emails:
        match |= Q(email__iexact=email)
    for user in User.objects.filter(match):
        person = Person.objects.filter(user=user).first()
        if person is None:
            continue
        verified = EmailAddress.objects.filter(
            user=user, email__iexact=user.email, verified=True).exists()
        trusted = person.user_type == 'admin' or user.is_staff or user.is_superuser
        if not (verified or trusted):
            continue
        person.is_developer = True
        person.user_type = 'admin'
        person.save(update_fields=['is_developer', 'user_type'])
        if not user.is_staff:
            user.is_staff = True
            user.save(update_fields=['is_staff'])


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0037_person_is_developer'),
        ('account', '0001_initial'),
        ('auth', '0012_alter_user_first_name_max_length'),
    ]

    operations = [
        migrations.RunPython(grant, migrations.RunPython.noop),
    ]
