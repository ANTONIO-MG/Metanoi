"""Resolving a Teams attendance report back to Metanoia accounts.

Microsoft reports each anonymous attendee as whatever they typed into the
"Enter your name" box, with an e-mail only sometimes. A display name is only a
safe key inside a bounded roster of people who could legitimately attend:

* everyone the session was addressed to (:mod:`apps.livesessions.audience`),
* everyone who clicked through from Metanoia (:class:`SessionJoin`) — which is
  authoritative, because that click happened behind the login gate.

Ambiguous names are refused. Matching is tried strongest-evidence-first:

1. **e-mail** — exact, unambiguous.
2. **Microsoft UPN** (``Person.ms_upn``) — the identity a licensed user carries.
3. **a remembered alias** (:class:`TeamsIdentityAlias`) — a correction an educator
   already made once, so nobody makes it twice.
4. **normalised name**, within the roster only, and only when exactly one person
   in the roster answers to it.

Anything left over is not guessed at: it is handed to the educator on the
register to resolve, and that answer is remembered as an alias.

``_DECORATION`` matches the trailing decoration people put in their Teams name
— "(Student)", bracketed notes, a phone's autocapitalised initials — which is
stripped (with emoji and punctuation) before comparing.
"""

import logging
import re
import unicodedata

logger = logging.getLogger('apps')

_DECORATION = re.compile(r'[\(\[\{].*?[\)\]\}]')
_NON_NAME = re.compile(r'[^a-z0-9 ]+')


def normalise(value):
    """A display name or address reduced to a comparable key.

    Case-folded, accent-stripped, bracketed decoration and emoji removed,
    whitespace collapsed. ``"  Thabo  MOKOENA (PGDA) 🎓 "`` → ``"thabo mokoena"``.
    """
    if not value:
        return ''
    text = str(value).strip()
    text = _DECORATION.sub(' ', text)
    text = unicodedata.normalize('NFKD', text)
    text = ''.join(ch for ch in text if not unicodedata.combining(ch))
    text = text.casefold()
    text = _NON_NAME.sub(' ', text)
    return ' '.join(text.split())


def normalise_address(value):
    """An e-mail / UPN reduced to a comparable key (case only — never strip dots)."""
    return str(value or '').strip().casefold()


def display_name_for(user):
    """The name we ask this person to enter in Teams, and record against them.

    Profile first and last name, falling back through the account's name fields
    to the username, so it is never blank.
    """
    person = getattr(user, 'profile', None)
    if person is not None:
        name = f'{person.first_name} {person.last_name}'.strip()
        if name:
            return name
    name = (user.get_full_name() or '').strip()
    return name or user.get_username()


def roster_for(meeting):
    """Every :class:`User` who could legitimately appear in this session.

    The union of the session's audience and everyone who clicked through from
    Metanoia. Bounding the candidate set is what makes name matching safe: a name
    that is ambiguous across the platform is usually unique inside one class.
    """
    from django.contrib.auth import get_user_model
    from django.db.models import Q

    from .audience import recipients_for

    User = get_user_model()
    return (User.objects
            .filter(Q(pk__in=recipients_for(meeting).values('pk'))
                    | Q(session_joins__meeting=meeting))
            .select_related('profile')
            .distinct())


def build_index(meeting, roster=None):
    """``(index, ambiguous)`` for one session.

    ``index`` maps a normalised key → user. ``ambiguous`` is the set of keys that
    more than one person in the roster answers to; those are deliberately absent
    from the index, because a wrong match on a register is worse than no match.

    Each person is indexed by e-mail, Microsoft UPN, display name and the local
    part of their address (which is what a lot of people type). Remembered
    aliases are added last so they can fill gaps; they are per-person and
    explicit, so they never create ambiguity of their own.
    """
    roster = list(roster if roster is not None else roster_for(meeting))
    index, seen_twice = {}, set()

    def claim(key, user):
        """Index ``key`` to ``user``, or mark it ambiguous when another person already holds it."""
        if not key:
            return
        existing = index.get(key)
        if existing is None:
            index[key] = user
        elif existing.pk != user.pk:
            seen_twice.add(key)

    for user in roster:
        person = getattr(user, 'profile', None)
        claim(normalise_address(user.email), user)
        if person is not None:
            claim(normalise_address(person.ms_upn), user)
        claim(normalise(display_name_for(user)), user)
        local = normalise_address(user.email).split('@')[0]
        claim(normalise(local.replace('.', ' ')), user)

    from .models import TeamsIdentityAlias
    person_ids = {u.profile.pk: u for u in roster if getattr(u, 'profile', None)}
    for alias in TeamsIdentityAlias.objects.filter(person_id__in=person_ids):
        index.setdefault(alias.identity, person_ids[alias.person_id])

    for key in seen_twice:
        index.pop(key, None)
    return index, seen_twice


def keys_for_record(record):
    """Every key a Graph attendance record offers, strongest evidence first."""
    identity = record.get('identity') or {}
    addresses = [
        record.get('emailAddress'),
        identity.get('userPrincipalName'),
        record.get('userPrincipalName'),
    ]
    names = [identity.get('displayName'), record.get('displayName')]

    keys = []
    for value in addresses:
        key = normalise_address(value)
        if key and key not in keys:
            keys.append(key)
    for value in names:
        key = normalise(value)
        if key and key not in keys:
            keys.append(key)
    return keys


def resolve(record, index):
    """``(user, matched_key)`` for one Graph record, or ``(None, '')``."""
    for key in keys_for_record(record):
        user = index.get(key)
        if user is not None:
            return user, key
    return None, ''


def label_for_record(record):
    """How to show an unmatched attendee to an educator."""
    identity = record.get('identity') or {}
    name = (identity.get('displayName') or record.get('displayName') or '').strip()
    email = (record.get('emailAddress') or '').strip()
    if name and email:
        return f'{name} <{email}>'
    return name or email or 'Unnamed attendee'


def remember(person, raw_identity, *, by=None, source=None):
    """Record that ``raw_identity`` belongs to ``person``, for every future session.

    Refuses to attach an identity that already belongs to somebody else: silently
    moving one would rewrite whichever register it was previously matching.
    Returns the alias, or ``None`` when it was refused.
    """
    from .models import TeamsIdentityAlias

    key = normalise_address(raw_identity)
    if '@' not in key:
        key = normalise(raw_identity)
    if not key or person is None:
        return None

    existing = TeamsIdentityAlias.objects.filter(identity=key).first()
    if existing is not None:
        if existing.person_id != person.pk:
            logger.warning('livesessions: %r already belongs to %s — not reassigning',
                           raw_identity, existing.person)
            return None
        return existing

    return TeamsIdentityAlias.objects.create(
        person=person, identity=key, raw=str(raw_identity)[:255],
        source=source or TeamsIdentityAlias.SOURCE_MANUAL, created_by=by)
