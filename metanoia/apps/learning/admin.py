"""Django admin for the learning app (shown in the hub's layout).

The academic spine is authored top-down: an institution holds programmes, a
programme holds its offerings (each drawn from the canonical module catalogue),
and an offering holds its topic *links* into the shared topic library. Each
screen inlines the level below it, so the whole of Milpark PGDA can be built
without leaving the programme page.

Every offering owns its own topics, so a topic is always edited in the context of
one institution — its wording, ordering, depth, and that institution's own
past-paper frequency and mark weighting.
"""

from django.contrib import admin

from . import models


class ProgrammeInline(admin.TabularInline):
    """Programmes, inline on the institution page."""
    model = models.Programme
    extra = 0
    fields = ('code', 'name', 'full_name', 'level', 'depth_default', 'is_competency_based',
              'order', 'is_active')
    ordering = ('order', 'name')
    show_change_link = True


class ProgrammePhaseInline(admin.TabularInline):
    """Programme phases, inline on the programme page."""
    model = models.ProgrammePhase
    extra = 0
    fields = ('order', 'name', 'period', 'focus', 'is_active')
    ordering = ('order',)


class ProgrammeModuleInline(admin.TabularInline):
    """Module offerings, inline on the programme page."""
    model = models.ProgrammeModule
    extra = 1
    fields = ('code', 'module', 'name', 'depth_level', 'order', 'is_active')
    autocomplete_fields = ('module',)
    ordering = ('order',)
    show_change_link = True


class CohortInline(admin.TabularInline):
    """Cohorts, inline on the programme page."""
    model = models.Cohort
    extra = 0
    fields = ('code', 'name', 'start_date', 'end_date', 'is_active')


class TopicInline(admin.TabularInline):
    """The offering's own syllabus — edited here, in the context of one
    institution, without touching any other institution's copy."""

    model = models.Topic
    extra = 1
    fields = ('order', 'code', 'title', 'reference', 'depth', 'frequency', 'avg_marks',
              'is_non_negotiable', 'is_active')
    ordering = ('order',)
    show_change_link = True


@admin.register(models.Institution)
class InstitutionAdmin(admin.ModelAdmin):
    """Admin for institutions, with their programmes inline."""
    list_display = ('code', 'name', 'accent_name', 'programme_count', 'order', 'is_active')
    list_filter = ('is_active',)
    search_fields = ('code', 'name')
    prepopulated_fields = {'slug': ('name',)}
    inlines = [ProgrammeInline]

    @admin.display(description='Programmes')
    def programme_count(self, obj):
        """Column: the number of programmes."""
        return obj.programmes.count()


@admin.register(models.Programme)
class ProgrammeAdmin(admin.ModelAdmin):
    """Admin for programmes, with their phases, offerings and cohorts inline."""
    list_display = ('display_name', 'abbreviation', 'institution', 'full_code', 'level',
                    'depth_default', 'module_count', 'is_active')
    list_filter = ('institution', 'level', 'depth_default', 'is_active')
    search_fields = ('name', 'full_name', 'abbreviation', 'code',
                     'institution__name', 'institution__code')

    @admin.display(description='Programme', ordering='full_name')
    def display_name(self, obj):
        """Column: the programme's display name."""
        return obj.display_name
    autocomplete_fields = ('institution',)
    inlines = [ProgrammeModuleInline, ProgrammePhaseInline, CohortInline]

    @admin.display(description='Modules')
    def module_count(self, obj):
        """Column: the number of module offerings."""
        return obj.modules.count()


@admin.register(models.Module)
class ModuleAdmin(admin.ModelAdmin):
    """The canonical module catalogue — FR, TX, GA, MA and the APC competency areas."""

    list_display = ('code', 'name', 'is_competency_area', 'offering_count', 'order', 'is_active')
    list_filter = ('is_competency_area', 'is_active')
    search_fields = ('name', 'code')
    prepopulated_fields = {'slug': ('name',)}

    @admin.display(description='Offered on')
    def offering_count(self, obj):
        """Column: the number of programmes offering the module."""
        return obj.offerings.count()


