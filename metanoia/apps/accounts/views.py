"""Accounts pages: registration wizard, invites, profile, settings, POPIA
exports, account closure and admin/staff member management.

Django's settings are imported as ``dj_settings`` because this module has a view named
:func:`settings`.

Registration is a 3-step wizard (:func:`register` → :func:`register_course` →
:func:`register_review`). ``/accounts/`` is exempt from ``OnboardingMiddleware`` so the
user can move back and forth between the steps.

Module constants:

- ``ONE_ON_ONE_HOURLY_RATE`` — what a one-on-one hour costs when the catalogue item has
  no price yet.
- ``REGISTRATION_OUTCOMES`` — the hand-off screen copy per registration outcome
  (``paid`` = PayFast cleared, ``eft`` = free week with invoice out, ``oneonone``).
- ``REGISTRATION_COMPLETE_SECONDS`` — how long the hand-off screen holds before it
  forwards to the dashboard.
"""

import logging

from django.conf import settings as dj_settings
from django.contrib import messages
from django.contrib.auth import get_user_model, logout
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone, translation
from django.views.decorators.http import require_POST

from core.branding import t
from core.errors import capture, note, report
from core.redirects import redirect_back
from core.roles import role_flags

from . import emails, forms, models, registration_billing, services


def _person_for(user):
    """The user's :class:`Person` profile, or None."""
    return getattr(user, 'profile', None)


def _require_admin_staff(request):
    """Return True if the current user may manage other people's enrolment."""
    return role_flags(request).get('is_admin_staff', False)


def enrolled_programmes(person):
    """The programmes a person is registered for, most recent first."""
    if person is None:
        return []
    from apps.learning.models import ProgrammeEnrolment
    return [
        row.programme for row in (
            ProgrammeEnrolment.objects
            .filter(person=person)
            .select_related('programme__institution', 'cohort')
            .order_by('-created_at'))
    ]


@login_required
def my_programmes(request):
    """"My Programme(s)": 0 → registration, 1 → their modules, 2+ → a picker."""
    person = _person_for(request.user)
    programmes = enrolled_programmes(person)

    if not programmes:
        note('USER-4001', request, reason='no-enrolment')
        messages.info(request, "You're not registered for a programme yet.")
        return redirect('accounts:register-course')
    if len(programmes) == 1:
        return redirect('learning:my-modules')
    return render(request, 'accounts/my-programmes.html', {'programmes': programmes})


def _client_ip(request):
    """The client's IP address: the first ``X-Forwarded-For`` hop, else ``REMOTE_ADDR``.
    """
    xff = request.META.get('HTTP_X_FORWARDED_FOR')
    if xff:
        return xff.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR')


def _related_or_none(person, attr):
    """A Person's reverse-OneToOne side-table, or None if not created yet."""
    try:
        return getattr(person, attr)
    except Exception:
        return None


def _step1_forms(person, data=None, files=None):
    """The grouped Step-1 sections — each a dict with a form bound to the Person
    or its side-table, plus accordion metadata (title/icon/open).

    Each form is given the person, because the contact form checks the number was
    verified.
    """
    built = []
    for key, form_cls, attr, title, icon, is_open in forms.WIZARD_STEP1_FORMS:
        instance = person if attr is None else _related_or_none(person, attr)
        form = form_cls(data, files, instance=instance, prefix=key)
        form.person = person
        built.append({
            'key': key, 'form': form,
            'title': title, 'icon': icon, 'open': is_open,
        })
    return built


def _keep_uploaded_picture(person, sections):
    """Save a newly chosen, valid profile picture even when another field failed,
    since browsers never re-send a file after an error page.

    Nothing changes when no new picture was uploaded.
    """
    basics = next((s['form'] for s in sections if s['key'] == 'basics'), None)
    if basics is None or 'profile_picture' in basics.errors:
        return
    upload = basics.cleaned_data.get('profile_picture') if hasattr(basics, 'cleaned_data') else None
    if not upload or not hasattr(upload, 'content_type'):
        return
    person.profile_picture = upload
    person.save(update_fields=['profile_picture'])


def _get_or_create_consent(person):
    """The person's :class:`PersonConsent` row, created on first use."""
    consent, _ = models.PersonConsent.objects.get_or_create(person=person)
    return consent


def _serialize_person(person):
    """A JSON-able snapshot of everything we hold about a person (POPIA right of
    access). File fields are referenced by name only, not dumped."""
    from django.forms.models import model_to_dict
    exclude = ['id', 'person', 'user', 'profile_picture']

    def dump(obj):
        """``model_to_dict`` of ``obj`` without the excluded fields, or None."""
        return model_to_dict(obj, exclude=exclude) if obj is not None else None

    return {
        'account': {
            'email': person.user.email,
            'username': person.user.get_username(),
            'date_joined': person.user.date_joined,
        },
        'profile': dump(person),
        'contact': dump(_related_or_none(person, 'contact')),
        'study_profile': dump(_related_or_none(person, 'study_profile')),
        'consent': dump(_related_or_none(person, 'consent')),
    }


@login_required
def register(request):
    """Step 1 — personal detail, saved to Person + side-tables so the wizard can
    be resumed; marks ``profile_status``.

    The 3-step wizard is for students only; parents are sent elsewhere. When the form
    promises "we'll invite them", the guardian is invited straight away — best-effort, a
    failed invite never blocks registration.
    """
    person = _person_for(request.user) or models.Person.objects.create(user=request.user)
    if person.onboarding_complete:
        return redirect('myhub:index')
    if person.user_type == 'parent':
        return redirect('accounts:register-parent')
    if person.user_type == 'educator' and person.invite_id:
        return redirect('accounts:register-educator')
    if person.user_type in ('admin', 'staff', 'educator'):
        return redirect('accounts:register-staff')
    if person.pending_invoice_uid and not person.enrolment_settled:
        return redirect('accounts:enrol-checkout')

    sections = _step1_forms(person, request.POST or None, request.FILES or None)
    if request.method == 'POST':
        valid = all(s['form'].is_valid() for s in sections)
        basics = next((s['form'] for s in sections if s['key'] == 'basics'), None)
        gemail = ''
        if basics is not None and hasattr(basics, 'cleaned_data'):
            gemail = (basics.cleaned_data.get('guardian_email') or '').strip().lower()
            if gemail and gemail == (request.user.email or '').strip().lower():
                basics.add_error('guardian_email', "That is your own e-mail address — enter your "
                                                   "parent's, guardian's or sponsor's instead.")
                valid = False
        if valid:
            for s in sections:
                key, f = s['key'], s['form']
                obj = f.save(commit=False)
                if key == 'basics':
                    obj.profile_status = True
                    obj.guardian_emails = [gemail] if gemail else []
                    obj.save()
                    if gemail:
                        from .parent_views import invite_guardian
                        try:
                            invite_guardian(obj, gemail)
                        except Exception:  # pragma: no cover - never block registration
                            logging.getLogger('accounts').exception('accounts: guardian invite failed')
                else:
                    obj.person = person
                    if key == 'consent':
                        obj.consent_date = timezone.now()
                        obj.consent_version = models.CONSENT_VERSION
                        obj.consent_ip = _client_ip(request)
                        obj.consent_device = request.META.get('HTTP_USER_AGENT', '')[:300]
                    obj.save()
            return redirect('accounts:register-course')
        _keep_uploaded_picture(person, sections)
        messages.error(request, 'Please complete the required fields highlighted below.')

    return render(request, 'accounts/register/step1.html', {
        'page_title': 'Registration', 'person': person, 'sections': sections, 'step': 1,
        **_wa_context(person),
    })


def _posted_number(request):
    """The number from the code box, normalised the way the form will store it."""
    from django.core.exceptions import ValidationError
    try:
        return forms.normalise_phone(request.POST.get('number', ''), request.POST.get('country') or None)
    except ValidationError:
        return ''


@login_required
@require_POST
def phone_send_code(request):
    """Send a WhatsApp verification code to the number typed in the form."""
    from . import phone_verify
    ok, message = phone_verify.send_code(request.user, _posted_number(request))
    return JsonResponse({'ok': ok, 'message': message}, status=200 if ok else 400)


