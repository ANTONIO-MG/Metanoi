"""Admin/staff management of the academic spine — Institution → Programme →
ProgrammeModule (offering) → Cohort.

In-app CRUD behind the "Academic structure" area. Access is admin/staff only, enforced by :func:`_admin_staff_required` (educators
are teach-only, students/parents never reach these routes).
"""
from functools import wraps

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from core.roles import role_flags

from . import forms, models


def _admin_staff_required(view):
    """Decorate a view so only administrators and staff reach it.

    Anyone else gets an error message and is redirected to My Hub.
    """
    @wraps(view)
    def _wrapped(request, *args, **kwargs):
        """Run the view for admin/staff; redirect everyone else."""
        if not role_flags(request).get('is_admin_staff'):
            messages.error(request, "You don't have permission to manage the academic structure — ask an administrator.")
            return redirect('myhub:index')
        return view(request, *args, **kwargs)
    return _wrapped


def _save(request, form, *, redirect_to, label, template, redirect_pk=None, extra=None):
    """Render/handle a bound-or-unbound form; on valid POST, save + redirect
    (to ``redirect_to``, passing ``pk=redirect_pk`` when the target needs one)."""
    if request.method == 'POST' and form.is_valid():
        form.save()
        messages.success(request, f'{label} saved.')
        return redirect(redirect_to, pk=redirect_pk) if redirect_pk is not None else redirect(redirect_to)
    if request.method == 'POST':
        messages.error(request, 'Please correct the errors highlighted below.')
    context = {'form': form, 'page_title': label}
    if extra:
        context.update(extra)
    return render(request, template, context)


@login_required
@_admin_staff_required
def institutions(request):
    """List every institution with its programmes — the "Academic structure" home. Admin
    and staff only.
    """
    items = (models.Institution.objects.all()
             .prefetch_related('programmes').order_by('order', 'name'))
    return render(request, 'learning/manage/institutions.html', {
        'page_title': 'Academic structure', 'institutions': items,
    })


@login_required
@_admin_staff_required
def institution_detail(request, pk):
    """Show one institution with its programmes, their modules and cohorts. Admin and
    staff only.
    """
    institution = get_object_or_404(models.Institution, pk=pk)
    programmes = institution.programmes.prefetch_related('modules', 'cohorts').order_by('order', 'name')
    return render(request, 'learning/manage/institution_detail.html', {
        'page_title': institution.display_name, 'institution': institution, 'programmes': programmes,
    })


@login_required
@_admin_staff_required
def institution_add(request):
    """Create an institution and, in the same submit, the structure it needs.

    :class:`~apps.learning.forms.InstitutionBlueprintForm` builds the chosen
    blueprint's programmes, priced modules and the year's calendar. The blueprint
    fields are rendered in their own panel rather than with the institution fields.
    """
    form = forms.InstitutionBlueprintForm(request.POST or None, request.FILES or None)
    if request.method == 'POST' and form.is_valid():
        institution = form.save()
        built = getattr(form, 'blueprint_result', {})
        programmes, offerings = built.get('programmes', []), built.get('offerings', [])
        if programmes:
            messages.success(
                request,
                f'{institution.name} created with {len(programmes)} programme'
                f'{"s" if len(programmes) != 1 else ""} '
                f'({", ".join(p.short_code for p in programmes)}), {len(offerings)} module'
                f'{"s" if len(offerings) != 1 else ""}'
                + (f' and {built["events"]} calendar date(s).' if built.get('events')
                   else '.'))
        else:
            messages.success(request, f'{institution.name} created. Add its programmes below.')
        return redirect('learning:manage-institution', pk=institution.pk)
    if request.method == 'POST':
        messages.error(request, 'Please correct the errors highlighted below.')
    return render(request, 'learning/manage/institution_add.html', {
        'page_title': 'Add institution', 'form': form,
        'form_title': 'Add institution', 'blueprints': form.blueprints,
        'blueprint_fields': ['blueprint', 'price_per_month', 'create_calendar'],
        'cancel_url': 'learning:manage-institutions',
    })


@login_required
@_admin_staff_required
def institution_edit(request, pk):
    """Edit an institution's details. Admin and staff only."""
    institution = get_object_or_404(models.Institution, pk=pk)
    form = forms.InstitutionForm(request.POST or None, request.FILES or None, instance=institution)
    return _save(request, form, redirect_to='learning:manage-institution', label='Institution',
                 template='learning/manage/form.html', redirect_pk=institution.pk,
                 extra={'form_title': f'Edit {institution.display_name}',
                        'cancel_url': 'learning:manage-institution', 'cancel_pk': institution.pk})


@login_required
@_admin_staff_required
@require_POST
def institution_delete(request, pk):
    """Delete an institution (POST only). Admin and staff only."""
    institution = get_object_or_404(models.Institution, pk=pk)
    institution.delete()
    messages.success(request, 'Institution deleted.')
    return redirect('learning:manage-institutions')


@login_required
@_admin_staff_required
def programme_detail(request, pk):
    """Show one programme with its module offerings and cohorts. Admin and staff only.
    """
    programme = get_object_or_404(
        models.Programme.objects.select_related('institution'), pk=pk)
    modules = programme.modules.select_related('module').order_by('order', 'code')
    cohorts = programme.cohorts.order_by('-start_date', 'code')
    return render(request, 'learning/manage/programme_detail.html', {
        'page_title': programme.full_code, 'programme': programme,
        'modules': modules, 'cohorts': cohorts,
    })


