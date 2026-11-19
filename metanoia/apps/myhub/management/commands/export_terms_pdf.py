"""Write the Terms & Conditions and the Terms of Service to PDF.

Each PDF is rendered from the same template the site shows
(myhub/pages/_terms-body.html and _tos-body.html), so the downloadable copies
match the wording users accept. Run it whenever either wording changes:

    python manage.py export_terms_pdf

Per document it writes static/legal/<slug>.pdf (the site's download link — run
collectstatic afterwards) and docs/legal/<Name>-<version>.pdf (the archived copy
of that version).
"""
import io
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.template.loader import render_to_string

from apps.accounts.forms import TERMS_VERSION
from apps.finance.pdf import _link_callback
from apps.myhub.views import TOS_VERSION
from core.branding import strings

DOCUMENTS = [
    {'title': 'Terms & Conditions', 'body': 'myhub/pages/_terms-body.html',
     'version': TERMS_VERSION, 'slug': 'metanoia-terms-and-conditions',
     'archive': 'Metanoia-Terms-and-Conditions', 'subtitle_suffix': 'Learning Hub'},
    {'title': 'Terms of Service', 'body': 'myhub/pages/_tos-body.html',
     'version': TOS_VERSION, 'slug': 'metanoia-terms-of-service',
     'archive': 'Metanoia-Terms-of-Service', 'subtitle_suffix': 'website and public pages'},
]


class Command(BaseCommand):
    help = 'Render the Terms & Conditions and Terms of Service to PDF (static/legal and docs/legal).'

    def handle(self, *args, **options):
        from xhtml2pdf import pisa

        brand = strings().get('brand', {})
        base = Path(settings.BASE_DIR)
        for doc in DOCUMENTS:
            html = render_to_string('legal/terms_pdf.html', {
                'brand': brand,
                'doc_title': doc['title'],
                'body_template': doc['body'],
                'subtitle': f"{brand.get('name', '')} {doc['subtitle_suffix']}",
                'version': doc['version'],
                'terms_version': TERMS_VERSION,
                'tos_version': TOS_VERSION,
            })
            buf = io.BytesIO()
            if pisa.CreatePDF(html, dest=buf, link_callback=_link_callback).err:
                raise CommandError(f'xhtml2pdf could not render the {doc["title"]}.')
            for path in (base / 'static' / 'legal' / f"{doc['slug']}.pdf",
                         base / 'docs' / 'legal' / f"{doc['archive']}-{doc['version']}.pdf"):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(buf.getvalue())
                self.stdout.write(f'Wrote {path} ({len(buf.getvalue()) // 1024} KB)')