@login_required
@require_POST
def phone_check_code(request):
    """Check the code; on success the number is stored as the verified one."""
    from . import phone_verify
    ok, message = phone_verify.check_code(request.user, _posted_number(request),
                                          request.POST.get('code', ''))
    return JsonResponse({'ok': ok, 'message': message}, status=200 if ok else 400)


def _wa_context(person):
    """What the WhatsApp code box beside a phone field needs."""
    from . import phone_verify
    from core import whatsapp
    return {'wa_verify': phone_verify.required(),
            'wa_verified': getattr(person, 'whatsapp_number', ''),
            'wa_number': whatsapp.display(getattr(dj_settings, 'WHATSAPP_DISPLAY_NUMBER', ''))
            or 'our WhatsApp number'}


@login_required
def register_staff(request):
    """First-login profile for a team account: step-1 details + POPIA, no
    enrolment. Completing it lifts the onboarding gate and sends the welcome pack.

    Team members have no enrolment step, so they are marked registered with no
    guardians. The welcome pack is best-effort.
    """
    person = _person_for(request.user) or models.Person.objects.create(user=request.user)
    if person.onboarding_complete:
        return redirect('myhub:index')
    if person.user_type not in ('admin', 'staff', 'educator'):
        return redirect('accounts:register')

    sections = [s for s in _step1_forms(person, request.POST or None, request.FILES or None)
                if s['key'] in ('basics', 'contact', 'consent')]
    if request.method == 'POST':
        if all(s['form'].is_valid() for s in sections):
            for s in sections:
                key, f = s['key'], s['form']
                obj = f.save(commit=False)
                if key == 'basics':
                    obj.profile_status = True
                    obj.registered = True
                    obj.guardian_emails = []
                    obj.save()
                else:
                    obj.person = person
                    if key == 'consent':
                        obj.consent_date = timezone.now()
                        obj.consent_version = models.CONSENT_VERSION
                        obj.consent_ip = _client_ip(request)
                        obj.consent_device = request.META.get('HTTP_USER_AGENT', '')[:300]
                    obj.save()
            try:
                from apps.communication.welcome import send_welcome_pack
                send_welcome_pack(request.user)
            except Exception:  # pragma: no cover - best-effort
                logging.getLogger('accounts').exception('accounts: welcome pack failed')
            messages.success(request, 'Welcome aboard — your profile is set up.')
            return redirect('myhub:index')
        _keep_uploaded_picture(person, sections)
        messages.error(request, 'Please complete the required fields highlighted below.')

    return render(request, 'accounts/register/step1.html', {
        'page_title': 'Complete your profile', 'person': person,
        'sections': sections, 'step': 1, 'staff': True, **_wa_context(person),
    })


@login_required
def staff_create(request):
    """Superuser only: create a verified team account with a random password and
    e-mail them the login details.

    The post-save signal creates the Person; the view fills in role and name, and leaves
    the person unregistered so they complete the lean profile on first login.
    """
    if not request.user.is_superuser:
        messages.error(request, 'Only a superuser can create team accounts.')
        return redirect('myhub:index')

    form = forms.StaffAccountForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        from allauth.account.models import EmailAddress
        from django.contrib.auth import get_user_model
        from django.utils.crypto import get_random_string

        from . import emails
        cd = form.cleaned_data
        password = get_random_string(10)
        user = get_user_model().objects.create_user(
            username=cd['email'], email=cd['email'], password=password)
        EmailAddress.objects.update_or_create(
            user=user, email=cd['email'], defaults={'verified': True, 'primary': True})
        person = _person_for(user) or models.Person.objects.create(user=user)
        person.user_type = cd['user_type']
        person.first_name = cd['first_name']
        person.last_name = cd['last_name']
        person.registered = False
        person.profile_status = False
        person.save()
        emails.send_account_invitation(user, password, request)
        role_label = dict(forms.STAFF_ROLE_CHOICES).get(cd['user_type'], cd['user_type'])
        messages.success(
            request,
            f"{cd['first_name']} {cd['last_name']} created as {role_label}. "
            "An invitation with login details has been e-mailed.")
        return redirect('accounts:staff-create')

    recent = (models.Person.objects.filter(user_type__in=['admin', 'staff', 'educator'])
              .select_related('user').order_by('-user__date_joined')[:15])
    return render(request, 'accounts/staff_create.html', {
        'page_title': 'Create team account', 'form': form, 'recent': recent,
    })


@login_required
def register_course(request):
    """Step 2 — pick institution, programme and modules (priced per module per
    month). The selection is kept in the session.

    One-on-one (``track=one_on_one``): any modules the student wants, each with its
    hours; there is no "all modules are compulsory" rule (that is for group classes). An
    "Other" institution is stored on the person straight away, so staff can see where
    such a student really studies even before they finish. The stored ``cohort_id`` is a
    real cohort id, or the string ``'year'`` when the programme has no named intakes and
    the current academic year stands in as its cohort; ``hours`` are the hours to buy
    now per module (0 = register now, buy hours later).
    """
    person = _person_for(request.user)
    if person is None or not person.profile_status:
        return redirect('accounts:register')
    if person.onboarding_complete:
        return redirect('myhub:index')

    reg = request.session.get('reg', {})

    if request.method == 'POST':
        if request.POST.get('nav') == 'back':
            return redirect('accounts:register')
        institution_id = (request.POST.get('institution_id') or '').strip()
        programme_id = (request.POST.get('programme_id') or '').strip()
        module_ids = [m for m in request.POST.getlist('module_ids') if m.isdigit()]
        one_on_one = request.POST.get('track') == 'one_on_one'
        if not one_on_one:
            module_ids = _mandatory_module_ids(programme_id) or module_ids
        other = _other_institution_from(request.POST, institution_id)
        if other['errors']:
            note('USER-1002', request, reason='other_institution_blank')
            messages.error(request, 'You chose "Other institution" — please type your institution '
                                    'and your cohort or year.')
            return _render_choose(request, person, reg, other=other, status=400)
        if not programme_id.isdigit():
            note('USER-1002', request, reason='no_programme')
            messages.error(request, 'Please choose the programme you are studying.')
        elif not module_ids:
            note('USER-1002', request, reason='no_modules', programme=programme_id)
            messages.error(request, 'Please tick at least one module to register for.')
        else:
            cohort_id = (request.POST.get('cohort_id') or '').strip()
            if other['is_other']:
                person.other_institution = other['institution']
                person.other_cohort = other['cohort']
                person.save(update_fields=['other_institution', 'other_cohort'])
                cohort_id = cohort_id or 'year'
            request.session['reg'] = {
                'institution_id': int(institution_id) if institution_id.isdigit() else None,
                'programme_id': int(programme_id),
                'module_ids': [int(m) for m in module_ids],
                'cohort_id': int(cohort_id) if cohort_id.isdigit() else (cohort_id or None),
                'other_institution': other['institution'] if other['is_other'] else '',
                'other_cohort': other['cohort'] if other['is_other'] else '',
                'track': 'one_on_one' if one_on_one else 'class',
                'hours': ({m: _posted_hours(request.POST, m) for m in module_ids}
                          if one_on_one else {}),
            }
            request.session.modified = True
            return redirect('accounts:register-review')

    return _render_choose(request, person, reg)


def _posted_hours(post, module_id):
    """Hours posted for one module on the one-on-one path: 0–20, default 1."""
    from apps.shop.one_on_one import MAX_HOURS_PER_MODULE
    raw = (post.get(f'hours_{module_id}') or '1').strip()
    return max(0, min(int(raw), MAX_HOURS_PER_MODULE)) if raw.isdigit() else 1


def _mandatory_module_ids(programme_id):
    """Every active module of a programme whose modules are all compulsory (IAC),
    as strings — or ``[]`` when the student chooses their own modules."""
    from apps.learning.models import Programme
    if not str(programme_id or '').isdigit():
        return []
    programme = Programme.objects.filter(pk=int(programme_id), modules_mandatory=True).first()
    if programme is None:
        return []
    return [str(pk) for pk in programme.modules.without_general()
            .filter(is_active=True).values_list('pk', flat=True)]


