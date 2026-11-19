"""The platform's own accounts on the ``@metanoiagroup.co.za`` role mailboxes.

Two kinds:

* :data:`STAFF_ADMINS`: the administrator mailbox (admin@). It is a superuser
  with Django admin access. People join the team by signing up and being given
  a role in the hub; no personal account is seeded.
* :data:`SYSTEM_ACCOUNTS`: the sending mailboxes (accounts@, info@,
  learninghub@; see apps/communication/senders.py). These are ``staff`` hub
  accounts, not superusers, and their contact details are the business's own.

Both tables hold ``(email, first_name, last_name, avatar file name)`` tuples.
No photographs ship with the platform: every avatar is drawn on the fly by
:func:`initials_avatar` (the account's initial letters on a solid colour) and
saved under the given file name.

Every account is e-mail-verified and onboarded, so the first sign-in goes
straight to the hub. A picture is attached only when the account has none, so a
picture uploaded later survives.

Used by ``.academic_seed.py`` on a fresh install and by
``manage.py ensure_staff_admins`` on a running one. Both are safe to re-run: an
existing account is repaired (names, flags, verification, onboarding) but its
password is only reset when ``reset_password`` is true. The password is passed
in by the caller and never stored in .env.

Optional details for the administrator account (title, gender, birthday, phone,
WhatsApp, area, Microsoft 365 sign-in) go in :data:`STAFF_PROFILES`. Only the
values filled in there are written; a blank one is skipped, so it never wipes
what someone has since entered on the profile page. Names work the same way.

:data:`STAFF_PROFILES` maps each :data:`STAFF_ADMINS` e-mail to a dict of
details. Fill in what you know and leave the rest as ``""``:

==================================  =============================================
``title``                           mr | mrs | ms | miss | dr | prof
``gender``                          male | female | other
``date_of_birth``                   ``"YYYY-MM-DD"``
``phone``                           any format, e.g. ``"082 123 4567"`` or
                                    ``"+27 82 123 4567"``
``whatsapp``                        the WhatsApp number (counts as verified: you
                                    vouch for it)
``suburb`` / ``city`` / ``province``  area (South Africa)
``primary_device``                  laptop | desktop | tablet | phone
``ms_upn``                          the Microsoft 365 / Teams sign-in, only if
                                    it is not the @metanoiagroup.co.za address
==================================  =============================================

:data:`AVATAR_COLOUR` is the background of the generated avatars and
:data:`AVATAR_SIZE` their width and height in pixels.
"""

import io

AVATAR_COLOUR = "#3E2415"
AVATAR_SIZE = 512

STAFF_ADMINS = [
    ("admin@metanoiagroup.co.za", "Metanoia", "Admin", "admin.png"),
]

_BLANK_PROFILE = dict(title="", gender="", date_of_birth="", phone="", whatsapp="",
                      suburb="", city="", province="", ms_upn="", primary_device="")
STAFF_PROFILES = {
    "admin@metanoiagroup.co.za": {**_BLANK_PROFILE},
}

def is_platform_developer(user):
    """Kept for older callers: a developer account (core.roles.is_developer)."""
    from core.roles import is_developer
    return is_developer(user)


SYSTEM_ACCOUNTS = [
    ("accounts@metanoiagroup.co.za",    "Metanoia", "Accounts",     "accounts.png"),
    ("info@metanoiagroup.co.za",        "Metanoia", "Info",         "info.png"),
    ("learninghub@metanoiagroup.co.za", "Metanoia", "Learning Hub", "learninghub.png"),
]


def initials(first, last):
    """The one or two capital letters an avatar shows: ``("Metanoia", "Learning Hub")`` gives ``"LH"``.

    The surname carries the letters when it has two words (the role mailboxes
    all start with "Metanoia", so their first name tells them apart from
    nothing); otherwise the first letters of first name and surname are used.
    """
    words = (last or "").split()
    if len(words) >= 2:
        letters = words[0][0] + words[1][0]
    else:
        letters = (first or "")[:1] + (last or "")[:1]
    return (letters or "M").upper()


