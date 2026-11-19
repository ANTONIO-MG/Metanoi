"""Per-user navbar / sidebar counters, cached across requests.

Every page draws the same handful of numbers — unread notifications and
messages, the bell's dropdown, cart items, the sidebar's study
tiles. They used to be recomputed on every request (~40 queries a page). They
now live in the default cache (Redis in production) for :data:`COUNTER_TTL`
seconds under ``counters:<user pk>:<name>``.

Two rules keep them honest:

* **Lazy** — :class:`LazyStats` only computes a value when a template actually
  reads it, so a counter no layout shows costs nothing at all.
* **Invalidated on the user's own actions** — reading a notification, opening
  a chat, adding to the cart, finishing a task etc. call
  :func:`invalidate_counters` (mostly through the model signals wired up in
  :func:`connect_signals`), so the badge a person just changed updates on the
  very next page. Anything else catches up within the TTL.

The cache is an optimisation only: a cache outage (or any error, such as an
unpicklable value) falls through to computing the value directly.

:data:`COUNTER_TTL` is how many seconds a counter may be served from cache
before it is recomputed; :data:`COUNTER_NAMES` lists every counter group cached
per user — invalidation clears them all.
"""

import logging
from collections.abc import Mapping

from django.core.cache import cache

logger = logging.getLogger(__name__)

COUNTER_TTL = 45

COUNTER_NAMES = ('comm', 'shop', 'account', 'nav')

_MISSING = object()


def counter_key(user_id, name):
    """The cache key of counter group ``name`` for a user."""
    return f'counters:{user_id}:{name}'


def cached_counter(user, name, builder, ttl=COUNTER_TTL):
    """``builder()``, served from the cache for ``ttl`` seconds per user."""
    user_id = getattr(user, 'pk', None)
    if user_id is None:
        return builder()
    key = counter_key(user_id, name)
    try:
        value = cache.get(key, _MISSING)
    except Exception:  # pragma: no cover
        value = _MISSING
    if value is _MISSING:
        value = builder()
        try:
            cache.set(key, value, ttl)
        except Exception:  # pragma: no cover
            logger.debug('counters: could not cache %s', key, exc_info=True)
    return value


def invalidate_counters(*users):
    """Forget the cached counters of ``users`` (User objects or primary keys)."""
    keys = []
    for user in users:
        user_id = getattr(user, 'pk', user)
        if user_id is None:
            continue
        keys.extend(counter_key(user_id, name) for name in COUNTER_NAMES)
    if not keys:
        return
    try:
        cache.delete_many(keys)
    except Exception:  # pragma: no cover
        logger.debug('counters: invalidation failed', exc_info=True)


class LazyStats(Mapping):
    """A read-only dict whose values are computed on first access.

    ``loaders`` maps each key to a zero-argument callable. Templates read it like
    the plain dict it replaces (``{{ shop_stats.cart_count }}``); a key nobody
    reads is never computed, and each key is computed at most once.

    A loader that raises behaves like a missing key — exactly what the old
    "return {} on any error" processors gave the templates.
    """

    def __init__(self, loaders):
        self._loaders = loaders
        self._values = {}

    def __getitem__(self, key):
        """The value for ``key``, computed on first access; unknown keys and failing loaders raise ``KeyError``."""
        try:
            return self._values[key]
        except KeyError:
            pass
        loader = self._loaders[key]
        try:
            value = loader()
        except Exception:
            logger.exception('counters: %r failed', key)
            raise KeyError(key)
        self._values[key] = value
        return value

    def __iter__(self):
        return iter(self._loaders)

    def __len__(self):
        return len(self._loaders)

    def __repr__(self):  # pragma: no cover
        """Debugging aid: the keys, not the (lazy) values."""
        return f'<LazyStats {sorted(self._loaders)}>'


def memo(fn):
    """Wrap a zero-argument callable so it runs at most once."""
    box = []

    def wrapper():
        if not box:
            box.append(fn())
        return box[0]
    return wrapper


def _person_user_id(person_id):
    """The user id behind a ``Person`` primary key."""
    from apps.accounts.models import Person
    return Person.objects.filter(pk=person_id).values_list('user_id', flat=True).first()


def connect_signals():
    """Clear a user's counters when the rows behind their badges change.

    Called once from ``MyHubConfig.ready``. Every receiver is a cache delete
    (plus, for a chat message, one cheap lookup of the room's members), so the
    write paths pay next to nothing. Marked assessment attempts are hooked too,
    since they move the sidebar's completion / readiness tiles; an order item
    whose order went with a cascade is ignored.
    """
    from django.db.models.signals import post_delete, post_save

    from apps.communication.models import ChatMembership, Message, Notification
    from apps.learning.models import ModuleEnrolment, ProgrammeEnrolment, StudySession
    from apps.shop.models import Order, OrderItem

    def on_user_field(field):
        """A receiver that clears the counters of the user in ``instance.<field>``."""
        def receiver(sender, instance, **kwargs):
            invalidate_counters(getattr(instance, field, None))
        return receiver

    def on_person(sender, instance, **kwargs):
        """Clear the counters of an enrolment's person (without a query when the person is cached)."""
        person = instance._state.fields_cache.get('person')
        invalidate_counters(person.user_id if person is not None
                            else _person_user_id(instance.person_id))

    def on_message(sender, instance, created=False, **kwargs):
        """A new chat message clears the counters of every member of the room."""
        if not created:
            return
        members = list(ChatMembership.objects.filter(group_id=instance.group_id)
                       .values_list('user_id', flat=True))
        invalidate_counters(*members)

    def on_order_item(sender, instance, **kwargs):
        """Clear the counters of the order's buyer."""
        try:
            invalidate_counters(Order.objects.filter(pk=instance.order_id)
                                .values_list('buyer_id', flat=True).first())
        except Exception:  # pragma: no cover
            pass

    hooks = [
        (Notification, on_user_field('recipient_id')),
        (ChatMembership, on_user_field('user_id')),
        (Order, on_user_field('buyer_id')),
        (StudySession, on_user_field('student_id')),
        (OrderItem, on_order_item),
        (ModuleEnrolment, on_person),
        (ProgrammeEnrolment, on_person),
    ]
    for model, receiver in hooks:
        uid = f'counters-{model._meta.label_lower}'
        post_save.connect(receiver, sender=model, weak=False, dispatch_uid=uid + '-save')
        post_delete.connect(receiver, sender=model, weak=False, dispatch_uid=uid + '-delete')
    post_save.connect(on_message, sender=Message, weak=False,
                      dispatch_uid='counters-communication.message-save')

    try:
        from apps.assessments.models import AssessmentAttempt
        receiver = on_user_field('student_id')
        post_save.connect(receiver, sender=AssessmentAttempt, weak=False,
                          dispatch_uid='counters-assessmentattempt-save')
    except Exception:  # pragma: no cover
        pass


class CounterInvalidationMiddleware:
    """Forget the signed-in user's counters after any request that changes data.

    A person's own actions are writes — marking notifications read (the page
    *and* the REST endpoint, which uses a bulk ``update()`` no signal sees),
    sending a message, adding to the cart, submitting work. Clearing their
    counters after every non-GET request makes all of those show on the very
    next page without hunting down each write path. One cache delete per write;
    reads are untouched.
    """

    SAFE_METHODS = ('GET', 'HEAD', 'OPTIONS', 'TRACE')

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if request.method not in self.SAFE_METHODS:
            user = getattr(request, 'user', None)
            if user is not None and getattr(user, 'is_authenticated', False):
                invalidate_counters(user)
        return response
