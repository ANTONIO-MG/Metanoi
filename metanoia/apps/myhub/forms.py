"""Forms for the calendar.

Once this held a ModelForm per MyHub CRUD page — professors, students, courses,
library, fees, holidays, CMS, blog. Those models and their pages are gone; what
is left is the event form the calendar posts to.
"""

from django import forms

from core.forms import DateTimeInput, StyledModelForm

from . import models


class EventForm(StyledModelForm):
    class Meta:
        model = models.Event
        fields = ['title', 'start', 'end', 'all_day', 'location', 'color', 'description']
        widgets = {
            'start': DateTimeInput(), 'end': DateTimeInput(),
            'description': forms.Textarea(attrs={'rows': 3}),
        }