def _avatar_font(size):
    """A bold TrueType font at ``size`` points, or Pillow's built-in font when none is installed."""
    from PIL import ImageFont

    for path in ("/System/Library/Fonts/Supplemental/Arial Bold.ttf",
                 "/System/Library/Fonts/Helvetica.ttc",
                 "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def initials_avatar(letters, *, colour=AVATAR_COLOUR, size=AVATAR_SIZE):
    """A square PNG (bytes) of ``letters`` in white, centred on a ``colour`` background."""
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (size, size), colour)
    draw = ImageDraw.Draw(image)
    font = _avatar_font(int(size * 0.42))
    box = draw.textbbox((0, 0), letters, font=font)
    draw.text(((size - box[2] - box[0]) / 2, (size - box[3] - box[1]) / 2),
              letters, fill="#FFFFFF", font=font)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _business_contact():
    """Metanoia's own phone and address, from the branding catalogue (.strings.json).

    The second address line is split on its comma, e.g. ``"ROODEPOORT, Gauteng
    2169"`` gives city ``Roodepoort`` and province ``Gauteng``.
    """
    from core.branding import strings
    contact = (strings().get("brand") or {}).get("contact") or {}
    lines = contact.get("address_lines") or []
    city_line = lines[1] if len(lines) > 1 else ""
    city, _, rest = city_line.partition(",")
    return {
        "phone": contact.get("phone", ""),
        "suburb": lines[0] if lines else "",
        "city": city.strip().title(),
        "province": rest.strip().split(" ")[0] if rest.strip() else "",
    }


def _attach_avatar(person, filename, *, replace=False):
    """Attach a generated initials avatar, saved as ``filename``, as the person's picture.

    The letters come from the person's names (:func:`initials`). An existing
    picture is kept unless ``replace`` is true. Returns whether a picture was
    saved.
    """
    if person.profile_picture and str(person.profile_picture) and not replace:
        return False
    from django.core.files.base import ContentFile
    png = initials_avatar(initials(person.first_name, person.last_name))
    person.profile_picture.save(filename, ContentFile(png), save=True)
    return True


def _apply_profile(person, details):
    """Write the filled-in :data:`STAFF_PROFILES` values; returns (set, still_blank).

    Raises ``ValueError`` naming the field when a value is not one the profile
    accepts, so a typo in the table stops the seed instead of saving rubbish. A
    new WhatsApp number is marked verified at once: the owner vouches for it,
    which stands in for the code check.
    """
    from datetime import date

    from django.core.exceptions import ValidationError
    from django.utils import timezone

    from apps.accounts.forms import normalise_phone
    from apps.accounts.models import Person, PersonContact

    email = person.user.email
    given = {k: (v or "").strip() for k, v in details.items() if (v or "").strip()}
    blank = sorted(k for k in details if k not in given)

    for name in ("title", "gender", "primary_device"):
        if name in given:
            value = given[name].lower()
            allowed = [c[0] for c in Person._meta.get_field(name).choices]
            if value not in allowed:
                raise ValueError(f"{email}: {name} {given[name]!r} is not one of {', '.join(allowed)}")
            setattr(person, name, value)
    try:
        if "date_of_birth" in given:
            person.date_of_birth = date.fromisoformat(given["date_of_birth"])
        if "phone" in given:
            person.phone = normalise_phone(given["phone"], "ZA")
        if "whatsapp" in given:
            digits = normalise_phone(given["whatsapp"], "ZA").lstrip("+").replace(" ", "")
            if person.whatsapp_number != digits:
                person.whatsapp_number, person.whatsapp_verified_at = digits, timezone.now()
    except (ValueError, ValidationError) as exc:
        msg = exc.messages[0] if isinstance(exc, ValidationError) else str(exc)
        raise ValueError(f"{email}: {msg} (STAFF_PROFILES)") from exc
    if "ms_upn" in given:
        person.ms_upn = given["ms_upn"].lower()
    person.save()

    area = {k: given[k] for k in ("suburb", "city", "province") if k in given}
    if "phone" in given:
        area["primary_phone"] = person.phone
    if area:
        PersonContact.objects.update_or_create(person=person, defaults={"phone_country": "ZA", **area})
    return sorted(given), blank


