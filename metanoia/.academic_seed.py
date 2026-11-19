#!/usr/bin/env python
"""
.academic_seed.py  —  set up the Metanoia Learning Hub so it is ready to use.

One script for a new machine, a fresh database, or topping up a running one. It
creates the sample academic structure and the Metanoia role mailboxes, and **no
demo or example people** — the only accounts are the four on the
``@metanoiagroup.co.za`` role mailboxes (core.staff_accounts):

    admin@                              the administrator (superuser)
    accounts@  info@  learninghub@      the sending mailboxes (staff)

The academic structure is neutral sample data (one Sample Programme with three
modules, see core.academic_spine); replace it with the real programmes.

What it sets up, in order
-------------------------
 1. The database — created if missing; with ``--fresh`` every table is dropped
    first. Then ``migrate``.
 2. The site address (from SITE_URL) and the folders the app writes to.
 3. The academic structure — institutions, programmes, modules, cohorts, phases
    and calendars (core.academic_spine, placeholder module fee
    ``DEFAULT_PRICE``), every module's topics (``seed_sample_structure``), each
    module's 4-test + 2-exam schedule (``scaffold_module_schedule``), and one
    community per programme next to the Metanoia Community. With
    ``--sample-pack`` also the Sample Programme pack: weeks, lessons, files and
    live sessions (``seed_sample_programme``). Fees are not changed here: run
    ``manage.py apply_module_pricing`` once its rule is the owner's.
 4. The four role-mailbox accounts — verified, onboarded, with generated
    initials avatars.
 5. Metanoia Mail — the mail server settings, a mailbox for each account whose
    password is in the environment, and the Metanoia signature on every mailbox.
 6. The sending mailboxes — which one sends what (apps.communication.senders),
    and whether each has the credentials it needs. ``--test-email`` sends a test
    message from each.
 7. The shop — the billing catalogue, the store, and the hourly 1-on-1 shop item
    (coaches are given a rate with ``manage.py ensure_one_on_one_rates --email``).
 8. Learning — the onboarding guide, the module chat rooms, the bundled sample
    content pack and, if a ``Metanoia Material`` archive is present, its material.

Every step is idempotent: re-running changes nothing that is already right,
and never resets a password unless asked to.

Usage (from the directory containing ``manage.py``)::

    python .academic_seed.py                    # build or top up — safe on a live database
    python .academic_seed.py --fresh            # DROP EVERY TABLE, then build (asks first)
    python .academic_seed.py --fresh --yes      # ... without asking
    python .academic_seed.py --remove-demo      # delete leftover demo / example accounts
    python .academic_seed.py --password 'X'     # password for team accounts it has to create
    python .academic_seed.py --reset-passwords --password 'X'   # and reset existing ones to it
    python .academic_seed.py --test-email you@example.com       # test each sending mailbox
    python .academic_seed.py --check-mail       # also sign in to each Metanoia Mail mailbox
    python .academic_seed.py --sample-pack      # also build the Sample Programme pack

Passwords are never stored in this file or in .env. Without ``--password``
(or SEED_ACCOUNT_PASSWORD in the environment) a team account that has to be
created gets a random password, printed once at the end.

Mailbox passwords come from the environment (on the server: OCI Vault, through
metanoia-secrets.service): EMAIL_ACCOUNTS_PASSWORD, EMAIL_INFO_PASSWORD and
EMAIL_LEARNINGHUB_PASSWORD for the sending mailboxes, and MAILBOX_PASSWORD_ADMIN
for the administrator's.
"""
import os
import secrets
import string
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402

django.setup()

from django.conf import settings  # noqa: E402
from django.contrib.auth import get_user_model  # noqa: E402
from django.core.management import call_command  # noqa: E402
from django.db import connections  # noqa: E402

from core.staff_accounts import STAFF_ADMINS, SYSTEM_ACCOUNTS  # noqa: E402

TEAM = [row[0] for row in STAFF_ADMINS + SYSTEM_ACCOUNTS]

DEMO_DOMAINS = ("@metanoia.co.za", "@example.com")
"""E-mail domains of accounts made by the old demo / reset scripts. ``--remove-demo``
deletes these and nothing else — never a real student."""