def _other_institution_from(post, institution_id):
    """Validate step 2's "Other institution" boxes.

    Returns ``{'is_other', 'institution', 'cohort', 'errors'}``; both boxes are
    required only when the "Other institution" row was picked.
    """
    from apps.learning.models import Institution

    from .forms import OTHER_REQUIRED
    out = {'is_other': False, 'institution': (post.get('other_institution') or '').strip()[:160],
           'cohort': (post.get('other_cohort') or '').strip()[:60], 'errors': {}}
    inst = (Institution.objects.filter(pk=int(institution_id)).first()
            if institution_id.isdigit() else None)
    if inst is not None and inst.is_other:
        out['is_other'] = True
        for key in ('institution', 'cohort'):
            if not out[key]:
                out['errors'][key] = OTHER_REQUIRED
    return out


def _render_choose(request, person, reg, one_on_one_form=None, status=200, other=None):
    """Render step 2: the programme/module picker or the one-on-one request form.

    Bundles ("All 4 modules — R800 off") are passed as module ids, so the live total
    shows the same discount the review page and the invoice will. When a programme has
    no named intakes the current year stands in, resolved to a real Cohort on enrolment,
    so every student belongs to a group.
    """
    from apps.learning.models import Institution

    current_year = timezone.now().year
    from apps.shop.pricing import live_bundles
    bundles_by_programme = {}
    for bundle in live_bundles():
        items = list(bundle.products.values_list('module_id', 'programme_id'))
        if items and all(module_id for module_id, _ in items):
            for programme_id in {programme_id for _, programme_id in items}:
                bundles_by_programme.setdefault(programme_id, []).append({
                    'modules': [module_id for module_id, _ in items],
                    'off': float(bundle.value), 'label': bundle.name})
    institutions = (Institution.objects.filter(is_active=True)
                    .prefetch_related('programmes__modules__module', 'programmes__cohorts')
                    .order_by('order', 'name'))
    institutions_data = []
    for inst in institutions:
        programmes = []
        for prog in inst.programmes.all():
            if not prog.is_active:
                continue
            modules = [{
                'id': pm.pk, 'code': pm.code, 'name': pm.display_name,
                'price': float(pm.price_per_month or 0), 'free': not (pm.price_per_month or 0),
            } for pm in prog.modules.all() if pm.is_active and not pm.is_general]
            cohorts = [{
                'id': c.pk, 'code': c.code, 'name': c.name,
            } for c in prog.cohorts.all() if c.is_active]
            if not cohorts:
                cohorts = [{'id': 'year', 'code': str(current_year), 'name': f'{current_year} intake'}]
            programmes.append({
                'id': prog.pk, 'code': prog.short_code, 'name': prog.display_name,
                'label': prog.label, 'competency': prog.is_competency_based,
                'mandatory': prog.modules_mandatory,
                'cohorts': cohorts, 'modules': modules,
                'bundles': bundles_by_programme.get(prog.pk, []),
            })
        institutions_data.append({
            'id': inst.pk, 'code': inst.code, 'name': inst.display_name,
            'other': inst.is_other,
            'description': inst.description or '',
            'logo': inst.sharp_logo_url,
            'accent': inst.accent_colour or '#1F3864', 'programmes': programmes,
        })

    from apps.shop.one_on_one import hourly_rate
    hourly = hourly_rate()
    if other is None:
        other = {'is_other': False, 'errors': {},
                 'institution': (reg.get('other_institution') or person.other_institution or ''),
                 'cohort': (reg.get('other_cohort') or person.other_cohort or '')}
    path = 'oneonone' if (one_on_one_form is not None
                          or request.GET.get('path') == 'oneonone'
                          or (request.GET.get('path') != 'programme'
                              and reg.get('track') == 'one_on_one')) else 'programme'
    return render(request, 'accounts/register/step2.html', {
        'page_title': 'Choose', 'person': person, 'reg': reg,
        'institutions': institutions, 'institutions_data': institutions_data, 'step': 2,
        'one_on_one_form': one_on_one_form or forms.OneOnOneRequestForm(prefix='o2o'),
        'one_on_one_open': path == 'oneonone', 'one_on_one_rate': hourly,
        'reg_path': path, 'other': other,
        'reg_hours': {str(k): v for k, v in (reg.get('hours') or {}).items()},
        'posted_institution': (request.POST.get('institution_id') or '') if request.method == 'POST' else '',
    }, status=status)


ONE_ON_ONE_HOURLY_RATE = 300


@login_required
@require_POST
def register_one_on_one(request):
    """Record a one-on-one request for staff to complete by hand; also finishes
    registration for someone who wants only this.

    The welcome pack is best-effort.
    """
    person = _person_for(request.user)
    if person is None or not person.profile_status:
        return redirect('accounts:register')
    form = forms.OneOnOneRequestForm(request.POST, prefix='o2o')
    if not form.is_valid():
        messages.error(request, 'Please check the one-on-one details highlighted below.')
        return _render_choose(request, person, request.session.get('reg', {}), form, status=400)

    req = form.save(commit=False)
    req.person = person
    req.save()
    choice = form.cleaned_data.get('institution_choice')
    if choice is not None and choice.is_other:
        person.other_institution = form.cleaned_data['other_institution']
        person.other_cohort = form.cleaned_data['other_cohort']
        person.save(update_fields=['other_institution', 'other_cohort'])
    _notify_one_on_one(request, req)

    newly_registered = not person.registered
    if newly_registered:
        person.registered = True
        person.save(update_fields=['registered'])
        try:
            from apps.communication.welcome import send_welcome_pack
            send_welcome_pack(request.user)
        except Exception:  # pragma: no cover - best-effort
            logging.getLogger('accounts').exception('accounts: welcome pack failed')
    if person.onboarding_complete and not newly_registered:
        messages.success(request, 'Thank you — your one-on-one request is with our team.')
        return redirect('myhub:index')
    return redirect(reverse('accounts:register-complete') + '?mode=oneonone-request')


def _notify_one_on_one(request, req):
    """Tell the admin team a one-on-one request has arrived. Best effort.

    Best-effort: the request is never lost over a notice.
    """
    try:
        from apps.communication.services import notify
        from apps.shop.fulfilment import staff_recipients
        from core.utils import display_name

        who = display_name(req.person.user)
        url = reverse('staffdesk:ops-one-on-one')
        for user in staff_recipients():
            notify(user, title='New one-on-one request',
                   body=' · '.join(filter(None, [who, req.institution, req.cohort, req.programme])),
                   verb='requested one-on-one coaching', url=url,
                   actor=request.user, category=None)
    except Exception:  # pragma: no cover - never lose the request over a notice
        logging.getLogger('accounts').exception('accounts: one-on-one notice failed')


