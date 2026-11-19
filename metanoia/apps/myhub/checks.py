"""
Startup guard against template syntax errors.

With the cached loader a malformed tag only surfaces as a production 500 on
first render. This system check compiles every first-party template up front so
the mistake fails ``manage.py check`` (and ``runserver`` boot) instead.

Only templates that physically live under ``BASE_DIR`` are checked. Third-party
templates in site-packages are skipped: some legitimately require optional
packages we don't install (e.g. ``django_filters``'s crispy template needs
``crispy_forms``) and would be false positives.

The module also checks the SMTP TLS settings and the integrity of the bundled
icon fonts. Fonts once shipped mojibake-corrupted (binaries re-written as
UTF-8, bytes → U+FFFD), which silently renders every icon as an empty box while
the file still serves 200; :func:`check_icon_fonts_intact` compares each font's
self-declared length with the real file size, the invariant that corruption
breaks.
"""
import re
import sys
from pathlib import Path

from django.apps import apps
from django.conf import settings
from django.core.checks import Error, Tags, Warning as CheckWarning, register


def _first_party_template_names():
    """Map every under-BASE_DIR template to its file, honouring loader order.

    First-party means under ``BASE_DIR`` *and* outside the virtualenv and
    site-packages: the venv often sits inside ``BASE_DIR`` (``.venv/``), and
    its third-party templates may load tags from packages we don't install
    (false positives). When two roots hold the same name the first one wins,
    mirroring the loader's lookup.
    """
    base = Path(settings.BASE_DIR).resolve()

    roots = []
    for cfg in settings.TEMPLATES:
        roots += [Path(d) for d in cfg.get("DIRS", [])]
    for app in apps.get_app_configs():
        app_templates = Path(app.path) / "templates"
        if app_templates.is_dir():
            roots.append(app_templates)

    venv = Path(sys.prefix).resolve()

    def _is_first_party(root):
        """Whether template directory ``root`` is ours (see above)."""
        rp = root.resolve()
        if not rp.is_relative_to(base):
            return False
        if rp.is_relative_to(venv):
            return False
        return "site-packages" not in rp.parts

    roots = [r for r in roots if _is_first_party(r)]

    names = {}
    for root in roots:
        for f in sorted(root.rglob("*.html")):
            name = f.relative_to(root).as_posix()
            names.setdefault(name, f)
    return names


@register(Tags.templates)
def check_templates_compile(app_configs, **kwargs):
    """Fail the check run if any first-party template won't compile.

    Other exceptions are ignored: missing included/extended files resolve at
    render time, not compile time, and only genuine *syntax* errors should
    fail this guard, so it stays trustworthy and noise-free.
    """
    from django.template import TemplateSyntaxError
    from django.template.loader import get_template

    errors = []
    for name, path in _first_party_template_names().items():
        try:
            get_template(name)
        except TemplateSyntaxError as exc:
            errors.append(
                Error(
                    f"Template '{name}' has a syntax error: {exc}",
                    hint=str(path),
                    id="templates.E001",
                )
            )
        except Exception:
            pass
    return errors


@register(Tags.templates)
def check_template_comments(app_configs, **kwargs):
    """Flag multi-line ``{# #}`` comments, which Django prints as literal text.

    Django's ``{# #}`` comment tag cannot span lines (its tokenizer isn't
    DOTALL), so a comment that opens on one line and closes on another is not
    recognised as a comment and is rendered verbatim into the page — leaking
    developer notes to users. ``{% comment %}…{% endcomment %}`` is the
    multi-line form and is stripped correctly.
    """
    errors = []
    for name, path in _first_party_template_names().items():
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for match in re.finditer(r"\{#", text):
            start = match.start()
            newline = text.find("\n", start)
            segment = text[start : newline if newline != -1 else len(text)]
            if "#}" not in segment:
                line_no = text.count("\n", 0, start) + 1
                errors.append(
                    Error(
                        f"Template '{name}' has a multi-line '{{# #}}' comment "
                        f"that Django renders as literal text — use "
                        f"'{{% comment %}}…{{% endcomment %}}' instead.",
                        hint=f"{path}:{line_no}",
                        id="templates.E002",
                    )
                )
    return errors