def _ensure(email, first, last, avatar, *, superuser, password, reset_password,
            business=None, refresh_picture=False):
    """Create or repair one account and its profile.

    A missing account needs ``password``. An existing one is reactivated, its
    flags reset and, only with ``reset_password``, its password changed; a blank
    name in the table never wipes one entered on the site. The profile is marked
    registered and profile-complete so onboarding is skipped, ``business``
    contact details are written for system accounts, :data:`STAFF_PROFILES` are
    applied and the e-mail is verified.

    Returns ``(user, action, pictured, details)``.
    """
    from django.contrib.auth import get_user_model

    from apps.accounts.models import Person, PersonContact
    from core import seed_builders

    User = get_user_model()
    user = User.objects.filter(email__iexact=email).first()
    if user is None:
        if not password:
            raise ValueError(f"{email} does not exist yet; a password is needed to create it.")
        maker = User.objects.create_superuser if superuser else User.objects.create_user
        user = maker(username=email.split("@", 1)[0], email=email, password=password,
                     first_name=first, last_name=last)
        action = "created"
    else:
        user.first_name = first or user.first_name
        user.last_name = last or user.last_name
        user.is_active = True
        user.is_staff = user.is_superuser = superuser
        if reset_password and password:
            user.set_password(password)
        user.save()
        action = "reset  " if reset_password else "updated"

    person = getattr(user, "profile", None) or Person.objects.create(user=user)
    person.first_name = first or person.first_name
    person.last_name = last or person.last_name
    person.user_type = "admin" if superuser else "staff"
    person.registered = True
    person.profile_status = True
    person.pending_invoice_uid = None
    if business:
        person.phone = business["phone"]
        person.primary_device = "laptop"
    person.save()

    if business:
        PersonContact.objects.update_or_create(person=person, defaults={
            "phone_country": "ZA", "primary_phone": business["phone"],
            "suburb": business["suburb"], "city": business["city"],
            "province": business["province"],
        })

    details = None
    if email in STAFF_PROFILES:
        details = _apply_profile(person, STAFF_PROFILES[email])

    pictured = _attach_avatar(person, avatar, replace=refresh_picture)
    seed_builders.verify_email(user)
    return user, action, pictured, details


def ensure_staff_admins(password=None, *, reset_password=False, refresh_pictures=False, log=print):
    """Create or repair every staff admin and system account. Returns the users.

    ``password`` is only used to create a missing account (or, with
    ``reset_password``, to reset one). ``refresh_pictures`` redraws the sending
    mailboxes' initials avatars (the administrator's picture is never
    replaced). The password is never read from .env: the install script passes
    it when it creates the accounts.
    """
    business = _business_contact()
    users = []
    for rows, superuser in ((STAFF_ADMINS, True), (SYSTEM_ACCOUNTS, False)):
        for email, first, last, avatar in rows:
            user, action, pictured, details = _ensure(
                email, first, last, avatar, superuser=superuser, password=password,
                reset_password=reset_password, business=None if superuser else business,
                refresh_picture=refresh_pictures and not superuser)
            users.append(user)
            role = "superuser · /admin/" if superuser else "staff (sending mailbox)"
            log(f"    {action} {email:34} {first} {last} · {role}"
                f"{' · picture set' if pictured else ''}")
            if details:
                done, todo = details
                log(f"      details set: {', '.join(done) or 'none'}"
                    f"{' · still to fill: ' + ', '.join(todo) if todo else ''}")
            if not (user.last_name or "").strip():
                log("      surname still to fill (STAFF_ADMINS)")
    return users
