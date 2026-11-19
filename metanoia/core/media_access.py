"""Access-controlled serving of uploaded files (``MEDIA_URL``).

Every upload lives on local disk under ``MEDIA_ROOT`` and is served by Django
itself (gunicorn, no front-end web server), whatever ``DEBUG`` says. Serving
them through ``django.views.static.serve`` with no checks made every proof of
payment, expense receipt, paid download, exam answer and chat file readable by
anyone holding (or guessing) the URL, without logging in. This view puts one
gate in front of all of them.

Four tiers, decided by the file's path under ``MEDIA_ROOT``:

**Public** (:data:`PUBLIC_PREFIXES`) — no login. Only what a logged-out page
    actually displays:

    * ``institutions/`` — institution logos, shown on registration step 2
      (``accounts/register/step2.html``), which a visitor fills in before they
      have an account.

    Nothing else under ``/media/`` appears on the landing page, sign-in,
    sign-up, pay-link, estimate, invite or certificate-verification pages
    (they use ``/static/`` brand images), and the e-mail templates carry no
    media URLs — so profile pictures and shop images stay behind the login.

**Private** (:data:`PRIVATE_RULES`) — admin/staff (``core.roles``) plus, where
    cheap, the people the owning row names. Everyone else — including anonymous
    visitors — gets **404**, so the response does not even confirm the file
    exists. The product reaches most of these through dedicated views that do
    their own checks (``finance:proof-file``, ``finance:expense-receipt``,
    ``shop:download-product``, ``reports:certificate-pdf``); the raw URL is for
    staff tooling (the admin) and for the screens that do link it directly:

    * ``payment_proofs/``, ``expenses/``, ``shop/downloads/`` — staff only.
    * ``whatsapp/`` — staff only: photos, voice notes and files people sent
      to the WhatsApp number, opened from the staff inbox.
    * ``certificates/`` — staff, or the certificate's student.
    * ``answers/`` — staff and educators (they mark them in
      ``assessments/mark_attempt.html``), the attempt's student (their result
      page links the upload), and that student's linked parents.
    * ``assignments/`` — same as answers, keyed on the submission's student.
    * ``chat/`` — staff, or a member of the chat the message was posted in
      (the chat UI renders attachments from their raw media URL).
    * ``ai_uploads/`` — staff only: files left from the retired AI assistant
      (its upload table was dropped, so nothing names an owner any more).
    * ``tasks/`` — staff only: attachments left from the removed tasks app
      (its tables were dropped, so nothing says who the task was for).
    * ``modules/staff-only/`` — staff and educators: tutor-pack files (facilitator
      packs, mock papers and marking guides) placed by a programme pack
      (``core.programme_pack``); the row is staff-only, and so is the file.
    * ``programme_packs/`` — staff only: uploaded pack zips being imported.

**Course content** (:data:`COURSE_PREFIXES`) — what sits behind enrolment or
    payment: module materials (study guides, question packs, memos,
    solutions, mock papers — ``modules/``), lesson block media and attachments
    (``lessons/``, except the cover and author images in
    :data:`GENERAL_PREFIXES`), question images (``questions/``) and uploaded
    content packs (``uploads/content/``). Admin/staff pass; anyone else gets
    the file only when a row that references it is open to them by the same
    rule its page uses (:func:`apps.learning.content_files.may_read` —
    enrolment, trial, week window, General module, single-item purchase, the
    module's educators, the solution gate). Signed-in but refused → **404**;
    signed out → the sign-in page, like the rest of the site.

**Everything else** — any signed-in user. An anonymous browser request is sent
    to the sign-in page (with ``?next=``), which is what the rest of the site
    does; an API client sending ``Authorization: Bearer <jwt>`` is accepted
    too, since the REST API hands out media URLs to the mobile/desktop apps.

Paths are normalised before they are classified, so ``institutions/../
payment_proofs/x.pdf`` is judged as the payment proof it really is, and any
path that escapes ``MEDIA_ROOT`` is a 404. The file itself is still served by
``django.views.static.serve`` (which ``safe_join``s against ``document_root``),
and :mod:`core.media_headers` still stamps the response.

Rule tables
-----------
``PUBLIC_PREFIXES``
    Shown on logged-out pages and served to anyone. Keep this list minimal.
``PRIVATE_RULES``
    ``(prefix, check)`` pairs. Admin/staff always pass; ``check(user, name)``
    decides for everyone else who is signed in, and anyone refused gets a 404.
``COURSE_PREFIXES``
    Course content, served only to someone a referencing row is open to.
    Checked after ``PRIVATE_RULES``, so ``modules/staff-only/`` stays
    teaching-only.
``GENERAL_PREFIXES``
    Under a course prefix but shown in listings and cards, not paid content:
    lesson cover images and author avatars stay in the signed-in tier.

Every ownership lookup fails closed: an exception while checking means the
file is refused.
"""

import posixpath

from django.conf import settings
from django.contrib.auth.views import redirect_to_login
from django.http import Http404
from django.utils.cache import patch_cache_control
from django.views.static import serve

from core.roles import role_of_user

PUBLIC_PREFIXES = (
    'institutions/',
)


def _is_admin_staff(user):
    """True for Django staff/superusers and the ``admin`` / ``staff`` roles."""
    return bool(user.is_staff or user.is_superuser) or role_of_user(user) in ('admin', 'staff')


