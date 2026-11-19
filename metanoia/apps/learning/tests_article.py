"""Reading lessons render as a Metanoia blog post (apps/learning/article.py)."""

from django.test import TestCase

from apps.learning import article


class SentenceCaseTests(TestCase):
    """Shouted guide headings are put into sentence case without losing acronyms."""
    def test_shouted_headings_read_normally_and_keep_their_acronyms(self):
        self.assertEqual(article.sentence_case('P26F TEST 1 — WHAT IT TOOK OFF THE TABLE'),
                         'P26F test 1 — what it took off the table')
        self.assertEqual(article.sentence_case('SOCIE — NCI OR RE COLUMN'), 'SOCIE — NCI or RE column')
        self.assertEqual(article.sentence_case('FINAL PREDICTION TABLE — APRIL 2026'),
                         'Final prediction table — April 2026')


class TransformTests(TestCase):
    """A study guide's HTML becomes article HTML with a table of contents."""
    HTML = (
        '<p><strong>FREP TEST 2 — FINAL STRATEGIC BLUEPRINT | P26F</strong></p>'
        '<div class="lesson-callout"><p><strong>SECTION 1: WHAT IS TESTED — TOPIC FREQUENCY</strong></p></div>'
        '<div class="lesson-callout"><p><strong>THE 3 RULES: (1) State the principle. (2) Show working.</strong></p></div>'
        '<table class="table"><thead><tr><th>Topic</th></tr></thead><tbody><tr><td>IFRS 3</td></tr></tbody></table>'
    )

    def test_guide_structure_becomes_article_structure(self):
        """The duplicate title is dropped, sections become headings and a key point
        keeps its label.
        """
        html, headings = article.transform(self.HTML, title='FREP Test 2 - Final Strategic Blueprint')
        self.assertNotIn('FINAL STRATEGIC BLUEPRINT | P26F', html)
        self.assertEqual(headings[0]['kicker'], 'Section 1')
        self.assertEqual(headings[0]['label'], 'What is tested — topic frequency')
        self.assertIn('<h2 class="ar-h2" id="what-is-tested-topic-frequency">', html)
        self.assertIn('ar-callout__label">The 3 rules<', html)
        self.assertIn('<div class="ar-table"><table class="ar-table__grid">', html)


class IsArticleTests(TestCase):
    """Only pure reading renders as an article; anything interactive keeps the player.
    """
    def _row(self, kind='reading', blocks=('text',), **extra):
        """A player section row of ``kind`` holding blocks of the given types."""
        from unittest import mock
        section = mock.Mock(resolved_type=kind, assessments=[])
        return dict({'kind': 'section', 'section': section,
                     'blocks': [mock.Mock(block_type=b) for b in blocks]}, **extra)

    def test_pure_reading_is_an_article(self):
        self.assertTrue(article.is_article([self._row(), self._row(blocks=('text', 'image'))]))

    def test_video_quiz_or_lock_keeps_the_player(self):
        self.assertFalse(article.is_article([self._row(), self._row(kind='video')]))
        self.assertFalse(article.is_article([self._row(blocks=('text', 'quiz'))]))
        self.assertFalse(article.is_article([self._row(locked=True)]))