SENDING_PASSWORD_ENV = {
    "accounts@metanoiagroup.co.za": "EMAIL_ACCOUNTS_PASSWORD",
    "info@metanoiagroup.co.za": "EMAIL_INFO_PASSWORD",
    "learninghub@metanoiagroup.co.za": "EMAIL_LEARNINGHUB_PASSWORD",
}
"""Sending mailbox → the environment variable holding its password."""


def arg(name, default=None):
    """The value after ``--name`` on the command line, if any."""
    flag = f"--{name}"
    for i, item in enumerate(sys.argv):
        if item == flag and i + 1 < len(sys.argv):
            return sys.argv[i + 1]
        if item.startswith(flag + "="):
            return item.split("=", 1)[1]
    return default


def flag(name):
    """True when ``--name`` appears on the command line."""
    return f"--{name}" in sys.argv


def step(n, total, title):
    """Print the ``[n/total] title`` heading for a setup step."""
    print(f"\n[{n}/{total}] {title}")


def ensure_database(alias):
    """Create the PostgreSQL database if the server does not have it yet.

    Returns ``True`` only when the database was created. A managed database
    often refuses connections to the ``postgres`` maintenance database; it has
    already created ours, so proving we can reach it is enough.
    """
    conn = connections[alias]
    cfg = conn.settings_dict
    if conn.vendor != "postgresql":
        return False
    import psycopg2
    from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT

    opts = {k: v for k, v in (cfg.get("OPTIONS") or {}).items()
            if k in ("sslmode", "sslrootcert", "sslcert", "sslkey", "connect_timeout")}
    common = dict(user=cfg.get("USER") or None, password=cfg.get("PASSWORD") or None,
                  host=cfg.get("HOST") or None, port=cfg.get("PORT") or None, **opts)
    try:
        admin = psycopg2.connect(dbname="postgres", **common)
    except psycopg2.OperationalError:
        psycopg2.connect(dbname=cfg["NAME"], **common).close()
        return False
    admin.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    try:
        with admin.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s;", [cfg["NAME"]])
            if cur.fetchone():
                return False
            cur.execute(f'CREATE DATABASE "{cfg["NAME"]}";')
            if cfg.get("USER"):
                cur.execute(f'ALTER DATABASE "{cfg["NAME"]}" OWNER TO "{cfg["USER"]}";')
            print(f"  → created database '{cfg['NAME']}'")
            return True
    finally:
        admin.close()


def drop_all_tables(alias):
    """Drop every table on ``alias`` (PostgreSQL: recreate the ``public`` schema)."""
    conn = connections[alias]
    print(f"  → dropping every table on '{alias}' ...")
    with conn.cursor() as cur:
        if conn.vendor == "postgresql":
            cur.execute("DROP SCHEMA public CASCADE;")
            cur.execute("CREATE SCHEMA public;")
            if conn.settings_dict.get("USER"):
                cur.execute(f'GRANT ALL ON SCHEMA public TO "{conn.settings_dict["USER"]}";')
            cur.execute("GRANT ALL ON SCHEMA public TO public;")
        else:
            from django.core.management.color import no_style
            for sql in conn.ops.sql_flush(no_style(), conn.introspection.table_names(cur),
                                          reset_sequences=True, allow_cascade=True):
                cur.execute(sql)


def confirm_fresh():
    """Ask the operator to type ``FRESH`` before ``--fresh`` wipes the database.

    ``--yes`` / ``--y`` skips the prompt.
    """
    if flag("yes") or flag("y"):
        return True
    cfg = connections["default"].settings_dict
    print("=" * 72)
    print(f"--fresh PERMANENTLY DROPS EVERY TABLE in '{cfg['NAME']}' on {cfg.get('HOST') or 'localhost'}")
    print("and rebuilds it with the sample academic structure and the four role-mailbox accounts.")
    print("=" * 72)
    return input("Type 'FRESH' to continue: ").strip() == "FRESH"


