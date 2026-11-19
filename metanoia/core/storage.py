"""File-server storage selection.

Two tiers of storage:

* **default** — the app server's local disk. Holds low-churn identity images:
  user + course profile pictures, lesson cover/author images. Nothing here.
* **files** — the "file server" for everything users exchange and the system
  generates: chat / discussion / feed attachments, workspace files, lesson
  material (documents/media), reports, summaries, certificates. Configured by
  ``settings.STORAGES['files']`` — a local folder for now, swappable to S3 /
  Azure Blob / any django-storages backend purely via ``.env`` (see
  ``FILE_SERVER_BACKEND``), with **no model migration** because fields reference
  the callable below, not a concrete backend.

Use it on a model field like::

    file = models.FileField(upload_to=files_upload_to('chat'), storage=files_storage)
"""

from django.core.files.storage import storages
from django.utils.deconstruct import deconstructible
from django.utils import timezone


def files_storage():
    """The configured 'files' storage backend (see module docstring)."""
    return storages['files']


def files_upload_to(category):
    """Return an ``upload_to`` callable that files things under a tidy, dated,
    category-namespaced path: ``<category>/<YYYY>/<MM>/<filename>``.

    Kept structured so the file server stays browsable/organised whatever the
    backend (local folder, S3 prefix, Azure container path).
    """
    def _path(instance, filename):
        now = timezone.now()
        return f'{category}/{now:%Y/%m}/{filename}'
    _path.__name__ = f'files_upload_to_{category}'
    return _path


@deconstructible
class UnguessableUploadTo:
    """``upload_to`` for course content: ``<prefix>[/YYYY/MM]/<random>/<filename>``.

    Course files keep their original names (an author's ``FREP Mock 2
    Memo.pdf``), so before this a URL could be guessed from a title. The random
    directory (96 bits) makes a new upload's URL unguessable; the path still
    starts with ``prefix`` so :mod:`core.media_access` gates it as course
    content, and the filename stays readable for the download. Files already on
    disk keep their stored names. This is defence in depth — the access check in
    :mod:`apps.learning.content_files` is what actually refuses a request.
    """

    def __init__(self, prefix, dated=False):
        self.prefix = prefix.rstrip('/')
        self.dated = dated

    def __call__(self, instance, filename):
        import posixpath
        import secrets

        parts = [self.prefix]
        if self.dated:
            parts.append(timezone.now().strftime('%Y/%m'))
        parts.append(secrets.token_urlsafe(12))
        parts.append(posixpath.basename(filename.replace('\\', '/')) or 'file')
        return '/'.join(parts)

    def __eq__(self, other):
        return (isinstance(other, UnguessableUploadTo)
                and (self.prefix, self.dated) == (other.prefix, other.dated))
