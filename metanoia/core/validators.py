"""Shared upload validators.

Two problems an unrestricted upload field has:

* **Size.** Nothing stopped a multi-gigabyte upload filling the disk.
* **Type.** An uploaded ``.svg`` or ``.html`` is served from the media domain and
  runs as script *in this site's origin* — it can read the session cookie and
  act as the signed-in user. ``.svg`` is the awkward one: it is a legitimate
  image format and an executable document at the same time, so it is allowed
  where images are, and the media response headers
  (:mod:`apps.accounts.middleware`) stop it executing.

The allow-lists are deliberately narrow — an extension not listed is one nobody
has asked for yet.

Validators must be module-level named callables (not lambdas or locals), or
``makemigrations`` cannot serialise them into a migration.

Extension lists and per-field validators
----------------------------------------
``ATTACHMENT_EXTENSIONS`` is the union of the image, document, media and
archive lists: a learner submitting an assignment or a colleague sharing a
workspace file should not have to care which bucket a file falls in.

Sizes are per field rather than global: a profile picture has no business
being 25 MB, and a lecture recording legitimately is.

* ``validate_image`` (5 MB), ``validate_avatar`` (2 MB), ``validate_document``
  and ``validate_attachment`` (25 MB), ``validate_media`` (100 MB),
  ``validate_package`` (zip, 200 MB).
* ``validate_broadcast`` — sent with a broadcast notification: a poster, a
  short clip or the document itself (100 MB).
* ``validate_download`` — a shop download: the blueprint PDF, the spreadsheet,
  or the zip of past papers (200 MB).
* ``validate_lesson_media`` — a lesson block's own upload: an image or diagram,
  a recording, or the file a "File / download" block hands out, so documents
  as well as media (100 MB).
* ``validate_mail_file`` / ``validate_whatsapp_media`` — e-mail and WhatsApp
  carry whatever the other side sent (``.eml``, ``.ics``, ``.html`` …), so these
  are bounded by size only, never by extension. They are written by the system
  rather than through a form; the bound guards any form added later.

``NEVER_INLINE_EXTENSIONS`` are the extensions that must never be rendered
inline by the browser, whatever the stored content type says: serving one of
them from the media domain with ``Content-Disposition: inline`` runs it in this
site's origin (see :mod:`core.media_headers`).
"""

from django.core.exceptions import ValidationError
from django.core.validators import FileExtensionValidator
from django.template.defaultfilters import filesizeformat
from django.utils.deconstruct import deconstructible

MB = 1024 * 1024

IMAGE_EXTENSIONS = ['jpg', 'jpeg', 'png', 'gif', 'webp', 'svg', 'avif']
DOCUMENT_EXTENSIONS = [
    'pdf', 'doc', 'docx', 'xls', 'xlsx', 'ppt', 'pptx',
    'txt', 'csv', 'rtf', 'odt', 'ods', 'odp',
]
MEDIA_EXTENSIONS = ['mp3', 'wav', 'm4a', 'ogg', 'mp4', 'webm', 'mov', 'm4v']
ARCHIVE_EXTENSIONS = ['zip']

ATTACHMENT_EXTENSIONS = sorted(
    set(IMAGE_EXTENSIONS + DOCUMENT_EXTENSIONS + MEDIA_EXTENSIONS + ARCHIVE_EXTENSIONS))


@deconstructible
class MaxFileSize:
    """Reject an upload larger than ``max_mb`` megabytes."""

    def __init__(self, max_mb):
        """Store the limit in megabytes."""
        self.max_mb = max_mb

    def __call__(self, value):
        """Raise ``ValidationError`` (code ``file_too_large``) when ``value`` exceeds the limit."""
        size = getattr(value, 'size', None)
        if size is not None and size > self.max_mb * MB:
            raise ValidationError(
                'That file is %(size)s. The limit is %(limit)s.',
                code='file_too_large',
                params={'size': filesizeformat(size),
                        'limit': filesizeformat(self.max_mb * MB)},
            )

    def __eq__(self, other):
        """Equal limits compare equal, so ``makemigrations`` does not detect a change forever."""
        return isinstance(other, MaxFileSize) and other.max_mb == self.max_mb

    def __hash__(self):
        """Hash consistent with :meth:`__eq__`."""
        return hash(('MaxFileSize', self.max_mb))


validate_image = [FileExtensionValidator(IMAGE_EXTENSIONS), MaxFileSize(5)]
validate_avatar = [FileExtensionValidator(IMAGE_EXTENSIONS), MaxFileSize(2)]
validate_document = [FileExtensionValidator(DOCUMENT_EXTENSIONS), MaxFileSize(25)]
validate_attachment = [FileExtensionValidator(ATTACHMENT_EXTENSIONS), MaxFileSize(25)]
validate_media = [FileExtensionValidator(IMAGE_EXTENSIONS + MEDIA_EXTENSIONS),
                  MaxFileSize(100)]
validate_package = [FileExtensionValidator(ARCHIVE_EXTENSIONS), MaxFileSize(200)]
validate_broadcast = [FileExtensionValidator(ATTACHMENT_EXTENSIONS), MaxFileSize(100)]
validate_download = [FileExtensionValidator(DOCUMENT_EXTENSIONS + ARCHIVE_EXTENSIONS),
                     MaxFileSize(200)]
validate_lesson_media = [FileExtensionValidator(ATTACHMENT_EXTENSIONS), MaxFileSize(100)]
validate_mail_file = [MaxFileSize(50)]
validate_whatsapp_media = [MaxFileSize(100)]


def upload_errors(upload, validators):
    """Run ``validators`` over ``upload``; return a list of messages (empty = OK).

    Model-field ``validators`` only run through a ModelForm or ``full_clean()``.
    A view that assigns ``request.FILES[...]`` straight onto a model and calls
    ``save()``/``create()`` skips them entirely — so every such view must call
    this (or :func:`check_upload`) first.
    """
    errors = []
    for validator in validators:
        try:
            validator(upload)
        except ValidationError as exc:
            errors.extend(exc.messages)
    return errors


def check_upload(upload, validators):
    """Raise :class:`ValidationError` (prefixed with the file name) if ``upload``
    fails ``validators``."""
    errors = upload_errors(upload, validators)
    if errors:
        raise ValidationError(f'{getattr(upload, "name", "file")}: {" ".join(errors)}')


NEVER_INLINE_EXTENSIONS = frozenset({
    'svg', 'html', 'htm', 'xhtml', 'xml', 'js', 'mjs', 'css', 'swf', 'xht',
})


def must_download(name):
    """True when ``name`` must be served as an attachment rather than inline."""
    _, _, ext = (name or '').rpartition('.')
    return ext.lower() in NEVER_INLINE_EXTENSIONS
