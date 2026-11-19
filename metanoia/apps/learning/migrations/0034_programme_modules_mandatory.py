from django.db import migrations, models


def iac_is_all_modules(apps, schema_editor):
    Programme = apps.get_model('learning', 'Programme')
    Programme.objects.filter(code='IAC').update(modules_mandatory=True)


class Migration(migrations.Migration):

    dependencies = [
        ('learning', '0033_unguessable_upload_paths'),
    ]

    operations = [
        migrations.AddField(
            model_name='programme',
            name='modules_mandatory',
            field=models.BooleanField(
                default=False,
                help_text='Every active module is compulsory: registering for the programme '
                          'registers all of them, and students cannot pick a subset (IAC).'),
        ),
        migrations.RunPython(iac_is_all_modules, migrations.RunPython.noop),
    ]
