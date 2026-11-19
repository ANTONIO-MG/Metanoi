"""URL routes for the learning app (mounted at /learning/ under the ``learning``
namespace).

The routes, by area:

* **Lessons and modules** — the lesson list, study-session timer, My Modules and
  the "unlock a module" flow (:mod:`.views`).
* **Content in** (admin/staff) — ``content/import/`` gets a topic's content in,
  by JSON or from documents (:mod:`.content_import`); ``content/…`` is the
  content queue: upload → draft (template or AI) → review → approve → publish
  (:mod:`.content_uploads`); ``content/programme-pack/…`` takes a whole
  programme pack as a zip → spine, weeks, lessons, sessions, calendar
  (:mod:`.programme_pack_views`). ``modules/<pk>/analytics/`` is the course report.
* **The material door** — ``materials/<pk>/open/`` is the one gated door onto a
  material (:mod:`.material_open`).
* **Module feed** — the schedule and its tabs (:mod:`.module_feed`);
  ``feed/weeks/`` is the Schedule tab's progressive loader (the bodies of a few
  weeks, gated) and must stay above the ``feed/<tab>/`` route.
* **Module builder** — blocks, weeks and materials of a schedule
(:mod:`.module_build`).
* **Academic structure** — ``manage/…`` CRUD for institutions, programmes,
  modules and cohorts (:mod:`.manage`).
* **Lesson player** — viewing, completion, block tracking, per-section progress,
  private notes and bookmarks (:mod:`.views`).
* **Lesson authoring** — the manager, AI import, editor, sections (the
  tear-drops), blocks, lesson attachments (PDFs, images, text files …) and
  linked blocks: quiz (assessment) and live session (meeting) (:mod:`.authoring`,
  :mod:`.ai_import`).
* **Module profile** — ``modules/<pk>/`` and ``modules/<pk>/<tab>/``; the tab
  form catches any slug, so it stays last.
"""

from django.urls import path

from . import (ai_import, authoring, content_import, content_uploads, course_analytics, manage,
               material_open, module_build, module_feed, programme_pack_views, views)

app_name = 'learning'

