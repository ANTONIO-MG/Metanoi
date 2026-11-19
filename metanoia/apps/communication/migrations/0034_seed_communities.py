"""Create the default Metanoia Community and one community per programme."""
from django.db import migrations
from django.utils.text import slugify


def seed(apps, schema_editor):
    Community = apps.get_model('communication', 'Community')
    Programme = apps.get_model('learning', 'Programme')
    if not Community.objects.filter(is_default=True).exists():
        Community.objects.create(name='Metanoia Community', slug='metanoia', is_default=True,
                                 description='Everyone on the Metanoia Learning Hub.')
    for programme in Programme.objects.select_related('institution'):
        if Community.objects.filter(programme=programme).exists():
            continue
        name = ' '.join(filter(None, [programme.institution.code, programme.name])).strip()
        base = slugify(name) or f'programme-{programme.pk}'
        slug, n = base, 2
        while Community.objects.filter(slug=slug).exists():
            slug, n = f'{base}-{n}', n + 1
        Community.objects.create(name=name, slug=slug, programme=programme,
                                 description=programme.full_name or '')


class Migration(migrations.Migration):
    dependencies = [
        ('communication', '0033_metanoia_community'),
        ('learning', '0029_materials_audience_cohort_phases_upload_queue'),
    ]
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