@admin.register(models.Topic)
class TopicAdmin(admin.ModelAdmin):
    """One offering's topic. The same code exists on every other institution
    that teaches it, as its own independently editable row."""

    list_display = ('code', 'title', 'programme_module', 'reference', 'resolved_depth',
                    'frequency_label', 'avg_marks', 'is_non_negotiable', 'item_count',
                    'order', 'is_active')
    list_filter = ('programme_module__programme__institution', 'programme_module__programme',
                   'programme_module__module', 'depth', 'frequency', 'is_non_negotiable', 'is_active')
    search_fields = ('code', 'title', 'reference', 'description', 'programme_module__code')
    autocomplete_fields = ('programme_module',)
    prepopulated_fields = {'slug': ('title',)}

    @admin.display(description='Depth')
    def resolved_depth(self, obj):
        """Column: the topic's effective depth."""
        return obj.resolved_depth

    @admin.display(description='Frequency')
    def frequency_label(self, obj):
        """Column: the topic's past-paper frequency label."""
        return obj.frequency_label

    @admin.display(description='Items')
    def item_count(self, obj):
        """Column: a topic's teaching material, its lessons plus its assessments.

        Topic has no ``items`` relation; that accessor never existed.
        """
        return obj.lessons.count() + obj.assessments.count()


@admin.register(models.ProgrammeModule)
class ProgrammeModuleAdmin(admin.ModelAdmin):
    """Admin for module offerings, with their topics inline."""
    list_display = ('reference', 'display_name', 'resolved_depth', 'topic_count', 'order', 'is_active')
    list_filter = ('programme__institution', 'programme', 'depth_level', 'is_active')
    search_fields = ('code', 'name', 'module__name', 'module__code', 'programme__code',
                     'programme__institution__code')
    autocomplete_fields = ('programme', 'module')
    inlines = [TopicInline]

    @admin.display(description='Topics')
    def topic_count(self, obj):
        """Column: the number of topics."""
        return obj.topics.count()


@admin.register(models.ProgrammePhase)
class ProgrammePhaseAdmin(admin.ModelAdmin):
    """Admin for programme phases."""
    list_display = ('name', 'programme', 'period', 'order', 'is_active')
    list_filter = ('programme__institution', 'programme', 'is_active')
    search_fields = ('name', 'period', 'focus')
    autocomplete_fields = ('programme',)


@admin.register(models.Cohort)
class CohortAdmin(admin.ModelAdmin):
    """Admin for cohorts."""
    list_display = ('code', 'programme', 'name', 'start_date', 'end_date', 'is_active')
    list_filter = ('programme__institution', 'programme', 'is_active')
    search_fields = ('code', 'name', 'programme__code')
    autocomplete_fields = ('programme',)


class CalendarEventInline(admin.TabularInline):
    """Add the year's dates straight onto the calendar they belong to."""
    model = models.CalendarEvent
    extra = 1
    fields = ('start', 'end', 'all_day', 'kind', 'title', 'programme',
              'programme_module', 'cohort', 'weight_pct', 'is_published')
    autocomplete_fields = ('programme', 'programme_module', 'cohort')
    ordering = ('start',)


@admin.register(models.AcademicCalendar)
class AcademicCalendarAdmin(admin.ModelAdmin):
    """Admin for academic calendars, with their events inline."""
    list_display = ('__str__', 'institution', 'year', 'event_count', 'next_event', 'is_active')
    list_filter = ('institution', 'year', 'is_active')
    search_fields = ('name', 'institution__name', 'institution__code')
    autocomplete_fields = ('institution',)
    inlines = [CalendarEventInline]

    @admin.display(description='Events')
    def event_count(self, obj):
        """Column: the number of events."""
        return obj.events.count()

    @admin.display(description='Next up')
    def next_event(self, obj):
        """Column: the next upcoming event and its date."""
        event = obj.upcoming.first()
        return f'{event.title} · {event.start:%d %b}' if event else '—'


@admin.register(models.CalendarEvent)
class CalendarEventAdmin(admin.ModelAdmin):
    """Admin for individual calendar events."""
    list_display = ('title', 'kind', 'start', 'calendar', 'programme', 'programme_module',
                    'cohort', 'is_published')
    list_filter = ('kind', 'is_published', 'calendar__institution', 'calendar__year', 'programme')
    search_fields = ('title', 'description', 'programme_module__code', 'programme__code',
                     'calendar__institution__code')
    autocomplete_fields = ('calendar', 'programme', 'programme_module', 'cohort')
    date_hierarchy = 'start'


class LessonResourceInline(admin.TabularInline):
    """Lesson resources, inline on the lesson page."""
    model = models.LessonResource
    extra = 1


class LessonVersionInline(admin.TabularInline):
    """Lesson versions, inline on the lesson page."""
    model = models.LessonVersion
    extra = 0
    readonly_fields = ('version', 'created_by', 'created_at')
    can_delete = False