def prepare_database(fresh):
    """Step 1: create each configured database if needed, wipe it on ``--fresh``, migrate."""
    for alias in settings.DATABASES:
        created = ensure_database(alias)
        if fresh and not created:
            drop_all_tables(alias)
        call_command("migrate", database=alias, interactive=False, verbosity=0)
        print(f"  → '{alias}' migrated")


def sync_site():
    """Step 2: point the ``django.contrib.sites`` row at the domain in ``SITE_URL``."""
    from urllib.parse import urlsplit

    from django.contrib.sites.models import Site
    domain = urlsplit(getattr(settings, "SITE_URL", "") or "").netloc
    if domain:
        Site.objects.update_or_create(pk=getattr(settings, "SITE_ID", 1),
                                      defaults={"domain": domain, "name": "Metanoia Group"})
        print(f"  → site address: {domain} (SITE_URL)")
    else:
        print("  → SITE_URL is not set — the site address was left alone")


def prepare_runtime_dirs():
    """Step 2: create ``media/`` and ``backups/archived_accounts/`` if missing."""
    for rel in ("media", "backups/archived_accounts"):
        (BASE_DIR / rel).mkdir(parents=True, exist_ok=True)
    print("  → media/ and backups/ ready")


def seed_academics(sample_pack=False):
    """Step 3: institutions, programmes, modules, topics, schedules, communities.

    ``sample_pack`` also builds the Sample Programme pack (``seed_sample_programme``).
    """
    from apps.communication.models import Community
    from apps.learning.models import Programme
    from core import academic_spine

    result = academic_spine.seed(indent="    ")
    print(f"  → {len(result['institutions'])} institutions, {len(result['programmes'])} programmes, "
          f"{len(result['offerings'])} module offerings, {result['events_total']} calendar dates "
          f"for {result['year']}")
    call_command("seed_sample_structure", verbosity=0)
    print("  → every module's topics (seed_sample_structure)")
    if sample_pack:
        call_command("seed_sample_programme", verbosity=0)
        print("  → the Sample Programme pack: weeks, lessons, files, live sessions (seed_sample_programme)")
    call_command("scaffold_module_schedule", verbosity=0)
    print("  → 4 test blocks + 2 exam blocks per module (scaffold_module_schedule)")
    Community.default()
    for programme in Programme.objects.select_related("institution"):
        Community.for_programme(programme)
    print(f"  → communities: {Community.objects.count()} (the Metanoia Community + one per programme)")


def random_password():
    """A random ``Metanoia-…`` password for a team account created without ``--password``."""
    alphabet = string.ascii_letters + string.digits
    return "Metanoia-" + "".join(secrets.choice(alphabet) for _ in range(14))


def seed_team(password, reset):
    """Step 4: create (or top up) the four role-mailbox accounts.

    Returns ``(generated_password_or_None, emails_that_were_missing)``.
    """
    from core.staff_accounts import ensure_staff_admins

    User = get_user_model()
    missing = [e for e in TEAM if not User.objects.filter(email__iexact=e).exists()]
    generated = None
    if missing and not password:
        password = generated = random_password()
    ensure_staff_admins(password, reset_password=reset, log=lambda m: print(f"  {m}"))
    return generated, missing


def remove_demo_accounts():
    """Delete accounts on :data:`DEMO_DOMAINS`, never the team accounts."""
    from django.db.models import Q

    User = get_user_model()
    match = Q()
    for domain in DEMO_DOMAINS:
        match |= Q(email__iendswith=domain)
    victims = User.objects.filter(match).exclude(email__in=TEAM)
    emails = list(victims.values_list("email", flat=True))
    for user in victims:
        user.delete()
    print(f"  → removed {len(emails)} demo / example account(s)"
          + (f": {', '.join(sorted(emails)[:8])}{' …' if len(emails) > 8 else ''}" if emails else ""))


def mailbox_password(email):
    """The mailbox password for ``email`` from the environment, or ``''``.

    Sending mailboxes use :data:`SENDING_PASSWORD_ENV`; personal ones use
    ``MAILBOX_PASSWORD_<LOCALPART>``.
    """
    if email in SENDING_PASSWORD_ENV:
        return os.getenv(SENDING_PASSWORD_ENV[email], "")
    local = email.split("@", 1)[0].upper()
    return os.getenv(f"MAILBOX_PASSWORD_{local}", "")