@login_required
def register_review(request):
    """Step 3 — review and finish: pay now (PayFast) or free week + EFT invoice.
    Every priced registration starts the 7-day trial, even "pay now"; settling
    the invoice turns the modules ACTIVE.

    For a programme with mandatory modules (IAC) every module is taken, whatever was
    posted. The summary uses the same quote the invoice is raised from, so a bundle
    ("All 4 modules") shows here as the discount the invoice will carry.

    Both actions raise the *same* per-module invoice. Access is governed by module
    locking, not the global onboarding gate, so ``pending_invoice_uid`` is cleared
    either way and the student goes into the hub — with their modules open (trial) or
    locked (awaiting payment). When everything was free no invoice is raised. The free
    week starts on *both* paths, so a failed, cancelled or pending payment leaves the
    payer no worse off than a trial student; a cleared payment just flips the modules to
    ACTIVE. Free week + EFT: the invoice PDF goes by e-mail and WhatsApp in one call
    (see :mod:`apps.accounts.registration_billing` for why). ``action == 'pay'`` goes to
    PayFast, and settling unlocks the modules (trial → active).
    """
    person = _person_for(request.user)
    if person is None or not person.profile_status:
        return redirect('accounts:register')
    reg = request.session.get('reg')
    if not reg or not reg.get('programme_id') or not reg.get('module_ids'):
        return redirect('accounts:register-course')

    from decimal import Decimal

    from apps.learning import enrolment as enrol
    from apps.learning.models import Programme, ProgrammeModule

    programme = (Programme.objects.filter(pk=reg['programme_id'])
                 .select_related('institution').first())
    if programme is None:
        note('USER-3002', request, programme=reg.get('programme_id'))
        return redirect('accounts:register-course')
    if reg.get('track') == 'one_on_one':
        return _review_one_on_one(request, person, reg, programme)

    module_ids = reg['module_ids']
    if programme.modules_mandatory:
        module_ids = [int(pk) for pk in _mandatory_module_ids(programme.pk)]
        if sorted(module_ids) != sorted(reg['module_ids']):
            reg['module_ids'] = module_ids
            request.session['reg'] = reg
            request.session.modified = True
    modules = list(ProgrammeModule.objects.filter(
        pk__in=module_ids, programme=programme, is_active=True).select_related('module'))
    if not modules:
        note('USER-3002', request, programme=programme.pk, modules=reg.get('module_ids'))
        return redirect('accounts:register-course')

    from apps.shop.pricing import quote_modules

    quote = quote_modules([(m, Decimal(str(m.price_per_month or 0))) for m in modules])
    line_items = [(f'{m.display_name} ({m.code})', line['gross'])
                  for m, line in zip(modules, quote['lines'])]
    savings = {}
    for line in quote['lines']:
        if line['discount']:
            savings[line['label']] = savings.get(line['label'], Decimal('0')) + line['discount']
    total = quote['total']
    is_free = total <= 0

    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'back':
            return redirect('accounts:register-course')

        enrolments = enrol.register_modules(
            person, programme, [m.pk for m in modules], cohort_id=reg.get('cohort_id'))
        person.registered = True
        try:
            from apps.communication.welcome import send_welcome_pack
            send_welcome_pack(request.user)
        except Exception:  # pragma: no cover - best-effort
            logging.getLogger('accounts').exception('accounts: welcome pack failed')

        with capture('FIN-8002', request):
            invoice = enrol.invoice_for_modules(person, enrolments, send_to_student=False)
        person.pending_invoice_uid = None
        person.save(update_fields=['registered', 'pending_invoice_uid'])
        request.session.pop('reg', None)
        request.session.modified = True

        if invoice is None:
            for e in enrolments:
                e.activate(months=1)
            messages.success(request, 'You are enrolled — enjoy your modules!')
            return redirect('myhub:index')

        enrol.start_trial(enrolments)

        if action == 'trial':
            delivery = registration_billing.deliver_registration(
                person, invoice, enrolments, paid=False, programme=programme)
            sent = ' and '.join(delivery['sent']) or 'your account'
            messages.success(
                request,
                'Your free week has started — everything is open until '
                f'{delivery["trial_ends"]:%A, %-d %B}. Your invoice has been sent to {sent}; '
                'pay it by EFT before the week is out and your modules stay open.')
            if delivery['failed']:
                messages.info(request, 'We could not reach you on '
                                       f'{" and ".join(delivery["failed"])} — the invoice is in '
                                       'your account under Finance either way.')
            return redirect(reverse('accounts:register-complete') + '?mode=eft')

        return redirect('finance:pay', public_id=invoice.public_id)

    return render(request, 'accounts/register/step3.html', {
        'page_title': 'Review', 'person': person, 'programme': programme,
        'modules': modules, 'line_items': line_items, 'total': total,
        'subtotal': quote['subtotal'], 'savings': sorted(savings.items()),
        'is_free': is_free, 'currency': 'ZAR', 'step': 3,
    })


def _review_one_on_one(request, person, reg, programme):
    """Step 3 on the one-on-one path: the modules, the hours bought now, and
    pay now or by EFT invoice. The modules open straight away either way — the
    hours reach the balance once the invoice is paid (apps.shop.one_on_one).

    The welcome pack is best-effort.
    """
    from decimal import Decimal

    from apps.learning.models import ProgrammeModule
    from apps.shop import one_on_one

    modules = list(ProgrammeModule.objects.without_general().filter(
        pk__in=reg['module_ids'], programme=programme, is_active=True).select_related('module'))
    if not modules:
        return redirect('accounts:register-course')
    rate = one_on_one.hourly_rate()
    hours = {int(k): int(v) for k, v in (reg.get('hours') or {}).items()}
    lines = [(m, hours.get(m.pk, 1)) for m in modules]
    line_items = [(f'{m.display_name} ({m.code})', h, rate * h) for m, h in lines]
    total = sum((rate * h for _m, h in lines), Decimal('0'))

    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'back':
            return redirect(reverse('accounts:register-course') + '?path=oneonone')
        one_on_one.enrol(person, programme, [m.pk for m in modules], cohort_id=reg.get('cohort_id'))
        person.registered = True
        person.pending_invoice_uid = None
        person.save(update_fields=['registered', 'pending_invoice_uid'])
        try:
            from apps.communication.welcome import send_welcome_pack
            send_welcome_pack(request.user)
        except Exception:  # pragma: no cover - best-effort
            logging.getLogger('accounts').exception('accounts: welcome pack failed')
        request.session.pop('reg', None)
        request.session.modified = True

        buying = [(m, h) for m, h in lines if h > 0]
        if not buying:
            messages.success(request, 'You are registered for one-on-one coaching. Buy hours '
                                      'whenever you are ready to book a session.')
            return redirect(reverse('accounts:register-complete') + '?mode=oneonone')
        with capture('FIN-8002', request):
            purchase = one_on_one.create_purchase(request.user, buying, by=request.user,
                                                  send=action != 'pay')
        if action == 'pay' and purchase is not None:
            return redirect('finance:pay', public_id=purchase.invoice.public_id)
        messages.success(request, f'Your invoice for R{purchase.invoice.total:,.2f} has been e-mailed. '
                                  'Your modules are open now; your hours are added once it is paid, '
                                  'and then you can book your first session.')
        return redirect(reverse('accounts:register-complete') + '?mode=oneonone')

    return render(request, 'accounts/register/step3_one_on_one.html', {
        'page_title': 'Review', 'person': person, 'programme': programme,
        'line_items': line_items, 'total': total, 'rate': rate, 'currency': 'ZAR', 'step': 3,
    })


REGISTRATION_OUTCOMES = {
    'paid': {
        'heading': 'Payment completed',
        'detail': 'Thank you — your payment came through and every module you '
                  'registered for is unlocked.',
        'note': 'A receipt and your registration summary are on their way to your inbox.',
        'tone': 'success',
    },
    'oneonone': {
        'heading': 'You are registered for one-on-one',
        'detail': 'Your modules are open now — schedule, documents and your own '
                  'session recordings.',
        'note': 'Book a session from "My one-on-one sessions" once your hours are '
                'paid. Your invoice is on its way to your inbox.',
        'tone': 'success',
    },
    'oneonone-request': {
        'heading': 'Request received',
        'detail': 'Thank you — your one-on-one request is with our team. We will '
                  'match you with an educator and set up your enrolment.',
        'note': 'We will be in touch by e-mail or WhatsApp to confirm your coach '
                'and your first session.',
        'tone': 'success',
    },
    'eft': {
        'heading': 'Registration completed',
        'detail': 'Setting up your account — your free week starts now and the '
                  'whole platform is open.',
        'note': 'Your invoice and registration summary are on their way to your '
                'inbox. Pay it by EFT before the week is out and your modules stay open.',
        'tone': 'info',
    },
}

REGISTRATION_COMPLETE_SECONDS = 5


@login_required
def registration_complete(request):
    """Hand-off screen shown for a few seconds before the dashboard.

    ``?mode=paid|eft|oneonone``; unknown values fall back to the EFT copy,
    which claims the least.
    """
    mode = request.GET.get('mode', 'eft')
    outcome = REGISTRATION_OUTCOMES.get(mode) or REGISTRATION_OUTCOMES['eft']
    return render(request, 'accounts/register/complete.html', {
        'page_title': outcome['heading'],
        'outcome': outcome,
        'mode': mode,
        'seconds': REGISTRATION_COMPLETE_SECONDS,
        'next_url': reverse('myhub:index'),
    })


@login_required
@require_POST
def accept_terms(request):
    """Record that the signed-in person accepted the current Terms & Conditions
    (the one-time prompt in partials/_terms_update.html), then go back."""
    from .forms import TERMS_VERSION
    profile = getattr(request.user, 'profile', None)
    if profile is not None:
        profile.terms_accepted_at = timezone.now()
        profile.terms_version = TERMS_VERSION
        profile.save(update_fields=['terms_accepted_at', 'terms_version'])
    return redirect_back(request, fallback='myhub:index')