@admin.register(models.Lesson)
class LessonAdmin(admin.ModelAdmin):
    """Admin for lessons, with their resources and versions inline."""
    list_display = ('title', 'topic', 'module', 'status', 'visibility', 'version',
                    'estimated_minutes', 'created_by', 'created_at')
    list_filter = ('status', 'visibility', 'topic__programme_module__programme', 'module',
                   'created_at')
    search_fields = ('title', 'body')
    date_hierarchy = 'created_at'
    prepopulated_fields = {'slug': ('title',)}
    autocomplete_fields = ('topic', 'module', 'created_by')
    filter_horizontal = ('target_programmes', 'target_modules', 'target_users')
    inlines = [LessonResourceInline, LessonVersionInline]


@admin.register(models.LessonResource)
class LessonResourceAdmin(admin.ModelAdmin):
    """Admin for lesson resources."""
    list_display = ('title', 'lesson', 'kind', 'order')
    list_filter = ('kind',)
    search_fields = ('title', 'lesson__title')


@admin.register(models.StudySession)
class StudySessionAdmin(admin.ModelAdmin):
    """Admin for study sessions."""
    list_display = ('student', 'lesson', 'status', 'minutes', 'completion_pct',
                    'start_time', 'end_time')
    list_filter = ('status', 'created_at')
    search_fields = ('student__username', 'student__email', 'lesson__title')
    date_hierarchy = 'created_at'
    autocomplete_fields = ('student', 'lesson')
    readonly_fields = ('public_id', 'total_seconds')


class ModuleWeekInline(admin.TabularInline):
    """Module weeks, inline on the module phase page."""
    model = models.ModuleWeek
    extra = 0
    fields = ('number', 'title', 'starts_on', 'ends_on', 'is_published')
    ordering = ('number',)


class WeekTopicInline(admin.TabularInline):
    """The ordered series of topics a week works through."""
    model = models.WeekTopic
    extra = 1
    fields = ('order', 'topic')
    autocomplete_fields = ('topic',)
    ordering = ('order',)


class ModuleMaterialInline(admin.TabularInline):
    """Module materials, inline on the phase or week page."""
    model = models.ModuleMaterial
    extra = 0
    fk_name = 'phase'
    fields = ('order', 'kind', 'title', 'week', 'topic', 'file', 'product', 'is_published', 'is_preview')
    autocomplete_fields = ('week', 'topic')
    ordering = ('week', 'order')


@admin.register(models.ModulePhase)
class ModulePhaseAdmin(admin.ModelAdmin):
    """Admin for module phases, with their weeks and materials inline."""
    list_display = ('display_title', 'programme_module', 'kind', 'sequence',
                    'assessment_date', 'week_count', 'material_count', 'is_published')
    list_filter = ('kind', 'is_published', 'is_active',
                   'programme_module__programme__institution', 'programme_module__programme')
    search_fields = ('title', 'programme_module__code', 'programme_module__module__name')
    autocomplete_fields = ('programme_module', 'calendar_event')
    inlines = [ModuleWeekInline, ModuleMaterialInline]

    @admin.display(description='Weeks')
    def week_count(self, obj):
        """Column: the number of weeks."""
        return obj.weeks.count()

    @admin.display(description='Material')
    def material_count(self, obj):
        """Column: the number of materials."""
        return obj.materials.count()


@admin.register(models.ModuleWeek)
class ModuleWeekAdmin(admin.ModelAdmin):
    """Admin for module weeks."""
    list_display = ('display_title', 'phase', 'number', 'topic_codes', 'starts_on', 'ends_on', 'is_published')
    list_filter = ('is_published', 'is_active', 'phase__kind',
                   'phase__programme_module__programme__institution')
    search_fields = ('title', 'topics__title', 'topics__code', 'phase__title')
    autocomplete_fields = ('phase',)
    inlines = [WeekTopicInline]

    @admin.display(description='Topics')
    def topic_codes(self, obj):
        """Column: the codes of the week's topics."""
        return ', '.join(obj.topics.values_list('code', flat=True)) or '—'


@admin.register(models.ModuleMaterial)
class ModuleMaterialAdmin(admin.ModelAdmin):
    """Admin for module materials."""
    list_display = ('title', 'kind', 'phase', 'week', 'topic', 'is_published', 'is_preview', 'product')
    list_filter = ('kind', 'is_published', 'is_preview',
                   'phase__programme_module__programme__institution')
    search_fields = ('title', 'description', 'phase__title', 'week__title', 'topic__title', 'topic__code')
    autocomplete_fields = ('phase', 'week', 'topic', 'lesson', 'assessment', 'meeting', 'product')
    readonly_fields = ('created_by',)
