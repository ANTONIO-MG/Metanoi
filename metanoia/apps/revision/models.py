"""Review cards: questions a student got wrong (or only partly right), brought
back on a Leitner schedule until they stick.

Box 1 comes back after a day, box 5 after five weeks. A card climbs a box each
time it is recalled and drops to box 1 when it is not. The question itself is
the card's content, so an educator correcting a question corrects every card.
"""

from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone


class ReviewCard(models.Model):
    """A question one student is revising, scheduled Leitner-style in boxes 1–5.

    ``INTERVALS`` gives the days until the next review once a card sits in each
    box. ``streak`` counts consecutive successful recalls and is reset by
    "Again"; ``lapses`` counts the times the card was forgotten — in review or by
    getting it wrong again in a paper. One card per user per question.
    """

    BOX_MIN = 1
    BOX_MAX = 5
    INTERVALS = {1: 1, 2: 3, 3: 7, 4: 16, 5: 35}

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                             related_name='review_cards')
    question = models.ForeignKey('assessments.Question', on_delete=models.CASCADE,
                                 related_name='review_cards')
    box = models.PositiveSmallIntegerField(default=1)
    due_at = models.DateTimeField(default=timezone.now, db_index=True)
    last_reviewed_at = models.DateTimeField(null=True, blank=True)
    streak = models.PositiveIntegerField(default=0)
    lapses = models.PositiveIntegerField(default=0)
    source_attempt = models.ForeignKey('assessments.AssessmentAttempt', on_delete=models.SET_NULL,
                                       null=True, blank=True, related_name='review_cards')
    created_at = models.DateTimeField(auto_now_add=True)
    suspended = models.BooleanField(default=False, help_text='Hidden from review by the student.')

    class Meta:
        ordering = ['due_at', 'id']
        constraints = [
            models.UniqueConstraint(fields=['user', 'question'], name='uniq_review_card_per_user_question'),
        ]
        indexes = [models.Index(fields=['user', 'suspended', 'due_at'])]

    def __str__(self):
        """Label shown in the admin."""
        return f'{self.user} · Q{self.question_id} (box {self.box})'

    @classmethod
    def interval_for(cls, box):
        """The review interval for ``box`` (clamped to ``BOX_MIN``–``BOX_MAX``)."""
        return timedelta(days=cls.INTERVALS[max(cls.BOX_MIN, min(cls.BOX_MAX, box))])

    @property
    def is_due(self):
        """Whether the card is not suspended and its due time has come."""
        return not self.suspended and self.due_at <= timezone.now()


class ReviewLog(models.Model):
    """One self-grade. Feeds the day streak and lets the schedule be audited."""

    AGAIN, HARD, GOOD, EASY = 'again', 'hard', 'good', 'easy'
    GRADE_CHOICES = [(AGAIN, 'Again'), (HARD, 'Hard'), (GOOD, 'Good'), (EASY, 'Easy')]

    card = models.ForeignKey(ReviewCard, on_delete=models.CASCADE, related_name='logs')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                             related_name='review_logs')
    grade = models.CharField(max_length=5, choices=GRADE_CHOICES)
    box_before = models.PositiveSmallIntegerField()
    box_after = models.PositiveSmallIntegerField()
    reviewed_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ['-reviewed_at']
        indexes = [models.Index(fields=['user', 'reviewed_at'])]

    def __str__(self):
        """Label shown in the admin."""
        return f'{self.user} · {self.grade} · {self.reviewed_at:%Y-%m-%d}'