def accept_invite(request, token):
    """Public entry point for an invite link.

    Parent invites go to :func:`apps.accounts.parent_views.parent_signup`.
    Educator invites are stashed in the session for the normal sign-up, which
    then sets the role from the invite.

    One e-mail, one account: when the invited address already has an account the page
    says so at the link, before a sign-up form that could never succeed.
    """
    invite = (models.Invitation.objects
              .filter(token=token).select_related('student__user', 'programme').first())
    if invite is None or not invite.is_open:
        if invite is not None:
            note('USER-4002', request, status=invite.status, expired=invite.is_expired)
        return render(request, 'accounts/invite-invalid.html', {'invite': invite}, status=410)
    if invite.role == invite.ROLE_PARENT:
        from .parent_views import parent_signup
        return parent_signup(request, invite)

    User = get_user_model()
    existing = User.objects.filter(email__iexact=invite.email).first()
    if existing is not None and not (request.user.is_authenticated
                                     and request.user.pk == existing.pk):
        note('USER-2002', request, kind='invite-email-taken', role=invite.role)
        existing_person = getattr(existing, 'profile', None)
        return render(request, 'accounts/invite-invalid.html', {
            'invite': invite,
            'email_taken': True,
            'existing_role': (existing_person.get_user_type_display()
                              if existing_person else 'an account'),
        }, status=409)

    if request.user.is_authenticated:
        person = _person_for(request.user)
        if person and not person.onboarding_complete and person.user_type == invite.role:
            return redirect('accounts:register-parent' if invite.role == invite.ROLE_PARENT
                            else 'accounts:register-educator')
        return redirect('myhub:index')
    request.session['invite_token'] = str(invite.token)
    return redirect(f"{reverse('myhub:page-register')}?email={invite.email}")


@login_required
def register_parent(request):
    """Single-page registration for an invited parent/guardian/sponsor."""
    person = _person_for(request.user) or models.Person.objects.create(user=request.user)
    if person.onboarding_complete:
        return redirect('myhub:index')
    if person.user_type != models.Invitation.ROLE_PARENT:
        return redirect('accounts:register')
    invite = person.invite
    student = invite.student if invite else None
    if request.method == 'POST':
        form = forms.ParentRegistrationForm(request.POST, instance=person)
        if form.is_valid():
            p = form.save(commit=False)
            p.profile_status = True
            p.registered = True
            if student:
                p.child_name = (f'{student.first_name} {student.last_name}'.strip()
                                or (student.user.email if student.user_id else ''))
            p.save()
            if student and student.user_id and models.ParentLink.can_add_parent(student.user):
                models.ParentLink.objects.get_or_create(
                    parent=request.user, student=student.user,
                    defaults={'relationship': form.cleaned_data.get('relationship', '')})
            if invite:
                invite.accept(request.user)
            try:
                emails.send_registration_summary(person)
            except Exception:
                pass
            messages.success(request, 'Registration complete — welcome aboard!')
            return redirect('myhub:index')
    else:
        form = forms.ParentRegistrationForm(instance=person)
    return render(request, 'accounts/register-parent.html', {
        'page_title': 'Register', 'form': form, 'student': student, 'person': person,
        **_wa_context(person)})


@login_required
def register_educator(request):
    """Single-page registration for an invited educator."""
    person = _person_for(request.user) or models.Person.objects.create(user=request.user)
    if person.onboarding_complete:
        return redirect('myhub:index')
    if person.user_type != models.Invitation.ROLE_EDUCATOR:
        return redirect('accounts:register')
    invite = person.invite
    programme = invite.programme if invite else None
    if request.method == 'POST':
        form = forms.EducatorRegistrationForm(request.POST, instance=person)
        if form.is_valid():
            p = form.save(commit=False)
            p.profile_status = True
            p.registered = True
            if programme:
                p.enrolled_class = programme.name
            p.save()
            if programme:
                services.enrol_educator(person, programme)
            if invite:
                invite.accept(request.user)
            try:
                emails.send_registration_summary(person, programme=programme)
            except Exception:
                pass
            messages.success(request, 'Registration complete — welcome aboard!')
            return redirect('myhub:index')
    else:
        form = forms.EducatorRegistrationForm(instance=person)
    return render(request, 'accounts/register-educator.html', {
        'page_title': 'Register', 'form': form, 'programme': programme, 'person': person,
        **_wa_context(person)})


@login_required
def add_educator(request):
    """Admin/staff: invite an educator to a programme by e-mail."""
    if not _require_admin_staff(request):
        messages.error(request, 'You do not have permission to invite educators.')
        return redirect('myhub:index')
    if request.method == 'POST':
        form = forms.AddEducatorForm(request.POST)
        if form.is_valid():
            invite = models.Invitation.objects.create(
                role=models.Invitation.ROLE_EDUCATOR, email=form.cleaned_data['email'],
                programme=form.cleaned_data['programme'], invited_by=request.user)
            emails.send_invite(invite)
            messages.success(request, f'Invite sent to {invite.email} for {invite.programme}.')
            return redirect('accounts:manage-members')
    else:
        form = forms.AddEducatorForm()
    return render(request, 'accounts/add-educator.html', {'page_title': 'Add educator', 'form': form})


@login_required
def enrol_checkout(request):
    """Settle the registration invoice: paid → PayFast; free → record a 0 payment
    here. Settling lifts the onboarding gate.

    A free enrolment still "goes through checkout": a 0 payment is recorded and the
    invoice marked paid (``refresh_status`` will not for a 0 total). A 0-total invoice
    does not fire ``finance._on_invoice_paid``, so the enrolment/e-mail fulfilment is
    run directly; it clears the onboarding gate.
    """
    person = _person_for(request.user)
    if person is None or not person.pending_invoice_uid:
        return redirect('myhub:index')
    invoice = person.get_pending_invoice()
    if invoice is None:
        person.pending_invoice_uid = None
        person.save(update_fields=['pending_invoice_uid'])
        return redirect('myhub:index')
    if invoice.status == invoice.STATUS_PAID:
        person.pending_invoice_uid = None
        person.save(update_fields=['pending_invoice_uid'])
        messages.success(request, 'Enrolment confirmed — welcome aboard!')
        return redirect('myhub:index')

    is_free = (invoice.total or 0) <= 0
    if request.method == 'POST':
        from apps.finance import services as finance_services
        if is_free:
            finance_services.settle_payment(
                invoice, 0, method='free', gateway=invoice.payments.model.GATEWAY_MANUAL)
            invoice.status = invoice.STATUS_PAID
            invoice.save(update_fields=['status', 'updated_at'])
            services.fulfil_paid_registration(person, invoice)
            messages.success(request, 'Enrolment confirmed — welcome aboard!')
            return redirect('myhub:index')
        return redirect('finance:pay', public_id=invoice.public_id)

    return render(request, 'accounts/enrol-checkout.html', {
        'page_title': 'Checkout', 'invoice': invoice, 'person': person,
        'is_free': is_free, 'item': invoice.items.first(),
    })


@login_required
def complete_profile(request):
    """Step 2: finish the personal profile, then land on the dashboard."""
    person = _person_for(request.user)
    if person is None:
        person = models.Person.objects.create(user=request.user)
    if not person.registered:
        return redirect('accounts:register')

    if request.method == 'POST':
        form = forms.CompleteProfileForm(request.POST, request.FILES, instance=person)
        if form.is_valid():
            obj = form.save(commit=False)
            obj.profile_status = True
            obj.save()
            messages.success(request, 'Welcome aboard! Your profile is complete.')
            return redirect('myhub:index')
        messages.error(request, t('messages.form_errors', 'Please correct the errors highlighted below.'))
    else:
        form = forms.CompleteProfileForm(instance=person)
    return render(request, 'accounts/complete-profile.html', {
        'page_title': 'Complete your profile', 'form': form, 'person': person,
    })


@login_required
def my_profile(request):
    """My own profile — rendered through the shared profile shell."""
    person = _person_for(request.user)
    if person is None:
        return redirect('myhub:index')
    return _render_person_profile(request, person, 'about')


