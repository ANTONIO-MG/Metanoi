"""Refresh ``content/reviews.json`` from the Google reviews on the business profile.

Why an API call and not a crawler
---------------------------------
The obvious approach — fetch the public Google Maps / Search page and read the
reviews out of the HTML — does not work and cannot be made to work reliably.
Both pages return a JavaScript shell with no review text in it, they gate
non-browser clients, and scraping them is against Google's terms of service, so
anything built that way breaks the first time Google changes a class name.

The supported route is the Places API, which returns the same reviews as
structured data. It needs two settings:

``GOOGLE_PLACES_API_KEY``
    An API key from the Google Cloud console with the **Places API (New)**
    enabled. Free tier covers a nightly sync many times over.
``GOOGLE_PLACE_ID``
    The profile's place ID — find it with Google's Place ID Finder, or read it
    off a ``maps.google.com/?cid=`` / ``place_id:`` link to the profile.

Note the API's own limit: it returns **at most five** reviews per place, chosen
by Google. There is no supported way to pull every review, so this syncs the
five Google surfaces and leaves anything else in the file alone.

Usage::

    python manage.py sync_google_reviews            # write content/reviews.json
    python manage.py sync_google_reviews --dry-run  # print what it would write

Run it from cron if you want the wall to keep itself current. Without the two
settings the command exits with a clear error and the hand-maintained file is
left exactly as it is.

``FIELD_MASK`` asks for only the fields we render: Places (New) bills by field
mask, so a narrow mask is both cheaper and faster.
"""

import json
from datetime import timezone as dt_timezone
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from core.content import CONTENT_DIR, reload as reload_content

REVIEWS_FILE = Path(CONTENT_DIR) / 'reviews.json'

PLACES_ENDPOINT = 'https://places.googleapis.com/v1/places/{place_id}'
FIELD_MASK = 'id,displayName,googleMapsUri,rating,userRatingCount,reviews'


class Command(BaseCommand):
    """``manage.py sync_google_reviews``: rewrite the reviews file from the Places API."""

    help = "Rewrite content/reviews.json from the Google Places API."

    def add_arguments(self, parser):
        """``--dry-run`` and ``--place-id``."""
        parser.add_argument('--dry-run', action='store_true',
                            help='Print the reviews that would be written, and change nothing.')
        parser.add_argument('--place-id', default=None,
                            help='Override settings.GOOGLE_PLACE_ID for this run.')

    def handle(self, *args, **options):
        """Fetch the place, keep the reviews that have text, and write (or print) the file.

        Star-only ratings with no words are skipped. ``when`` keeps Google's own
        wording ("2 months ago"), so the card reads the way the profile does
        rather than inventing a date format. The file's ``_about`` note is kept,
        and the content cache is reloaded so a running process picks the new
        file up. Raises :class:`CommandError` when the settings are missing,
        the API fails, or no review has text.
        """
        import requests

        api_key = getattr(settings, 'GOOGLE_PLACES_API_KEY', '')
        place_id = options['place_id'] or getattr(settings, 'GOOGLE_PLACE_ID', '')
        if not api_key or not place_id:
            raise CommandError(
                'GOOGLE_PLACES_API_KEY and GOOGLE_PLACE_ID must both be set (see .env). '
                'Until they are, content/reviews.json is maintained by hand — see the '
                '"_about" note at the top of that file.')

        response = requests.get(
            PLACES_ENDPOINT.format(place_id=place_id),
            headers={'X-Goog-Api-Key': api_key, 'X-Goog-FieldMask': FIELD_MASK},
            timeout=20)
        if response.status_code != 200:
            raise CommandError(
                f'Places API returned {response.status_code}: {response.text[:400]}')
        place = response.json()

        items = []
        for review in place.get('reviews') or []:
            text = ((review.get('originalText') or review.get('text') or {}).get('text') or '').strip()
            if not text:
                continue
            items.append({
                'name': (review.get('authorAttribution') or {}).get('displayName', '').strip(),
                'avatar': (review.get('authorAttribution') or {}).get('photoUri', ''),
                'rating': review.get('rating'),
                'text': text,
                'when': (review.get('relativePublishTimeDescription') or '').strip(),
                'source': 'google',
            })

        if not items:
            raise CommandError('The Places API returned no reviews with text; '
                               'content/reviews.json was left untouched.')

        existing = {}
        if REVIEWS_FILE.exists():
            try:
                existing = json.loads(REVIEWS_FILE.read_text(encoding='utf-8'))
            except ValueError:
                existing = {}

        payload = {
            '_about': existing.get('_about', ''),
            'source': 'Google',
            'profile_url': place.get('googleMapsUri') or existing.get('profile_url', ''),
            'rating': place.get('rating'),
            'count': place.get('userRatingCount'),
            'synced_at': timezone.now().astimezone(dt_timezone.utc).strftime('%Y-%m-%d'),
            'items': items,
        }

        if options['dry_run']:
            self.stdout.write(json.dumps(payload, indent=2, ensure_ascii=False))
            self.stdout.write(self.style.WARNING('\n--dry-run: nothing written.'))
            return

        REVIEWS_FILE.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
        reload_content()
        self.stdout.write(self.style.SUCCESS(
            f'Wrote {len(items)} review(s) to {REVIEWS_FILE} '
            f'({payload["rating"]}★ from {payload["count"]} ratings).'))