urlpatterns = [
    path('', views.lessons, name='lessons'),
    path('lessons/<int:lesson_id>/session/', views.session_action, name='session-action'),

    path('modules/', views.my_modules, name='my-modules'),
    path('modules/<int:module_id>/unlock/', views.module_unlock, name='module-unlock'),

    path('content/import/', content_import.content_import, name='content-import'),
    path('modules/<int:pk>/analytics/', course_analytics.course_analytics, name='course-analytics'),
    path('content/', content_uploads.queue, name='content-queue'),
    path('content/upload/', content_uploads.upload, name='content-upload'),
    path('content/create/', content_uploads.create, name='content-create'),
    path('content/format/', content_uploads.format_page, name='content-format'),
    path('content/format/samples.zip', content_uploads.sample_download, name='content-samples'),
    path('content/format/<slug:key>.zip', content_uploads.sample_download, name='content-sample'),
    path('content/programme-pack/', programme_pack_views.upload, name='programme-pack'),
    path('content/programme-pack/<str:token>/', programme_pack_views.review, name='programme-pack-review'),
    path('content/programme-pack/<str:token>/build/', programme_pack_views.build, name='programme-pack-build'),
    path('content/programme-pack/<str:token>/status/', programme_pack_views.build_status,
         name='programme-pack-status'),
    path('content/programme-pack/<str:token>/seed.py', programme_pack_views.seed_file,
         name='programme-pack-seed'),
    path('content/programme-pack/<str:token>/plan.json', programme_pack_views.plan_file,
         name='programme-pack-plan'),
    path('content/programme-pack/<str:token>/sheet/', programme_pack_views.save_sheet,
         name='programme-pack-sheet'),
    path('content/programme-pack/<str:token>/schedule.csv', programme_pack_views.sheet_file,
         name='programme-pack-sheet-file'),
    path('content/<uuid:public_id>/', content_uploads.review, name='content-review'),
    path('content/<uuid:public_id>/action/', content_uploads.action, name='content-action'),
    path('content/<uuid:public_id>/file/<int:file_id>/', content_uploads.file_download, name='content-file'),

    path('materials/<int:pk>/open/', material_open.material_open, name='material-open'),

    path('modules/<int:pk>/feed/', module_feed.module_feed, name='module-feed'),
    path('modules/<int:pk>/feed/weeks/', module_feed.module_weeks, name='module-weeks'),
    path('modules/<int:pk>/feed/<slug:tab>/', module_feed.module_feed, name='module-feed-tab'),

    path('modules/<int:pk>/build/', module_build.module_build, name='module-build'),
    path('modules/<int:pk>/build/scaffold/', module_build.module_scaffold, name='module-scaffold'),
    path('modules/<int:pk>/build/phases/save/', module_build.phase_save, name='module-phase-add'),
    path('modules/<int:pk>/build/phases/<int:phase_id>/save/', module_build.phase_save, name='module-phase-save'),
    path('modules/<int:pk>/build/phases/<int:phase_id>/delete/', module_build.phase_delete, name='module-phase-delete'),
    path('modules/<int:pk>/build/weeks/save/', module_build.week_save, name='module-week-add'),
    path('modules/<int:pk>/build/weeks/<int:week_id>/save/', module_build.week_save, name='module-week-save'),
    path('modules/<int:pk>/build/weeks/<int:week_id>/delete/', module_build.week_delete, name='module-week-delete'),
    path('modules/<int:pk>/build/weeks/<int:week_id>/availability/', module_build.week_availability,
         name='module-week-availability'),
    path('modules/<int:pk>/build/materials/save/', module_build.material_save, name='module-material-add'),
    path('modules/<int:pk>/build/materials/<int:material_id>/save/', module_build.material_save, name='module-material-save'),
    path('modules/<int:pk>/build/materials/<int:material_id>/delete/', module_build.material_delete, name='module-material-delete'),
    path('modules/<int:pk>/build/materials/<int:material_id>/publish/', module_build.material_publish, name='module-material-publish'),
    path('modules/<int:pk>/build/materials/reorder/', module_build.material_reorder, name='module-material-reorder'),

    path('manage/institutions/', manage.institutions, name='manage-institutions'),
    path('manage/institutions/add/', manage.institution_add, name='manage-institution-add'),
    path('manage/institutions/<int:pk>/', manage.institution_detail, name='manage-institution'),
    path('manage/institutions/<int:pk>/edit/', manage.institution_edit, name='manage-institution-edit'),
    path('manage/institutions/<int:pk>/delete/', manage.institution_delete, name='manage-institution-delete'),
    path('manage/institutions/<int:institution_pk>/programmes/add/', manage.programme_add, name='manage-programme-add'),
    path('manage/programmes/<int:pk>/', manage.programme_detail, name='manage-programme'),
    path('manage/programmes/<int:pk>/edit/', manage.programme_edit, name='manage-programme-edit'),
    path('manage/programmes/<int:pk>/delete/', manage.programme_delete, name='manage-programme-delete'),
    path('manage/programmes/<int:programme_pk>/modules/add/', manage.module_add, name='manage-module-add'),
    path('manage/modules/<int:pk>/edit/', manage.module_edit, name='manage-module-edit'),
    path('manage/modules/<int:pk>/delete/', manage.module_delete, name='manage-module-delete'),
    path('manage/programmes/<int:programme_pk>/cohorts/add/', manage.cohort_add, name='manage-cohort-add'),
    path('manage/cohorts/<int:pk>/edit/', manage.cohort_edit, name='manage-cohort-edit'),
    path('manage/cohorts/<int:pk>/delete/', manage.cohort_delete, name='manage-cohort-delete'),

    path('lessons/<int:lesson_id>/view/', views.lesson_view, name='lesson-view'),
    path('lessons/<int:lesson_id>/complete/', views.lesson_complete, name='lesson-complete'),
    path('blocks/<int:block_id>/track/', views.lesson_block_track, name='lesson-block-track'),
    path('sections/<int:section_id>/progress/', views.section_progress, name='section-progress'),
    path('lessons/<int:lesson_id>/note/', views.note_save, name='lesson-note-save'),
    path('lessons/<int:lesson_id>/bookmark/', views.bookmark_add, name='lesson-bookmark-add'),
    path('bookmarks/<int:bookmark_id>/delete/', views.bookmark_delete, name='lesson-bookmark-delete'),

    path('lessons/manage/', authoring.lesson_manage, name='lesson-manage'),
    path('lessons/new/', authoring.lesson_create, name='lesson-create'),
    path('lessons/ai/', ai_import.lesson_ai_import, name='lesson-ai-import'),
    path('lessons/<int:lesson_id>/ai/extend/', ai_import.lesson_ai_extend, name='lesson-ai-extend'),
    path('modules/<int:module_id>/live/schedule/', authoring.module_live_schedule, name='module-live-schedule'),
    path('lessons/<int:lesson_id>/edit/', authoring.lesson_editor, name='lesson-editor'),
    path('lessons/<int:lesson_id>/meta/', authoring.lesson_meta_save, name='lesson-meta-save'),
    path('lessons/<int:lesson_id>/publish/', authoring.lesson_publish, name='lesson-publish'),
    path('lessons/<int:lesson_id>/delete/', authoring.lesson_delete, name='lesson-delete'),
    path('lessons/<int:lesson_id>/sections/add/', authoring.section_add, name='section-add'),
    path('lessons/<int:lesson_id>/sections/reorder/', authoring.section_reorder, name='section-reorder'),
    path('sections/<int:section_id>/save/', authoring.section_save, name='section-save'),
    path('sections/<int:section_id>/delete/', authoring.section_delete, name='section-delete'),
    path('lessons/<int:lesson_id>/blocks/add/', authoring.block_add, name='block-add'),
    path('lessons/<int:lesson_id>/blocks/reorder/', authoring.block_reorder, name='block-reorder'),
    path('blocks/<int:block_id>/save/', authoring.block_save, name='block-save'),
    path('blocks/<int:block_id>/move/', authoring.block_move, name='block-move'),
    path('blocks/<int:block_id>/delete/', authoring.block_delete, name='block-delete'),
    path('blocks/<int:block_id>/upload/', authoring.block_upload, name='block-upload'),
    path('lessons/<int:lesson_id>/attachments/add/', authoring.attachment_add, name='attachment-add'),
    path('attachments/<int:resource_id>/delete/', authoring.attachment_delete, name='attachment-delete'),
    path('lessons/<int:lesson_id>/attach/quiz/', authoring.attach_quiz, name='attach-quiz'),
    path('lessons/<int:lesson_id>/attach/meeting/', authoring.attach_meeting, name='attach-meeting'),

    path('modules/<int:pk>/', views.module_profile, name='module-profile'),
    path('modules/<int:pk>/<slug:tab>/', views.module_profile, name='module-profile'),

]
