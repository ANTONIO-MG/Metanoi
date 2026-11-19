"""Is a material's file whole?

Part of the 2026 material import arrived cut short: the claude.ai project export
it came from stored large files in 256 KiB chunks and only the first chunk (or
two) made it across, so a 1.5 MB past paper landed as a 256 KiB PDF with no end.
The browser then shows "Failed to load PDF document", which reads as a broken
link rather than a broken file.

:func:`file_is_complete` answers from the file's tail — a PDF ends with
``%%EOF``, an Office file (a zip) with its end-of-central-directory record — so
the listing can say "being replaced" and the door can refuse to stream garbage.
The answer is cached per (name, size): a re-upload changes both.

``_TAIL`` is how much of the end is read: 64 KiB, because the zip EOCD record
sits within the last 64 KiB (+22 bytes) of the file.
"""

import logging

logger = logging.getLogger('apps')

_ZIP_TYPES = ('docx', 'xlsx', 'pptx')
_TAIL = 65536
_cache = {}


def file_is_complete(fieldfile):
    """True unless the file is a PDF / Office document that is visibly truncated.

    Other types, and anything we cannot read, get the benefit of the doubt —
    this flags known damage, it does not certify files.
    """
    if not fieldfile:
        return True
    name = fieldfile.name or ''
    ext = name.rsplit('.', 1)[-1].lower() if '.' in name else ''
    if ext != 'pdf' and ext not in _ZIP_TYPES:
        return True
    try:
        size = fieldfile.storage.size(name)
    except Exception:
        return True
    key = (name, size)
    if key in _cache:
        return _cache[key]
    try:
        with fieldfile.storage.open(name, 'rb') as handle:
            handle.seek(max(0, size - _TAIL))
            tail = handle.read()
    except Exception:
        return True
    ok = (b'%%EOF' in tail[-2048:]) if ext == 'pdf' else (b'PK\x05\x06' in tail)
    if not ok:
        logger.warning('material file truncated: %s (%s bytes)', name, size)
    _cache[key] = ok
    return ok