@login_required
def profile(request, pk, tab='about'):
    """Anyone's profile. ``profile_visibility`` is gated here (it must redirect);
    finer filtering happens in :func:`core.profiles.person_context`.

    Somebody the viewer shares no programme, module, chat or family with gets a 404, the
    same as an id that does not exist (see :mod:`apps.accounts.visibility`). Retired
    tabs (feed, progress, documents, activity) and unknown ones land on About, so old
    links and bookmarks still open the profile.
    """
    person = get_object_or_404(models.Person.objects.select_related('user'), pk=pk)
    from .visibility import can_view_person
    if not can_view_person(request.user, person):
        raise Http404('No such person')
    is_self = person.user_id == request.user.id
    viewer = _person_for(request.user)
    is_staff = (request.user.is_staff or request.user.is_superuser
                or (viewer and viewer.user_type in ('admin', 'staff')))

    if not is_self and not is_staff and person.user_id:
        vis = models.UserSettings.for_user(person.user).profile_visibility
        if vis == models.UserSettings.VISIBILITY_PRIVATE:
            messages.info(request, 'That member keeps their profile private.')
            return redirect('myhub:index')
        if vis == models.UserSettings.VISIBILITY_MEMBERS and not _shares_group(viewer, person):
            messages.info(request, 'That profile is only visible to members of the same course.')
            return redirect('myhub:index')

    from core.profiles import PERSON_DEFAULT_TAB, PERSON_TABS
    if tab not in {k for k, _l, _i in PERSON_TABS}:
        return redirect('accounts:profile', pk=person.pk, tab=PERSON_DEFAULT_TAB)

    return _render_person_profile(request, person, tab)


def _render_person_profile(request, person, tab):
    """Render the person profile page on ``tab`` (unknown tabs fall back to the default).

    ``base_template`` comes from :func:`core.profiles.shell_base`, so an HTMX tab swap
    gets the fragment only.
    """
    from core.profiles import PERSON_DEFAULT_TAB, PERSON_TABS, person_context, shell_base

    if tab not in {k for k, _l, _i in PERSON_TABS}:
        tab = PERSON_DEFAULT_TAB
    ctx = person_context(request, person, tab)
    ctx['page_title'] = str(person)
    ctx['base_template'] = shell_base(request)
    return render(request, 'accounts/my-profile.html', ctx)


def _shares_group(viewer_person, person):
    """True if viewer and owner share a module, taught or enrolled — educators
    are never enrolled, so teaching must count."""
    if not viewer_person:
        return False
    def module_ids(p):
        """Ids of the modules a person studies or teaches."""
        return set(p.selected_modules.values_list('pk', flat=True)) | \
               set(p.taught_modules.values_list('pk', flat=True))

    return bool(module_ids(viewer_person) & module_ids(person))


@login_required
def member_profile(request, pk):
    """Legacy alias for :func:`profile`, kept for old links."""
    return redirect('accounts:profile', pk=pk)


def _email_verified(user):
    """True/False if allauth tracks a verified e-mail for ``user`` (None if
    allauth is unavailable)."""
    try:
        from allauth.account.models import EmailAddress
        return EmailAddress.objects.filter(user=user, verified=True).exists()
    except Exception:
        return None


@login_required
def settings(request):
    """Tabbed settings hub; POST dispatches on the hidden ``form_name`` so each
    form submits without clobbering the others.

    A language change is applied to this response so the redirect already shows it;
    ``UserPreferenceMiddleware`` reads the saved row from then on. No vacation dates are
    offered for calendar categories: a candidate's holidays are not ours to keep. The
    connected-calendar forms POST to :mod:`apps.livesessions.views`. ``form`` is kept in
    the context alongside ``profile_form`` for backwards compatibility.
    """
    person = _person_for(request.user)
    if person is None:
        person = models.Person.objects.create(user=request.user)
    user_settings = models.UserSettings.for_user(request.user)

    consent = _get_or_create_consent(person)
    study_profile, _ = models.PersonStudyProfile.objects.get_or_create(person=person)

    profile_form = forms.ProfileForm(instance=person)
    settings_form = forms.UserSettingsForm(instance=user_settings)
    consent_form = forms.PrivacyConsentForm(instance=consent)
    active_tab = 'account'

    if request.method == 'POST':
        which = request.POST.get('form_name')
        if which == 'profile':
            active_tab = 'account'
            profile_form = forms.ProfileForm(request.POST, request.FILES, instance=person)
            if profile_form.is_valid():
                obj = profile_form.save(commit=False)
                obj.profile_status = True
                obj.save()
                messages.success(request, t('messages.saved', '{name} saved successfully.', name='Profile'))
                return redirect('accounts:my-profile')
            messages.error(request, t('messages.form_errors', 'Please correct the errors highlighted below.'))
        elif which == 'preferences':
            active_tab = 'preferences'
            settings_form = forms.UserSettingsForm(request.POST, instance=user_settings)
            if settings_form.is_valid():
                saved = settings_form.save()
                translation.activate(saved.language)
                messages.success(request, t('messages.saved', '{name} saved successfully.', name='Preferences'))
                response = redirect('accounts:settings')
                response.set_cookie(dj_settings.LANGUAGE_COOKIE_NAME, saved.language,
                                    max_age=365 * 24 * 3600, samesite='Lax')
                return response
            messages.error(request, t('messages.form_errors', 'Please correct the errors highlighted below.'))
        elif which == 'calendar':
            active_tab = 'calendar'
            cats = {'exam': 'exam_dates', 'assignment': 'assignment_dates',
                    'practical': 'practical_dates'}
            for prefix, field in cats.items():
                labels = request.POST.getlist(f'{prefix}_label')
                dates = request.POST.getlist(f'{prefix}_date')
                rows = [{'label': (lbl or '').strip(), 'date': d}
                        for lbl, d in zip(labels, dates) if d]
                setattr(study_profile, field, rows)
            study_profile.save()
            messages.success(request, 'Your academic calendar has been saved.')
            return redirect('accounts:settings')
        elif which == 'consent':
            active_tab = 'consent'
            consent_form = forms.PrivacyConsentForm(request.POST, instance=consent)
            if consent_form.is_valid():
                obj = consent_form.save(commit=False)
                obj.consent_date = timezone.now()
                obj.consent_version = models.CONSENT_VERSION
                obj.consent_ip = _client_ip(request)
                obj.consent_device = request.META.get('HTTP_USER_AGENT', '')[:300]
                obj.save()
                messages.success(request, 'Your privacy choices have been saved.')
                return redirect('accounts:settings')
            messages.error(request, t('messages.form_errors', 'Please correct the errors highlighted below.'))

    from apps.livesessions.forms import CalendarSubscriptionForm
    from apps.livesessions.models import CalendarSubscription
    settings_row = models.UserSettings.for_user(request.user)

    if request.GET.get('tab') in ('connected',):
        active_tab = 'connected'

    return render(request, 'accounts/settings.html', {
        'page_title': 'Settings',
        'platform_zone': dj_settings.TIME_ZONE,
        'person': person,
        'calendar_subscriptions': CalendarSubscription.objects.filter(user=request.user),
        'calendar_form': CalendarSubscriptionForm(user=request.user),
        'calendar_feed_url': request.build_absolute_uri(
            reverse('livesessions:personal-feed', args=[settings_row.calendar_token])),
        'form': profile_form,
        'profile_form': profile_form,
        'settings_form': settings_form,
        'consent_form': consent_form,
        'consent': consent,
        'consent_version': models.CONSENT_VERSION,
        'calendar_cats': [
            {'prefix': 'exam', 'title': 'Exam dates', 'icon': 'bi-mortarboard', 'rows': study_profile.exam_dates or []},
            {'prefix': 'assignment', 'title': 'Assignment dates', 'icon': 'bi-file-earmark-text', 'rows': study_profile.assignment_dates or []},
            {'prefix': 'practical', 'title': 'Practical dates', 'icon': 'bi-tools', 'rows': study_profile.practical_dates or []},
        ],
        'user_settings': user_settings,
        'email_verified': _email_verified(request.user),
        'active_tab': active_tab,
    })


