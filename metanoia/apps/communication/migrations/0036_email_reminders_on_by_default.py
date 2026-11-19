from django.db import migrations, models


def switch_on(apps, schema_editor):
    Pref = apps.get_model('communication', 'NotificationPreference')
    Pref.objects.filter(email_unsubscribed_at__isnull=True).update(email_reminders=True)


class Migration(migrations.Migration):

    dependencies = [
        ('communication', '0035_community_membership_for_exploring'),
    ]

    operations = [
        migrations.AlterField(
            model_name='notificationpreference',
            name='email_reminders',
            field=models.BooleanField(default=True, help_text='Upcoming live sessions, due dates and "where you left off" nudges.', verbose_name='Reminders by e-mail'),
        ),
        migrations.RunPython(switch_on, migrations.RunPython.noop),
    ]