@register()
def check_email_tls_config(app_configs, **kwargs):
    """Catch the SMTP TLS/SSL/port mismatch that breaks outgoing e-mail.

    Gmail-style submission ports only advertise the AUTH extension over an
    encrypted channel: 587 needs STARTTLS (``EMAIL_USE_TLS=true``), 465 needs
    implicit TLS (``EMAIL_USE_SSL=true``). With neither, ``smtplib.login()``
    raises "SMTP AUTH extension not supported by server" the first time the app
    sends mail — e.g. the sign-up verification e-mail, which 500s registration.
    Console, file and locmem backends never authenticate and are not checked.
    """
    if "smtp" not in getattr(settings, "EMAIL_BACKEND", ""):
        return []

    tls = bool(getattr(settings, "EMAIL_USE_TLS", False))
    ssl = bool(getattr(settings, "EMAIL_USE_SSL", False))
    port = getattr(settings, "EMAIL_PORT", None)

    if tls and ssl:
        return [
            Error(
                "EMAIL_USE_TLS and EMAIL_USE_SSL are both true — Django allows "
                "only one (TLS for port 587, SSL for port 465).",
                id="email.E001",
            )
        ]
    if port == 587 and not tls and not ssl:
        return [
            CheckWarning(
                "EMAIL_PORT=587 but EMAIL_USE_TLS is false — port 587 needs "
                "STARTTLS, or the server won't offer AUTH and every send raises "
                "SMTPNotSupportedError. Set EMAIL_USE_TLS=true.",
                id="email.W001",
            )
        ]
    if port == 465 and not tls and not ssl:
        return [
            CheckWarning(
                "EMAIL_PORT=465 but EMAIL_USE_SSL is false — port 465 needs "
                "implicit TLS. Set EMAIL_USE_SSL=true.",
                id="email.W002",
            )
        ]
    return []


import struct

_FONT_SUFFIXES = {".woff2", ".woff", ".ttf", ".otf", ".eot"}


def _font_defect(path):
    """Return a human-readable defect for a font file, or None when it looks sane.

    An unreadable file is a defect too. WOFF/WOFF2 headers hold the signature,
    the flavour and then a uint32 of the TOTAL file size; in an sfnt (TTF/OTF)
    file every entry in the table directory must lie inside the file; an EOT's
    magic number lives at offset 34 and its first uint32 is the file size.
    """
    try:
        data = path.read_bytes()
    except OSError as exc:
        return f"could not be read ({exc})"
    size = len(data)
    if size < 16:
        return f"is only {size} bytes (truncated or empty)"

    signature = data[:4]
    if signature in (b"wOF2", b"wOFF"):
        declared = struct.unpack(">I", data[8:12])[0]
        if declared != size:
            return (f"declares {declared} bytes but is {size} on disk — the binary has "
                    f"been corrupted (most likely re-saved as UTF-8 text)")
        return None

    if signature in (b"\x00\x01\x00\x00", b"true", b"OTTO"):
        num_tables = struct.unpack(">H", data[4:6])[0]
        directory_end = 12 + num_tables * 16
        if not 0 < num_tables < 512 or directory_end > size:
            return f"has an implausible sfnt table directory ({num_tables} tables)"
        for index in range(num_tables):
            entry = 12 + index * 16
            offset, length = struct.unpack(">II", data[entry + 8:entry + 16])
            if offset + length > size:
                return (f"sfnt table {index} runs past the end of the file — the binary "
                        f"has been corrupted (most likely re-saved as UTF-8 text)")
        return None

    if size > 40 and data[34:36] == b"\x4c\x50":
        declared = struct.unpack("<I", data[0:4])[0]
        if declared != size:
            return f"declares {declared} bytes but is {size} on disk — the binary has been corrupted"
        return None

    return f"does not start with a known font signature (got {signature!r})"


@register(Tags.files)
def check_icon_fonts_intact(app_configs, **kwargs):
    """Fail the boot when a bundled icon font is not a readable font binary."""
    base = Path(settings.BASE_DIR).resolve()
    roots = [Path(d) for d in getattr(settings, "STATICFILES_DIRS", [])]
    for app in apps.get_app_configs():
        app_static = Path(app.path) / "static"
        if app_static.is_dir():
            roots.append(app_static)

    venv = Path(sys.prefix).resolve()
    errors = []
    seen = set()
    for root in roots:
        root = root.resolve()
        if not root.is_relative_to(base) or root.is_relative_to(venv):
            continue
        for path in sorted(root.rglob("*")):
            if path.suffix.lower() not in _FONT_SUFFIXES or not path.is_file():
                continue
            if path in seen:
                continue
            seen.add(path)
            defect = _font_defect(path)
            if defect:
                errors.append(
                    Error(
                        f"Icon font {path.relative_to(base)} {defect}. Icons using it will "
                        f"render as empty boxes. Re-download the file from the vendor "
                        f"(binary mode) rather than copying it through a text editor.",
                        id="fonts.E001",
                    )
                )
    return errors