def seed_mail(check):
    """Step 5: Metanoia Mail server settings, team mailboxes and signatures.

    Blank server settings are filled from the sending settings
    (``mail.<domain>``, SMTP 465/SSL, IMAP 993/SSL); anything staff already set
    on the Mail settings page wins. With ``check`` each mailbox also signs in.
    """
    from apps.mailbox import signatures
    from apps.mailbox.models import MailAccount, MailServer

    server = MailServer.get()
    host = getattr(settings, "EMAIL_HOST", "") or ""
    changed = []
    if not server.domain:
        server.domain = os.getenv("MAILBOX_DOMAIN", "") or "metanoiagroup.co.za"
        changed.append("domain")
    if not server.imap_host and host and host not in ("localhost", "127.0.0.1"):
        server.imap_host, server.imap_port, server.imap_security = host, 993, "ssl"
        changed.append("IMAP")
    if not server.smtp_host and host and host not in ("localhost", "127.0.0.1"):
        server.smtp_host = host
        server.smtp_port = int(getattr(settings, "EMAIL_PORT", 465) or 465)
        server.smtp_security = "ssl" if getattr(settings, "EMAIL_USE_SSL", False) else "starttls"
        changed.append("SMTP")
    server.save()
    print(f"  → mail server: {server.domain} · IMAP {server.imap_host or '—'}:{server.imap_port} "
          f"{server.imap_security} · SMTP {server.smtp_host or '—'}:{server.smtp_port} {server.smtp_security}"
          + (f" (filled in: {', '.join(changed)})" if changed else ""))

    User = get_user_model()
    for email in TEAM:
        user = User.objects.filter(email__iexact=email).first()
        if user is None:
            continue
        account = MailAccount.objects.filter(user=user, email_address__iexact=email).first()
        password = mailbox_password(email)
        if account is None and not password:
            print(f"  · {email:36} no mailbox yet — its password is not in the environment")
            continue
        if account is None:
            account = MailAccount(user=user, email_address=email.lower(), is_default=True,
                                  display_name=f"{user.get_full_name().strip() or email} | Metanoia Group")
            note = "connected"
        else:
            note = "kept"
        if password:
            account.set_password(password)
        account.save()
        signed = signatures.apply_default(account, force=flag("refresh-signatures"))
        status = ""
        if check:
            from apps.mailbox.views import _test_account
            ok, text = _test_account(account)
            status = f" · {'sign-in OK' if ok else 'sign-in FAILED: ' + text}"
        print(f"  ✓ {email:36} {note}{' · signature set' if signed else ' · signature kept'}{status}")


def report_senders(test_to):
    """Step 6: print which mailbox sends what and whether it has credentials.

    With ``test_to`` a test message is sent from each mailbox to that address.
    """
    from django.core.mail import EmailMessage

    from apps.communication import senders

    backend = settings.EMAIL_BACKEND.rsplit(".", 1)[-1]
    print(f"  → e-mail backend: {backend}"
          + ("  (prints mail to the console — set the SMTP backend to deliver)"
             if "console" in settings.EMAIL_BACKEND else ""))
    sends = {senders.ACCOUNTS: [], senders.INFO: [], senders.LEARNING: []}
    for template, box in senders.TEMPLATE_MAILBOX.items():
        sends[box].append(template)
    for category, box in senders.NOTIFICATION_MAILBOX.items():
        sends[box].append(f"{category} notifications")
    sends[senders.INFO].append("everything else (announcements, messages, hub mail)")
    for box, what in sends.items():
        creds = (getattr(settings, "EMAIL_MAILBOXES", {}) or {}).get(box) or {}
        ready = bool(creds.get("user") and creds.get("password"))
        print(f"  {'✓' if ready else '·'} {box + '@':13} {'ready' if ready else 'NO PASSWORD — falls back to the default connection'}")
        print(f"      sends: {', '.join(what)}")
        if test_to:
            try:
                EmailMessage(subject=f"Metanoia test — sent from {box}@",
                             body="This is a test message from .academic_seed.py --test-email.",
                             from_email=senders.from_address(box), to=[test_to],
                             connection=senders.connection(box)).send()
                print(f"      test e-mail sent to {test_to}")
            except Exception as exc:  # noqa: BLE001
                print(f"      test e-mail FAILED: {exc}")


