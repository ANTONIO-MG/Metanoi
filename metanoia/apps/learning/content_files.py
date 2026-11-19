"""Who may download a course-content file by its storage path.

Course files sit under ``MEDIA_ROOT`` with guessable names, so serving them
by URL must apply the same rule as the page that links them. :func:`may_read`
finds every row referencing the path and allows the file when **any** of them
is open to the viewer:

* :class:`~apps.learning.models.ModuleMaterial` — :func:`apps.learning.
  material_open.material_state` (enrolment / trial / week window / General
  module / single-item purchase / staff & the module's educators, then the
  teaching order, so a solution stays shut until the attempt is in).
* :class:`~apps.learning.models.LessonBlock` / :class:`~apps.learning.models.
  LessonResource` — :func:`can_open_lesson`, the checks ``lesson_view`` makes.
* :class:`~apps.assessments.models.Question` media — :func:`can_see_question`.
* :class:`~apps.learning.upload_models.ContentUploadFile` — the uploader and the
  module's educators (and any material it was published as, above).

A path no row references is refused (admin/staff are let through earlier, in
:mod:`core.media_access`). Every refusal is a 404, never a 403, so the response
does not confirm the file exists.
"""

import logging

logger = logging.getLogger('apps')


def can_open_lesson(user, lesson):
    """The access checks :func:`apps.learning.views.lesson_view` applies, as a bool:
    audience/targeting, the module paywall and the schedule's week rule.

    ``_locked_module_for_lesson`` only knows a lesson's module through its topic,
    so a lesson filed straight on a paid module is checked separately: it must not
    hand out its files to a candidate whose module is locked.
    """
    from .access import schedule_refusal
    from .views import _can_view_lesson, _is_staff_like, _locked_module_for_lesson

    if not _can_view_lesson(user, lesson):
        return False
    if _locked_module_for_lesson(user, lesson) is not None:
        return False
    if _is_staff_like(user):
        return True
    if schedule_refusal(user, lesson=lesson) is not None:
        return False
    offering = lesson.module
    if offering is not None and not offering.is_unlocked_for(getattr(user, 'profile', None)):
        return False
    return True


def _teaches(user, module_id):
    """True when ``user`` is an educator who teaches the module ``module_id``."""
    from core.roles import role_of_user
    from core.scoping import taught_module_qs

    if not module_id or role_of_user(user) != 'educator':
        return False
    return taught_module_qs(user).filter(pk=module_id).exists()


def can_see_question(user, question):
    """A question's image/recording: its paper's educators and author, a
    candidate who has an attempt at the paper (the take page creates it before
    the images load; the result page shows them again), or a revision card on
    the question while its module is open to them (the revision rule)."""
    from apps.assessments.models import AssessmentAttempt
    from apps.revision.models import ReviewCard
    from apps.revision.services import module_of

    assessment = question.section.assessment
    module = module_of(question)
    if assessment.created_by_id == user.pk or _teaches(user, getattr(module, 'pk', None)):
        return True
    if AssessmentAttempt.objects.filter(assessment=assessment, student=user).exists():
        return True
    if ReviewCard.objects.filter(user=user, question=question).exists():
        person = getattr(user, 'profile', None)
        return module is None or bool(person and module.is_unlocked_for(person))
    return False


def _material_rule(user, name):
    """True when ``name`` is a module material file the user may open."""
    from .material_open import material_state
    from .models import ModuleMaterial

    materials = (ModuleMaterial.objects.filter(file=name)
                 .select_related('phase__programme_module', 'week', 'product'))
    return any(material_state(user, m).open for m in materials)


def _lesson_rule(user, name):
    """True when ``name`` is a lesson block or resource file in a lesson the user may open."""
    from .models import Lesson, LessonBlock, LessonResource

    lesson_ids = set(LessonBlock.objects.filter(media=name).values_list('lesson_id', flat=True))
    lesson_ids |= set(LessonResource.objects.filter(file=name).values_list('lesson_id', flat=True))
    lessons = Lesson.objects.filter(pk__in=lesson_ids).select_related('module', 'topic__programme_module')
    return any(can_open_lesson(user, lesson) for lesson in lessons)


def _question_rule(user, name):
    """True when ``name`` is question media the user may see."""
    from apps.assessments.models import Question

    questions = Question.objects.filter(media=name).select_related(
        'section__assessment__module', 'section__assessment__topic__programme_module')
    return any(can_see_question(user, q) for q in questions)


def _upload_rule(user, name):
    """True when ``name`` was uploaded by the user, or for a module they teach."""
    from .upload_models import ContentUploadFile

    for row in ContentUploadFile.objects.filter(file=name).select_related('upload'):
        upload = row.upload
        if upload.created_by_id == user.pk or _teaches(user, upload.programme_module_id):
            return True
    return False


_RULES = (_material_rule, _lesson_rule, _question_rule, _upload_rule)


def may_read(user, name):
    """True when some row referencing media path ``name`` is open to ``user``.

    Called by :mod:`core.media_access` for the course-content prefixes, after it
    has already let admin/staff through. Fails closed.
    """
    for rule in _RULES:
        try:
            if rule(user, name):
                return True
        except Exception:
            logger.exception('content file access check failed for %s', name)
            return False
    return False
