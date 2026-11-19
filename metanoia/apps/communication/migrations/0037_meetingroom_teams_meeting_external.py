from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('communication', '0036_email_reminders_on_by_default'),
    ]

    operations = [
        migrations.AddField(
            model_name='meetingroom',
            name='teams_meeting_external',
            field=models.BooleanField(default=False),
        ),
    ]