def seed_shop():
    """Step 7: billing catalogue, store items and the hourly one-on-one shop item."""
    call_command("sync_catalogue", verbosity=0)
    print("  → billing catalogue: a fee item per module and programme (sync_catalogue)")
    call_command("seed_shop", quiet=True, verbosity=0)
    print("  → store: merchandise and per-institution material (seed_shop)")
    call_command("ensure_one_on_one_rates", verbosity=0)
    print("  → 1-on-1 sessions: the hourly shop item (ensure_one_on_one_rates; add coaches with --email)")


def seed_learning():
    """Step 8: onboarding guide, module chat rooms, the sample content pack and archive material."""
    from apps.communication import services
    from apps.learning.models import ProgrammeModule
    from core import hub_guide

    author = get_user_model().objects.filter(email__iexact="admin@metanoiagroup.co.za").first()
    _course, _modules, lessons = hub_guide.build(author=author)
    print(f"  → onboarding guide: {lessons} lesson(s), one per audience")
    rooms = 0
    for offering in ProgrammeModule.objects.filter(is_active=True):
        services.sync_module_chat_members(offering)
        rooms += 1
    services.ensure_general_group()
    print(f"  → chat rooms: {rooms} module rooms + General")
    try:
        call_command("import_seed_packs", verbosity=0)
        print("  → bundled sample content pack (core/seed_packs/)")
    except Exception as exc:  # noqa: BLE001
        print(f"  · content packs skipped: {exc}")
    archive = BASE_DIR / "Metanoia Material"
    if archive.is_dir():
        call_command("seed_2026_materials", root=str(archive), as_user="learninghub@metanoiagroup.co.za")
        print("  → teaching material placed from 'Metanoia Material/'")
    else:
        print("  · teaching material: no 'Metanoia Material/' archive here (none ships with the platform)")


def main():
    """Run the eight setup steps in order and print the resulting accounts."""
    fresh = flag("fresh")
    if fresh and not confirm_fresh():
        print("Aborted. Nothing was changed.")
        sys.exit(1)
    password = arg("password") or os.getenv("SEED_ACCOUNT_PASSWORD", "")
    reset = flag("reset-passwords")
    if reset and not password:
        sys.exit("--reset-passwords needs --password (or SEED_ACCOUNT_PASSWORD).")
    total = 8

    step(1, total, "Database" + (" — fresh" if fresh else ""))
    prepare_database(fresh)
    step(2, total, "Site address and folders")
    sync_site()
    prepare_runtime_dirs()
    step(3, total, "Academic structure")
    seed_academics(sample_pack=flag("sample-pack"))
    step(4, total, "Role-mailbox accounts")
    generated, created = seed_team(password, reset)
    if flag("remove-demo"):
        remove_demo_accounts()
    step(5, total, "Metanoia Mail — server, mailboxes, signatures")
    seed_mail(check=flag("check-mail"))
    step(6, total, "Sending mailboxes — who sends what")
    report_senders(arg("test-email"))
    step(7, total, "Shop")
    seed_shop()
    step(8, total, "Learning — guide, chat rooms, content")
    seed_learning()

    User = get_user_model()
    print("\n" + "=" * 72)
    print(f"Done. {User.objects.count()} account(s) on the system:")
    for user in User.objects.order_by("-is_superuser", "email"):
        role = getattr(getattr(user, "profile", None), "user_type", "")
        print(f"  {user.email:38} {role}{' · superuser' if user.is_superuser else ''}")
    if generated:
        print(f"\nNew account(s) {', '.join(created)} were created with the password:\n"
              f"    {generated}\nIt is shown only now — sign in and change it.")
    elif created and password:
        print(f"\nNew account(s) {', '.join(created)} use the password you passed in.")
    print("=" * 72)


if __name__ == "__main__":
    main()