def _can_teach(user):
    """True for admin/staff and educators."""
    return _is_admin_staff(user) or role_of_user(user) == 'educator'


def _is_parent_of(user, student_id):
    """True when ``user`` is a parent linked to the student ``student_id``."""
    if not student_id or role_of_user(user) != 'parent':
        return False
    try:
        from apps.accounts.models import ParentLink
    except Exception:  # pragma: no cover - app not installed
        return False
    return ParentLink.objects.filter(parent=user, student_id=student_id).exists()


def _staff_only(user, name):
    """Rule check that refuses everyone who is not already admin/staff."""
    return False


def _certificate(user, name):
    """True when ``user`` is the student on the certificate stored at ``name``."""
    from apps.reports.models import Certificate
    return Certificate.objects.filter(pdf=name, student=user).exists()


def _answer(user, name):
    """Teachers, the attempt's student and that student's linked parents may read an answer upload."""
    if _can_teach(user):
        return True
    from apps.assessments.models import Answer
    student_ids = set(Answer.objects.filter(file=name).values_list('attempt__student_id', flat=True))
    return user.pk in student_ids or any(_is_parent_of(user, sid) for sid in student_ids)


def _assignment(user, name):
    """Teachers, the submitting student and their linked parents may read an assignment upload."""
    if _can_teach(user):
        return True
    from apps.assessments.models import AssignmentSubmission
    student_ids = set(AssignmentSubmission.objects.filter(file=name).values_list('student_id', flat=True))
    return user.pk in student_ids or any(_is_parent_of(user, sid) for sid in student_ids)


def _chat(user, name):
    """True when ``user`` is a member of the chat the attachment was posted in."""
    from apps.communication.models import ChatMembership, MessageAttachment
    groups = MessageAttachment.objects.filter(file=name).values('message__group_id')
    return ChatMembership.objects.filter(user=user, group_id__in=groups).exists()


def _teaching_only(user, name):
    """Rule check that admits admin/staff and educators."""
    return _can_teach(user)


PRIVATE_RULES = (
    ('payment_proofs/', _staff_only),
    ('expenses/', _staff_only),
    ('shop/downloads/', _staff_only),
    ('whatsapp/', _staff_only),
    ('certificates/', _certificate),
    ('answers/', _answer),
    ('assignments/', _assignment),
    ('chat/', _chat),
    ('ai_uploads/', _staff_only),
    ('tasks/', _staff_only),
    ('modules/staff-only/', _teaching_only),
    ('programme_packs/', _staff_only),
)


COURSE_PREFIXES = (
    'modules/',
    'lessons/',
    'questions/',
    'uploads/content/',
)

GENERAL_PREFIXES = (
    'lessons/covers/',
    'lessons/authors/',
)


def classify(name):
    """``'public'``, ``'private'``, ``'course'`` or ``'general'`` for a
    normalised media path."""
    if name.startswith(PUBLIC_PREFIXES):
        return 'public'
    if any(name.startswith(prefix) for prefix, _ in PRIVATE_RULES):
        return 'private'
    if name.startswith(GENERAL_PREFIXES):
        return 'general'
    if name.startswith(COURSE_PREFIXES):
        return 'course'
    return 'general'


def normalise(path):
    """The media-relative path ``path`` really names, or ``None`` if it escapes
    ``MEDIA_ROOT`` / is not a plain relative path."""
    if not path or '\\' in path or '\x00' in path:
        return None
    name = posixpath.normpath(path).lstrip('/')
    if name in ('', '.') or name == '..' or name.startswith('../'):
        return None
    return name


def _authenticated_user(request):
    """The session user, or the user a valid ``Bearer`` JWT names, or None."""
    user = getattr(request, 'user', None)
    if user is not None and user.is_authenticated:
        return user
    if not request.META.get('HTTP_AUTHORIZATION', '').startswith('Bearer '):
        return None
    try:
        from rest_framework_simplejwt.authentication import JWTAuthentication
        result = JWTAuthentication().authenticate(request)
    except Exception:
        return None
    return result[0] if result else None


def _may_read_private(user, name):
    """Apply the matching :data:`PRIVATE_RULES` check; any lookup error fails closed."""
    if _is_admin_staff(user):
        return True
    for prefix, check in PRIVATE_RULES:
        if name.startswith(prefix):
            try:
                return bool(check(user, name))
            except Exception:
                return False
    return False


def _may_read_course(user, name):
    """Defer to :func:`apps.learning.content_files.may_read`; any lookup error fails closed."""
    if _is_admin_staff(user):
        return True
    try:
        from apps.learning.content_files import may_read
        return bool(may_read(user, name))
    except Exception:
        return False


def protected_serve(request, path):
    """Serve ``MEDIA_ROOT/<path>`` if the requester may see it (see module doc).

    Non-public responses are marked ``Cache-Control: private`` so a shared cache
    never hands a private file to the next person.
    """
    name = normalise(path)
    if name is None:
        raise Http404('Not found')

    tier = classify(name)
    if tier != 'public':
        user = _authenticated_user(request)
        if tier == 'private':
            if user is None or not _may_read_private(user, name):
                raise Http404('Not found')
        elif user is None:
            return redirect_to_login(request.get_full_path(), settings.LOGIN_URL)
        elif tier == 'course':
            if not _may_read_course(user, name):
                raise Http404('Not found')

    response = serve(request, name, document_root=settings.MEDIA_ROOT)
    if tier != 'public':
        patch_cache_control(response, private=True)
    return response
