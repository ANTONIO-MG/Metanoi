"""Drop the task tables — the tasks feature was removed (client request,
2026-11-09). Deletes every Task and TaskAssignment row.

The app stays installed (migrations only) so this runs on ``migrate`` and the
``accounts`` / ``myhub`` migrations that depend on earlier ``orgtasks``
migrations keep resolving. Nothing else has a foreign key to these tables.
"""

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('orgtasks', '0009_alter_task_attachment'),
    ]

    operations = [
        migrations.DeleteModel(name='TaskAssignment'),
        migrations.DeleteModel(name='Task'),
    ]
