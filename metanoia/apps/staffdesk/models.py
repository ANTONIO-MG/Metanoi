"""Staff desk persistence.

* :class:`Nudge` — the educator dashboard's "Nudge inactive students" button.
* :class:`RiskTriage` — the academic desk's at-risk triage log.
"""

from django.conf import settings
from django.db import models


class Nudge(models.Model):
    """One "you've gone quiet" reminder sent to a module's inactive students.

    Kept so the button can be rate-limited (once per module per
    :data:`apps.staffdesk.dashboards.NUDGE_COOLDOWN`) and so the dashboard can
    say when the last one went out and to how many people.
    """

    module = models.ForeignKey('learning.ProgrammeModule', on_delete=models.CASCADE,
                               related_name='staffdesk_nudges')
    sent_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                null=True, blank=True, related_name='+')
    sent_at = models.DateTimeField(auto_now_add=True, db_index=True)
    recipients = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ['-sent_at']
        indexes = [models.Index(fields=['module', '-sent_at'], name='staffdesk_nudge_mod_sent')]

    def __str__(self):
        return f'Nudge · {self.module_id} · {self.sent_at:%Y-%m-%d %H:%M}'


class RiskTriage(models.Model):
    """One step in handling an analytics :class:`~apps.analytics.models.RiskFlag`.

    RiskFlag itself only knows ``resolved``; acknowledging, notes and who was
    told what live here as an append-only log, so the at-risk page can show
    the history of a case and not just its latest state.
    """

    ACTION_ACK = 'ack'
    ACTION_RESOLVE = 'resolve'
    ACTION_REOPEN = 'reopen'
    ACTION_NOTE = 'note'
    ACTION_NOTIFY_STUDENT = 'notify_student'
    ACTION_NOTIFY_EDUCATOR = 'notify_educator'
    ACTION_CHOICES = [
        (ACTION_ACK, 'Acknowledged'), (ACTION_RESOLVE, 'Resolved'), (ACTION_REOPEN, 'Reopened'),
        (ACTION_NOTE, 'Note'), (ACTION_NOTIFY_STUDENT, 'Notified student'),
        (ACTION_NOTIFY_EDUCATOR, 'Notified educator'),
    ]

    flag = models.ForeignKey('analytics.RiskFlag', on_delete=models.CASCADE, related_name='triage')
    action = models.CharField(max_length=16, choices=ACTION_CHOICES)
    note = models.TextField(blank=True)
    by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                           null=True, blank=True, related_name='+')
    at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-at']

    def __str__(self):
        return f'{self.get_action_display()} · flag {self.flag_id}'
