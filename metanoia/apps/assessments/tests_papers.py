"""Reading a past paper into parts and its memo into answers (apps.assessments.papers)."""

from django.test import SimpleTestCase

from . import papers

REQUIRED = """
© Milpark Education CGAU01-8 P22F Final Exam AFTERNOON REQUIRED  Page 2 of 2
AFTERNOON SESSION - REQUIRED ONLY
 Marks
Sub-
total
Total
a.  With reference to Section 2, describe the tests of control you
would perform on Aquafine's Revenue System in FY2022.

10
b. Discuss your concerns related to Aquafine's compliance with the
Companies Act (2008).  8
c. Describe the substantive procedures that SLM should perform:
i. Journal entry 306
ii. Journal entries 417


5
7
12
Total marks   30
"""

MEMO = """
P22F Final Exam Afternoon Paper - Suggested Solution
a.  With reference to Section 2, describe the tests of control you
would perform on Aquafine's Revenue System in FY2022.
10
Inspect the access logs for the revenue system and
confirm that only authorised staff posted credit notes.
(a) Whether the controller reviewed the exception report;
(b) Whether the review was evidenced.
b. Discuss your concerns related to Aquafine's compliance with the
Companies Act (2008).
8
The board did not file its annual return.
c. Describe the substantive procedures that SLM should perform:
12
Vouch journal entry 306 to supporting documents.
"""


class ParsePartsTests(SimpleTestCase):
    """:func:`papers.parse_parts` and :func:`papers.memo_for_parts` on a sample required
    and memo.
    """
    def test_parts_marks_and_total(self):
        """Parts, marks and total are read; sub-parts stay inside their part, on their own
        lines.
        """
        parts = papers.parse_parts(REQUIRED)
        self.assertEqual([p['label'] for p in parts], ['a', 'b', 'c'])
        self.assertEqual([p['marks'] for p in parts], [10, 8, 12])
        self.assertIn('tests of control', parts[0]['text'])
        self.assertIn('\nii. Journal entries 417', parts[2]['text'])
        self.assertEqual(papers.paper_total(REQUIRED, parts), 30)

    def test_memo_matches_parts_and_ignores_inner_lists(self):
        """"(b) Whether the review …" inside answer a belongs to answer a, not to part b.
        """
        parts = papers.parse_parts(REQUIRED)
        memo, agreement = papers.memo_for_parts(MEMO, parts)
        self.assertEqual(set(memo), {(None, 'a'), (None, 'b'), (None, 'c')})
        self.assertIn('Whether the review was evidenced', memo[(None, 'a')])
        self.assertTrue(memo[(None, 'b')].startswith('The board did not file'))
        self.assertGreater(agreement, 0.5)

    def test_memo_of_another_paper_disagrees(self):
        parts = papers.parse_parts(REQUIRED)
        other = 'a. Prepare the journal entries for the share-based payment.\n12\nDr Expense\n'
        _memo, agreement = papers.memo_for_parts(other, parts)
        self.assertLess(agreement, 0.2)


class KeyTests(SimpleTestCase):
    """Sitting keys, pair keys, display titles and kinds derived from file titles."""
    def test_paper_and_memo_share_a_key(self):
        self.assertEqual(papers.paper_key('P22F Final Exam — required (Afternoon)'),
                         papers.paper_key('P22F Final Exam — suggested solution (Afternoon)'))
        self.assertNotEqual(papers.paper_key('P23F Test 3 — required'),
                            papers.paper_key('P23F Test 1 — required'))

    def test_scenario_pairs_with_its_required(self):
        self.assertEqual(papers.pair_key('P25S FREP Afternoon Exam Required'),
                         papers.pair_key('P25S FREP Afternoon Exam Scenario'))
        self.assertNotEqual(papers.pair_key('P25S FREP Afternoon Exam Required'),
                            papers.pair_key('P25S_Afternoon_Required_FREP.pdf'))

    def test_titles_and_kinds(self):
        self.assertEqual(papers.tidy_title('P26F_FREP_Test_2_Required.pdf'), 'P26F FREP Test 2')
        self.assertEqual(papers.tidy_title('P22F Final Exam — required (Afternoon)'),
                         'P22F Final Exam (Afternoon)')
        self.assertEqual(papers.kind_for('P23F Test 3 — required', 'mock_exam'), 'test')
        self.assertEqual(papers.kind_for('P22F Final Exam — required', 'mock_exam'), 'mock_exam')
        self.assertEqual(papers.kind_for('INC AQ 2 Question', 'questions'), 'quiz')