@login_required
@require_POST
def data_export(request):
    """POPIA right of access — download everything we hold about you as JSON,
    and record that an export was requested."""
    person = _person_for(request.user) or models.Person.objects.create(user=request.user)
    consent = _get_or_create_consent(person)
    consent.data_export_requested = timezone.now()
    consent.save(update_fields=['data_export_requested', 'updated_at'])
    resp = JsonResponse(_serialize_person(person), json_dumps_params={'indent': 2})
    resp['Content-Disposition'] = f'attachment; filename="my-data-{request.user.pk}.json"'
    return resp


def _html_to_pdf(html):
    """Render an HTML string to PDF bytes (xhtml2pdf), or None on failure."""
    import io
    try:
        from xhtml2pdf import pisa
        buf = io.BytesIO()
        result = pisa.CreatePDF(html, dest=buf)
        return None if result.err else buf.getvalue()
    except Exception:  # pragma: no cover
        return None


def _export_sections(person):
    """Turn the raw data snapshot into humanised (title, [(label, value)]) sections."""
    raw = _serialize_person(person)
    titles = [
        ('account', 'Account'), ('profile', 'Profile'), ('contact', 'Contact & address'),
        ('study_profile', 'Academic calendar'), ('consent', 'Consent'),
    ]
    sections = []
    for key, title in titles:
        data = raw.get(key)
        if not data:
            continue
        rows = []
        for k, v in data.items():
            if v in (None, '', [], {}):
                continue
            if isinstance(v, list):
                v = ', '.join(str(x) for x in v)
            elif isinstance(v, dict):
                v = ', '.join(f'{a}: {b}' for a, b in v.items())
            rows.append((k.replace('_', ' ').capitalize(), v))
        if rows:
            sections.append((title, rows))
    return sections


@login_required
@require_POST
def data_export_pdf(request):
    """POPIA right of access — a human-readable PDF of the data we hold.

    Falls back to the JSON export if the PDF cannot be produced.
    """
    from core.branding import strings
    person = _person_for(request.user) or models.Person.objects.create(user=request.user)
    consent = _get_or_create_consent(person)
    consent.data_export_requested = timezone.now()
    consent.save(update_fields=['data_export_requested', 'updated_at'])
    html = render_to_string('accounts/data_export_pdf.html', {
        'sections': _export_sections(person), 'person': person,
        'brand': strings().get('brand', {}), 'now': timezone.now(),
    })
    pdf = _html_to_pdf(html)
    if pdf is None:
        return data_export(request)
    resp = HttpResponse(pdf, content_type='application/pdf')
    resp['Content-Disposition'] = f'attachment; filename="my-data-{request.user.pk}.pdf"'
    return resp


@require_POST
def cookie_consent(request):
    """Record the cookie-banner choice: a year-long cookie for the browser, and
    (for signed-in users) the ``cookie_consent`` flag on their profile. Open to
    anonymous visitors so the banner works on public pages too."""
    accepted = request.POST.get('choice') == 'accept'
    if request.user.is_authenticated:
        person = _person_for(request.user)
        if person is not None:
            c = _get_or_create_consent(person)
            c.cookie_consent = accepted
            c.consent_date = timezone.now()
            c.consent_ip = _client_ip(request)
            c.save(update_fields=['cookie_consent', 'consent_date', 'consent_ip', 'updated_at'])
    resp = redirect_back(request, '/')
    resp.set_cookie('cookie_consent', 'accepted' if accepted else 'declined',
                    max_age=60 * 60 * 24 * 365, samesite='Lax')
    return resp


@login_required
@require_POST
def set_theme(request):
    """Persist the navbar light/dark/auto choice to the same row as the settings
    page, so it follows the user across sign-ins and devices."""
    theme = (request.POST.get('theme') or '').strip()
    valid = {value for value, _label in models.UserSettings.THEME_CHOICES}
    if theme not in valid:
        return JsonResponse({'ok': False, 'error': 'unknown theme'}, status=400)
    user_settings = models.UserSettings.for_user(request.user)
    user_settings.theme = theme
    user_settings.save(update_fields=['theme', 'updated_at'])
    response = JsonResponse({'ok': True, 'theme': theme})
    response.set_cookie('ui_theme', theme, max_age=365 * 24 * 3600, samesite='Lax')
    return response


@login_required
def set_language(request):
    """Save the interface language and bounce back (UserPreferenceMiddleware
    applies it on later requests)."""
    language = (request.GET.get('lang') or request.POST.get('lang') or '').strip()
    valid = {code for code, _label in dj_settings.LANGUAGES}
    if language in valid:
        user_settings = models.UserSettings.for_user(request.user)
        user_settings.language = language
        user_settings.save(update_fields=['language', 'updated_at'])
        translation.activate(language)
    response = redirect_back(request, 'myhub:index')
    if language in valid:
        response.set_cookie(dj_settings.LANGUAGE_COOKIE_NAME, language,
                            max_age=365 * 24 * 3600, samesite='Lax')
    return response


@login_required
def close_account(request):
    """Verify the user, then soft-close their account (30-day grace).

    The done page is rendered directly: a redirect would bounce the now-anonymous user
    to login.
    """
    user_settings = models.UserSettings.for_user(request.user)

    if request.method == 'POST':
        form = forms.CloseAccountForm(request.user, request.POST)
        if form.is_valid():
            user_settings.mark_closing()
            request.user.is_active = False
            request.user.save(update_fields=['is_active'])
            logout(request)
            return render(request, 'accounts/close-account-done.html', {
                'page_title': 'Account closed',
                'grace_days': models.UserSettings.GRACE_DAYS,
            })
        messages.error(request, 'Could not close your account — see the errors below.')
    else:
        form = forms.CloseAccountForm(request.user)

    return render(request, 'accounts/close-account.html', {
        'page_title': 'Close account', 'form': form,
        'grace_days': models.UserSettings.GRACE_DAYS,
    })


@login_required
def close_account_done(request):
    """Standalone post-closure confirmation page (also shown inline on POST)."""
    return render(request, 'accounts/close-account-done.html', {
        'page_title': 'Account closed',
        'grace_days': models.UserSettings.GRACE_DAYS,
    })


@login_required
def manage_members(request):
    """Admin/staff roster: every person with the modules they are enrolled in.

    "Registered, holding no open module" means: they have an enrolment row and a settled
    (or absent) registration invoice, and every module is still locked.
    """
    if not _require_admin_staff(request):
        messages.error(request, 'You do not have permission to manage members.')
        return redirect('myhub:index')
    people = (models.Person.objects.filter(preview_of__isnull=True).select_related('user')
              .order_by('first_name', 'last_name'))
    from apps.learning.models import ModuleEnrolment
    open_states = [ModuleEnrolment.STATUS_TRIAL, ModuleEnrolment.STATUS_ACTIVE]
    pending = (models.Person.objects
               .filter(registered=True, pending_invoice_uid__isnull=True,
                       module_enrolments__isnull=False)
               .exclude(module_enrolments__status__in=open_states)
               .select_related('user')
               .distinct()
               .order_by('first_name', 'last_name'))
    return render(request, 'accounts/manage-members.html', {
        'page_title': 'Manage members', 'people': people, 'pending': pending,
    })


@login_required
@require_POST
def member_role(request, pk):
    """Change someone's account type. **Administrators only** — letting staff
    mint more staff would make the distinction meaningless.

    Demoting the last administrator is refused: nobody could grant roles again.
    """
    from . import roles_admin
    if not roles_admin.is_admin_user(request.user):
        note('USER-2001', request, kind='role-change')
        messages.error(request, 'Only an administrator can change what kind of account '
                                'someone has.')
        return redirect('accounts:manage-members')

    person = get_object_or_404(models.Person.objects.select_related('user'), pk=pk)
    new_role = (request.POST.get('user_type') or '').strip()
    error = roles_admin.role_change_error(request.user, person, new_role)
    if error:
        messages.error(request, error)
        return redirect('accounts:manage-members')

    was = person.get_user_type_display()
    person.user_type = new_role
    person.save(update_fields=['user_type'])
    roles_admin.sync_django_admin_flag(person)
    messages.success(request, f'{person} is now {person.get_user_type_display()} '
                              f'(was {was}).')
    return redirect('accounts:manage-members')


