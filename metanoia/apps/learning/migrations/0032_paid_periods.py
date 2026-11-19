from datetime import timedelta

import django.db.models.deletion
from django.db import migrations, models
from django.utils import timezone

PAID_DAYS = 33


def record_current_runs(apps, schema_editor):
    Enrolment = apps.get_model('learning', 'ModuleEnrolment')
    Period = apps.get_model('learning', 'PaidPeriod')
    rows = []
    for e in Enrolment.objects.filter(paid_until__isnull=False):
        start = e.paid_from
        if start is None and e.started_at:
            start = timezone.localdate(e.started_at)
        if start is None:
            start = e.paid_until - timedelta(days=PAID_DAYS)
        rows.append(Period(enrolment_id=e.pk, starts_on=start, ends_on=e.paid_until))
    Period.objects.bulk_create(rows)


class Migration(migrations.Migration):

    dependencies = [
        ('learning', '0031_week_make_available_and_paid_from'),
    ]

    operations = [
        migrations.CreateModel(
            name='PaidPeriod',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('starts_on', models.DateField()),
                ('ends_on', models.DateField()),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('enrolment', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='paid_periods', to='learning.moduleenrolment')),
            ],
            options={'ordering': ['enrolment', 'starts_on']},
        ),
        migrations.RunPython(record_current_runs, migrations.RunPython.noop),
    ]