@login_required
@_admin_staff_required
def programme_add(request, institution_pk):
    """Add a programme to an institution. Admin and staff only."""
    institution = get_object_or_404(models.Institution, pk=institution_pk)
    form = forms.ProgrammeForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        programme = form.save(commit=False)
        programme.institution = institution
        programme.save()
        messages.success(request, 'Programme saved.')
        return redirect('learning:manage-institution', pk=institution.pk)
    if request.method == 'POST':
        messages.error(request, 'Please correct the errors highlighted below.')
    return render(request, 'learning/manage/form.html', {
        'form': form, 'form_title': f'Add programme to {institution.display_name}',
        'cancel_url': 'learning:manage-institution', 'cancel_pk': institution.pk,
    })


@login_required
@_admin_staff_required
def programme_edit(request, pk):
    """Edit a programme's details. Admin and staff only."""
    programme = get_object_or_404(models.Programme, pk=pk)
    form = forms.ProgrammeForm(request.POST or None, instance=programme)
    return _save(request, form, redirect_to='learning:manage-programme', label='Programme',
                 template='learning/manage/form.html', redirect_pk=programme.pk,
                 extra={'form_title': f'Edit {programme.full_code}',
                        'cancel_url': 'learning:manage-programme', 'cancel_pk': programme.pk})


@login_required
@_admin_staff_required
@require_POST
def programme_delete(request, pk):
    """Delete a programme (POST only), then return to its institution. Admin and staff
    only.
    """
    programme = get_object_or_404(models.Programme, pk=pk)
    institution_pk = programme.institution_id
    programme.delete()
    messages.success(request, 'Programme deleted.')
    return redirect('learning:manage-institution', pk=institution_pk)


@login_required
@_admin_staff_required
def module_add(request, programme_pk):
    """Add a module to a programme.

    :class:`~apps.learning.forms.ProgrammeModuleAddForm` get-or-creates the canonical
    module behind the offering. Admin and staff only.
    """
    programme = get_object_or_404(models.Programme, pk=programme_pk)
    form = forms.ProgrammeModuleAddForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        form.save(programme)
        messages.success(request, 'Module added.')
        return redirect('learning:manage-programme', pk=programme.pk)
    if request.method == 'POST':
        messages.error(request, 'Please correct the errors highlighted below.')
    return render(request, 'learning/manage/form.html', {
        'form': form, 'form_title': f'Add module to {programme.full_code}',
        'cancel_url': 'learning:manage-programme', 'cancel_pk': programme.pk,
    })


@login_required
@_admin_staff_required
def module_edit(request, pk):
    """Edit a module offering. Admin and staff only."""
    module = get_object_or_404(models.ProgrammeModule.objects.select_related('programme'), pk=pk)
    form = forms.ProgrammeModuleForm(request.POST or None, instance=module)
    return _save(request, form, redirect_to='learning:manage-programme', label='Module',
                 template='learning/manage/form.html', redirect_pk=module.programme_id,
                 extra={'form_title': f'Edit {module.code}',
                        'cancel_url': 'learning:manage-programme', 'cancel_pk': module.programme_id})


@login_required
@_admin_staff_required
@require_POST
def module_delete(request, pk):
    """Delete a module offering (POST only), then return to its programme. Admin and
    staff only.
    """
    module = get_object_or_404(models.ProgrammeModule, pk=pk)
    programme_pk = module.programme_id
    module.delete()
    messages.success(request, 'Module deleted.')
    return redirect('learning:manage-programme', pk=programme_pk)


@login_required
@_admin_staff_required
def cohort_add(request, programme_pk):
    """Add a cohort (intake) to a programme. Admin and staff only."""
    programme = get_object_or_404(models.Programme, pk=programme_pk)
    form = forms.CohortForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        cohort = form.save(commit=False)
        cohort.programme = programme
        cohort.save()
        messages.success(request, 'Cohort saved.')
        return redirect('learning:manage-programme', pk=programme.pk)
    if request.method == 'POST':
        messages.error(request, 'Please correct the errors highlighted below.')
    return render(request, 'learning/manage/form.html', {
        'form': form, 'form_title': f'Add cohort to {programme.full_code}',
        'cancel_url': 'learning:manage-programme', 'cancel_pk': programme.pk,
    })


@login_required
@_admin_staff_required
def cohort_edit(request, pk):
    """Edit a cohort. Admin and staff only."""
    cohort = get_object_or_404(models.Cohort.objects.select_related('programme'), pk=pk)
    form = forms.CohortForm(request.POST or None, instance=cohort)
    return _save(request, form, redirect_to='learning:manage-programme', label='Cohort',
                 template='learning/manage/form.html', redirect_pk=cohort.programme_id,
                 extra={'form_title': f'Edit cohort {cohort.code}',
                        'cancel_url': 'learning:manage-programme', 'cancel_pk': cohort.programme_id})


@login_required
@_admin_staff_required
@require_POST
def cohort_delete(request, pk):
    """Delete a cohort (POST only), then return to its programme. Admin and staff only.
    """
    cohort = get_object_or_404(models.Cohort, pk=pk)
    programme_pk = cohort.programme_id
    cohort.delete()
    messages.success(request, 'Cohort deleted.')
    return redirect('learning:manage-programme', pk=programme_pk)