@login_required
@require_POST
def member_developer(request, pk):
    """Grant or remove developer access. **Developers only** — see
    :mod:`apps.accounts.developers`. Granting it also makes the person an
    administrator; anybody else gets a 404, as on the developer tools."""
    from . import developers
    developers.require_developer(request)
    person = get_object_or_404(models.Person.objects.select_related('user'), pk=pk)
    grant = request.POST.get('grant') == '1'
    error = developers.developer_change_error(request.user, person, grant)
    if error:
        messages.error(request, error)
        return redirect('accounts:manage-members')
    if grant:
        developers.make_developer(person)
        messages.success(request, f'{person} is now a developer (and an administrator).')
    elif person.is_developer:
        person.is_developer = False
        person.save(update_fields=['is_developer'])
        messages.success(request, f'{person} is no longer a developer; they stay an administrator.')
    return redirect('accounts:manage-members')


@login_required
def member_enrol(request, pk):
    """Admin/staff: set a person's modules via
    :func:`apps.accounts.services.set_module_membership`. Newly ticked priced
    modules start the free week; enrolment is not payment.
    """
    if not _require_admin_staff(request):
        messages.error(request, 'You do not have permission to manage members.')
        return redirect('myhub:index')

    from apps.learning.models import Programme, ProgrammeModule
    person = get_object_or_404(models.Person.objects.select_related('user'), pk=pk)
    held = set(person.selected_modules.values_list('id', flat=True))

    if request.method == 'POST':
        wanted = {int(i) for i in request.POST.getlist('modules') if str(i).isdigit()}
        removed = []
        change = services.set_module_membership(
            person, add_ids=wanted - held, remove_ids=held - wanted, actor=request.user,
            undo_sink=removed)
        parts = [f'Enrolment updated for {person}.']
        if change.removed:
            parts.append('Closed: ' + ', '.join(m.code for m in change.removed) + '.')
        if change.trialled:
            parts.append(
                'Free week started for ' + ', '.join(e.programme_module.code for e in change.trialled)
                + f' — open until {timezone.localtime(change.trial_ends):%a %-d %b, %H:%M}.')
        already = [e for e in change.added if e not in change.trialled
                   and not e.programme_module.is_free and not e.is_unlocked]
        if already:
            parts.append(
                'Already trialled or paid before, so no new free week: '
                + ', '.join(e.programme_module.code for e in already)
                + ' — open them from Record payment & grant access.')
        if change.cancelled_invoices:
            parts.append('Cancelled unpaid invoice'
                         + ('s ' if len(change.cancelled_invoices) > 1 else ' ')
                         + ', '.join(str(i.number) for i in change.cancelled_invoices) + '.')
        if removed:
            parts.append('Removed modules can be put back with Undo.')
        from . import undo
        undo.success_with_undo(request, ' '.join(parts), removed[0] if removed else None)
        return redirect('accounts:manage-members')

    programmes = (Programme.objects.filter(is_active=True)
                  .select_related('institution').order_by('institution__name', 'name'))
    modules = (ProgrammeModule.objects.filter(is_active=True)
               .select_related('programme', 'module')
               .order_by('programme__name', 'order', 'name'))
    return render(request, 'accounts/member-enrol.html', {
        'page_title': f'Enrol · {person}', 'person': person,
        'programmes': programmes, 'modules': modules,
        'enrolled_module_ids': held,
    })


@login_required
def closed_accounts(request):
    """Recovery desk: accounts still in their grace period (instant reactivate)
    and purged archives (rebuilt from the archive park, minus links dropped to
    keep other people's threads intact)."""
    if not _require_admin_staff(request):
        messages.error(request, 'You do not have permission to manage closed accounts.')
        return redirect('myhub:index')

    now = timezone.now()
    closing = (models.UserSettings.objects
               .filter(is_closing=True)
               .select_related('user', 'user__profile', 'closed_by')
               .order_by('purge_at'))
    rows = []
    for user_settings in closing:
        remaining = None
        if user_settings.purge_at:
            remaining = (user_settings.purge_at - now).days
        rows.append({'settings': user_settings, 'user': user_settings.user,
                     'person': getattr(user_settings.user, 'profile', None),
                     'days_left': remaining,
                     'overdue': remaining is not None and remaining < 0})

    archives = models.AccountArchive.objects.all()[:200]

    return render(request, 'accounts/closed-accounts.html', {
        'page_title': 'Closed accounts',
        'closing': rows,
        'archives': archives,
        'grace_days': models.UserSettings.GRACE_DAYS,
        'admin_grace_days': models.UserSettings.ADMIN_GRACE_DAYS,
    })


@login_required
@require_POST
def reactivate_account(request, pk):
    """Undo a closure that is still inside its grace period."""
    if not _require_admin_staff(request):
        messages.error(request, 'You do not have permission to reactivate accounts.')
        return redirect('myhub:index')

    User = get_user_model()
    user = get_object_or_404(User, pk=pk)
    user_settings = models.UserSettings.for_user(user)
    if not user_settings.is_closing:
        messages.info(request, f'{user.email or user} is not a closed account.')
        return redirect('accounts:closed-accounts')

    from . import archiving
    archiving.restore_account(user, by=request.user, reason='Reactivated on the Closed accounts page')
    messages.success(
        request,
        f'{user.email or user} has been reactivated. They can sign in again with '
        f'their existing password.')
    return redirect('accounts:closed-accounts')


@login_required
@require_POST
def restore_archive(request, pk):
    """Rebuild a purged account from the archive park."""
    if not _require_admin_staff(request):
        messages.error(request, 'You do not have permission to restore accounts.')
        return redirect('myhub:index')

    archive = get_object_or_404(models.AccountArchive, pk=pk)
    if archive.restored_at:
        messages.info(request, f'{archive.display_name} has already been restored.')
        return redirect('accounts:closed-accounts')

    from . import archiving
    try:
        user = archiving.restore_archive(archive)
    except Exception as exc:
        report('USER-2004', exc, request, context={'archive': archive.pk})
        messages.error(
            request,
            f'Could not restore {archive.display_name}: {exc}. The archive is '
            f'untouched — nothing was lost by the attempt.')
        return redirect('accounts:closed-accounts')

    messages.success(
        request,
        f'{archive.display_name} has been restored from the archive '
        f'({archive.content_rows} row(s)). Their password was not restored — ask '
        f'them to use “Forgot password” on the login page to set a new one.')
    return redirect('accounts:member-profile', pk=getattr(getattr(user, 'profile', None), 'pk', 0)) \
        if getattr(user, 'profile', None) else redirect('accounts:closed-accounts')


@login_required
def undo_action(request, pk):
    """Confirmation page behind every "Undo" link. GET previews (PayFast money
    needs an acknowledged warning); POST performs it with a required reason."""
    from . import undo

    if not undo.can_undo(request.user):
        note('USER-2001', request, kind='undo')
        messages.error(request, 'Only administrators and staff can undo actions.')
        return redirect('myhub:index')

    action = get_object_or_404(models.UndoAction, pk=pk)
    next_url = request.POST.get('next') or request.GET.get('next') or ''
    if not next_url.startswith('/') or next_url.startswith('//'):
        next_url = ''
    try:
        handler = undo.handler_for(action)
    except undo.UndoError as exc:
        messages.error(request, str(exc))
        return redirect(next_url or 'myhub:index')

    warning = handler.warning(action) if handler.warning else ''
    blocked = '' if action.undone_at else (handler.blocked(action) if handler.blocked else '')
    error = ''
    if request.method == 'POST' and not action.undone_at:
        try:
            result = undo.perform(action, by=request.user, reason=request.POST.get('reason', ''),
                                  acknowledged=bool(request.POST.get('acknowledge')))
        except undo.UndoError as exc:
            error = str(exc)
        else:
            messages.success(request, f'Undone. {result}')
            return redirect(next_url or (action.payload or {}).get('next') or 'myhub:index')

    return render(request, 'accounts/undo-confirm.html', {
        'page_title': 'Undo', 'action': action,
        'lines': handler.describe(action) if handler.describe and not action.undone_at else [],
        'warning': warning, 'blocked': blocked, 'error': error,
        'reason': request.POST.get('reason', ''), 'next': next_url,
    }, status=400 if error else 200)
