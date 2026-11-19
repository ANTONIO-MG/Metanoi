"""HTML routes for the accounts community area (mounted under the ``accounts``
namespace — see :mod:`config.urls`).

* **Registration wizard** — ``register`` (step 1 · personal), ``register-course``
  (step 2 · course/products), ``register-review`` (step 3 · review → pay), the
  one-on-one request, the completion hand-off and the enrolment checkout.
* **Invite-based single-page registration** — ``accept-invite`` (the role is
  decided by the invite link), the parent, educator and staff pages
  (``register-staff`` is the team's first-login profile), and the WhatsApp
  number verification behind the code box beside every phone field.
* **Team** — ``staff-create`` (a superuser creates team accounts) and
  ``add-educator``.
* **Metanoia Community → student** — verify the e-mail, then choose a new password.
* **Parents** — public registration via a student's invite token, and picking
  which linked child to follow.
* **Profiles and settings** — profiles, person cards and settings; POPIA
  download-my-data (right of access) and cookie-banner consent; UI preferences
  set from the chrome (navbar theme buttons, language switcher).
* **Close account** — verify → soft close → 30-day grace → purge command.
* **Recovery desk** (admin/staff) — reactivate during grace, restore after purge;
  ``undo`` reverses a recorded payment, a delete or an account deletion
  (confirmation and reason required).
* **Members** — member management, roles, developer access and enrolment.
* **Super student** — admins/staff/educators switch into a student's view.
  These routes are never swapped by ``core.student_view.StudentViewMiddleware``
  (see its ``EXEMPT_PREFIXES``).
"""

from django.urls import path

from . import community_views
from . import parent_views
from . import person_card
from . import student_view
from . import views

app_name = 'accounts'

urlpatterns = [
    path('register/', views.register, name='register'),
    path('register/course/', views.register_course, name='register-course'),
    path('register/review/', views.register_review, name='register-review'),
    path('register/one-on-one/', views.register_one_on_one, name='register-one-on-one'),
    path('register/complete/', views.registration_complete, name='register-complete'),
    path('register/checkout/', views.enrol_checkout, name='enrol-checkout'),
    path('invite/<uuid:token>/', views.accept_invite, name='accept-invite'),
    path('terms/accept/', views.accept_terms, name='accept-terms'),
    path('register/parent/', views.register_parent, name='register-parent'),
    path('register/educator/', views.register_educator, name='register-educator'),
    path('register/staff/', views.register_staff, name='register-staff'),
    path('register/phone/send-code/', views.phone_send_code, name='phone-send-code'),
    path('register/phone/check-code/', views.phone_check_code, name='phone-check-code'),
    path('staff/create/', views.staff_create, name='staff-create'),
    path('educators/add/', views.add_educator, name='add-educator'),
    path('complete-profile/', views.complete_profile, name='complete-profile'),
    path('become-student/', community_views.become_student, name='become-student'),
    path('become-student/password/', community_views.become_student_password,
         name='become-student-password'),
    path('parents/<uuid:token>/', parent_views.parent_register, name='parent-register'),
    path('my-children/select/', parent_views.select_child, name='parent-select-child'),

    path('my-programmes/', views.my_programmes, name='my-programmes'),

    path('profile/', views.my_profile, name='my-profile'),
    path('profile/<int:pk>/', views.profile, name='profile'),
    path('people/<int:pk>/card/', person_card.person_card, name='person-card'),
    path('people/user/<int:user_id>/card/', person_card.person_card_for_user, name='person-card-user'),
    path('profile/<int:pk>/<slug:tab>/', views.profile, name='profile'),
    path('settings/', views.settings, name='settings'),
    path('privacy/export/', views.data_export, name='data-export'),
    path('privacy/export/pdf/', views.data_export_pdf, name='data-export-pdf'),
    path('privacy/cookies/', views.cookie_consent, name='cookie-consent'),
    path('preferences/theme/', views.set_theme, name='set-theme'),
    path('preferences/language/', views.set_language, name='set-language'),

    path('settings/close/', views.close_account, name='close-account'),
    path('settings/close/done/', views.close_account_done, name='close-account-done'),

    path('closed-accounts/', views.closed_accounts, name='closed-accounts'),
    path('closed-accounts/<int:pk>/reactivate/', views.reactivate_account, name='reactivate-account'),
    path('closed-accounts/archive/<int:pk>/restore/', views.restore_archive, name='restore-archive'),
    path('undo/<int:pk>/', views.undo_action, name='undo'),

    path('members/', views.manage_members, name='manage-members'),
    path('members/<int:pk>/', views.member_profile, name='member-profile'),
    path('members/<int:pk>/role/', views.member_role, name='member-role'),
    path('members/<int:pk>/developer/', views.member_developer, name='member-developer'),
    path('members/<int:pk>/enrol/', views.member_enrol, name='member-enrol'),

    path('student-view/', student_view.picker, name='student-view'),
    path('student-view/start/', student_view.start, name='student-view-start'),
    path('student-view/stop/', student_view.stop, name='student-view-stop'),
]
