"""Who may open what, on a module.

Two ways in, both honoured:

* **Pay for the module** — a :class:`~apps.learning.models.ModuleEnrolment`
  that is ``active`` (paid & current) or on a live ``trial`` opens everything.
* **Buy one item** — a :class:`~apps.learning.models.ModuleMaterial` sold as a
  ``shop.Product`` opens that item only.

Views build one :class:`Gate` per request rather than querying per material.
Staff, admins and the offering's educators bypass it.

Deliberately **not** decided here: whether a candidate has *earned* a row yet
(teaching order) — that is :mod:`apps.learning.sequence`. A row needs both to open.

**Open reasons** (``REASON_*``) say why a row is open, in the order the gate
checks them. The reason is carried through to the template (``REASON_LABELS``)
so a row can say something more useful than "open" or "locked": "in your free
week", "you bought this", "included in the module". ``REASON_MADE_AVAILABLE``
is a week an admin or educator ticked "make available" (see
:mod:`apps.learning.week_access`); ``REASON_ONE_ON_ONE`` is a one-on-one
student, whose modules stay open and are billed by the hour instead.

**Lock reasons** (``LOCK_*``) say why a row is shut:

- ``LOCK_UNPAID`` -- the module is not paid for, or the item not bought;
- ``LOCK_EXPIRED`` -- the trial ran out or the subscription lapsed;
- ``LOCK_EMBARGO`` -- released later (``available_from`` is in the future);
- ``LOCK_UNPUBLISHED`` -- not published;
- ``LOCK_OUT_OF_WINDOW`` -- paid, but the week sits outside the paid month and
  is not the previous, current or next week (see :mod:`apps.learning.week_access`);
- ``LOCK_SEQUENCE`` -- an earlier step in the week is not done;
- ``LOCK_SOLUTION`` -- the attempt has not been submitted yet.

The last two are set by :mod:`apps.learning.sequence` and carried here so one
:class:`MaterialState` can express both verdicts and templates have a single
thing to read.
"""

import logging

logger = logging.getLogger('apps')

REASON_STAFF = 'staff'
REASON_EDUCATOR = 'educator'
REASON_FREE = 'free'
REASON_PREVIEW = 'preview'
REASON_TRIAL = 'trial'
REASON_PAID = 'paid'
REASON_PURCHASED = 'purchased'
REASON_MADE_AVAILABLE = 'made_available'
REASON_ONE_ON_ONE = 'one_on_one'

REASON_LABELS = {
    REASON_STAFF: 'Staff access',
    REASON_EDUCATOR: 'You teach this module',
    REASON_FREE: 'Free module',
    REASON_PREVIEW: 'Free preview',
    REASON_TRIAL: 'Open during your free week',
    REASON_PAID: 'Included in your module',
    REASON_PURCHASED: 'You bought this item',
    REASON_MADE_AVAILABLE: 'Made available by your coach',
    REASON_ONE_ON_ONE: 'Open on your one-on-one plan',
    'paid_before': 'You paid for this week',
}

LOCK_UNPAID = 'unpaid'
LOCK_EXPIRED = 'expired'
LOCK_EMBARGO = 'embargo'
LOCK_UNPUBLISHED = 'unpublished'
LOCK_OUT_OF_WINDOW = 'window'
LOCK_SEQUENCE = 'sequence'
LOCK_SOLUTION = 'solution'


def _one_on_one_hidden_kinds():
    """Material kinds not listed for one-on-one students (live classes and recordings)."""
    from .models import ModuleMaterial
    return {ModuleMaterial.KIND_LIVE, ModuleMaterial.KIND_RECORDING}


class _LazyKinds:
    """Material kinds a one-on-one student never sees (group live classes and
    their recordings) — resolved on first use to keep imports light."""
    _kinds = None

    def __contains__(self, kind):
        if self._kinds is None:
            self._kinds = _one_on_one_hidden_kinds()
        return kind in self._kinds


ONE_ON_ONE_HIDDEN_KINDS = _LazyKinds()


def purchased_product_ids(user):
    """IDs of every shop product this user has actually paid for and still holds.

    Reads paid orders (what the shop checkout writes), not payments; expired
    lines (``OrderItem.expiry_date``) drop out so time-limited passes lapse.
    """
    if not getattr(user, 'is_authenticated', False):
        return set()
    try:
        from apps.shop.models import Order, OrderItem

        items = (OrderItem.objects
                 .filter(order__buyer=user, order__status=Order.STATUS_PAID)
                 .only('product_id', 'expiry_date'))
        return {item.product_id for item in items
                if item.product_id and not item.is_expired}
    except Exception:  # pragma: no cover — the shop is optional
        logger.exception('access: could not read purchased products')
        return set()


def is_staff_like(user):
    """Staff, superusers and anyone whose profile is admin/staff."""
    if not getattr(user, 'is_authenticated', False):
        return False
    if user.is_superuser or user.is_staff:
        return True
    return getattr(getattr(user, 'profile', None), 'user_type', '') in ('admin', 'staff')


class Gate:
    """One request's answer to "may this person open this?" for one offering.

    Built once per page load; caches the module verdict and owned products so
    per-material checks cost no further queries.

        gate = Gate(request.user, offering)
        if gate.module_open: ...
        state = gate.for_material(material)      # → MaterialState

    One-on-one students do not see live-class and recording rows: group classes
    and their recordings are not part of their plan. Purchases are only looked up
    when they could change an answer.
    """

    def __init__(self, user, offering, person=None, today=None):
        self.user = user
        self.offering = offering
        self.person = person if person is not None else getattr(user, 'profile', None)
        self.today = today

        self.is_staff = is_staff_like(user)
        self.is_educator = self._teaches()
        self.one_on_one = bool(not self.is_staff and not self.is_educator
                               and getattr(self.person, 'is_one_on_one', False))
        self.enrolment = offering.enrolment_for(self.person) if self.person else None

        self.module_open, self.reason = self._resolve_module()
        self._owned = None
        self._weeks = None
        self._cohort_id = False

    def _teaches(self):
        """True when the viewer is one of the offering's educators."""
        if self.person is None:
            return False
        try:
            return self.offering.educators.filter(pk=self.person.pk).exists()
        except Exception:  # pragma: no cover
            return False

    def _resolve_module(self):
        """``(open, reason)`` for the module as a whole."""
        if self.is_staff:
            return True, REASON_STAFF
        if self.is_educator:
            return True, REASON_EDUCATOR
        if self.offering.is_free:
            return True, REASON_FREE
        enrolment = self.enrolment
        if enrolment is None:
            return False, LOCK_UNPAID
        if enrolment.is_trial_valid:
            return True, REASON_TRIAL
        if self.one_on_one and enrolment.is_unlocked:
            return True, REASON_ONE_ON_ONE
        if enrolment.is_unlocked:
            return True, REASON_PAID
        if enrolment.status in ('trial', 'expired'):
            return False, LOCK_EXPIRED
        return False, LOCK_UNPAID

    @property
    def can_author(self):
        """Staff and the offering's educators may add and edit material."""
        return self.is_staff or self.is_educator

    @property
    def weeks(self):
        """The viewer's per-week verdicts — :class:`apps.learning.week_access.WeekAccess`."""
        if self._weeks is None:
            from .week_access import WeekAccess
            self._weeks = WeekAccess(self, today=self.today)
        return self._weeks

    def week_verdict(self, week):
        """``(is_open, reason)`` for a week's content — THE week rule. See
        :mod:`apps.learning.week_access`."""
        return self.weeks.verdict(week)

    @property
    def cohort_id(self):
        """The viewer's intake on this programme (whose blocks they see), or None."""
        if self._cohort_id is False:
            self._cohort_id = None
            if self.person is not None:
                self._cohort_id = (self.person.programme_enrolments
                                   .filter(programme_id=self.offering.programme_id)
                                   .values_list('cohort_id', flat=True).first())
        return self._cohort_id

    @property
    def owned_products(self):
        """Ids of the products this user has bought, looked up on first use."""
        if self._owned is None:
            self._owned = purchased_product_ids(self.user)
        return self._owned

    @property
    def module_status_label(self):
        """What the module header says about access."""
        if self.module_open:
            return REASON_LABELS.get(self.reason, 'Open')
        if self.reason == LOCK_EXPIRED:
            return 'Access expired'
        return 'Locked — awaiting payment'

    def for_material(self, material):
        """Resolve one material into a :class:`MaterialState`.

        Staff and educators see everything, including drafts and embargoed items.
        Material in a week follows the week rule (paid month, previous/current/next
        week, or made available); material on the block itself follows the module.
        A material that is not open may still have been bought on its own.
        """
        if self.is_staff or self.is_educator:
            return MaterialState(material, True, REASON_STAFF if self.is_staff else REASON_EDUCATOR,
                                 visible=True)
        if not material.is_published:
            return MaterialState(material, False, LOCK_UNPUBLISHED, visible=False)
        if self.one_on_one and material.kind in ONE_ON_ONE_HIDDEN_KINDS:
            return MaterialState(material, False, LOCK_UNPAID, visible=False)
        if not material.is_released:
            return MaterialState(material, False, LOCK_EMBARGO, visible=True)
        if material.is_preview:
            return MaterialState(material, True, REASON_PREVIEW, visible=True)
        if material.week_id:
            is_open, reason = self.week_verdict(material.week_id)
        else:
            is_open, reason = self.module_open, self.reason
        if is_open:
            return MaterialState(material, True, reason, visible=True)
        if material.product_id and material.product_id in self.owned_products:
            return MaterialState(material, True, REASON_PURCHASED, visible=True)
        return MaterialState(material, False, reason, visible=True,
                             week_locked=bool(material.week_id))

    def states_for(self, materials, *, sequence=None, week_materials=None):
        """``for_material`` across an iterable, dropping what must not be seen.

        Pass ``sequence`` (a :class:`apps.learning.sequence.SequenceGate`) and
        ``week_materials`` to apply the teaching order on top; a row it shuts
        then carries the sequence reason rather than plain "locked".

        Rows that money has already shut are not re-checked by the sequence gate.
        """
        rows = [self.for_material(m) for m in materials]
        if sequence is not None:
            scope = list(week_materials if week_materials is not None else materials)
            for row in rows:
                if not row.open:
                    continue
                is_open, reason = sequence.check(row.material, scope)
                if not is_open:
                    row.open = False
                    row.reason = reason
        return [row for row in rows if row.visible]


class MaterialState:
    """A material plus this viewer's verdict on it — what templates iterate.

    Instances stay mutable on purpose: the sequence gate re-decides ``open`` and
    ``reason`` after the commercial gate has spoken. ``week_locked`` marks a row
    shut because its whole week is locked for this viewer: listings show the week
    (title, dates) but not this row, unless it is sold on its own.
    """

    __slots__ = ('material', 'open', 'reason', 'visible', 'week_locked')

    def __init__(self, material, is_open, reason, *, visible=True, week_locked=False):
        self.material = material
        self.open = is_open
        self.reason = reason
        self.visible = visible
        self.week_locked = week_locked

    def __repr__(self):  # pragma: no cover — debugging aid
        return f'<MaterialState {self.material_id} open={self.open} {self.reason}>'

    @property
    def material_id(self):
        """The material's primary key."""
        return self.material.pk

    @property
    def locked(self):
        """True when the row is shut."""
        return not self.open

    @property
    def reason_label(self):
        """The human label for why the row is open or shut."""
        if self.open:
            return REASON_LABELS.get(self.reason, '')
        from .sequence import SEQUENCE_LOCK_LABELS
        return {
            LOCK_EMBARGO: 'Released later',
            LOCK_EXPIRED: 'Your access has expired',
            LOCK_UNPAID: 'Unlock the module to open this',
            LOCK_OUT_OF_WINDOW: 'Outside your paid month — pay to unlock',
            **SEQUENCE_LOCK_LABELS,
        }.get(self.reason, 'Locked')

    @property
    def url(self):
        """Where the row goes when it is open — always through the gated door.

        Never the material's own ``target_url``: a storage URL bypasses both
        gates and can be shared. :func:`apps.learning.material_open.material_open`
        re-asks both questions before streaming or redirecting.
        """
        if not self.open:
            return ''
        from django.urls import reverse
        return reverse('learning:material-open', args=[self.material.pk])

    @property
    def file_broken(self):
        """The material carries a file, but the file on disk is cut short — it
        is listed as "being replaced" rather than linked to a PDF that won't load."""
        from .file_check import file_is_complete
        return bool(self.material.file) and not file_is_complete(self.material.file)

    @property
    def is_sequence_lock(self):
        """Shut by the teaching order, not by money — never shown as a payment problem."""
        return self.locked and self.reason in (LOCK_SEQUENCE, LOCK_SOLUTION)

    @property
    def listed(self):
        """Should a listing show this row at all? A row in a locked week is
        hidden (the week says it is locked) unless it is sold on its own."""
        return not self.week_locked or self.can_buy_single

    @property
    def can_buy_single(self):
        """A locked row that is also sold on its own — offer the shop link.
        Never for a sequence lock (they already own it)."""
        return self.locked and not self.is_sequence_lock and self.material.is_purchasable


def schedule_refusal(user, *, lesson=None, assessment=None):
    """The schedule row that keeps a lesson / assessment shut for ``user``, or ``None``.

    Scheduled content opens only when at least one published row carrying it is
    open to this viewer — this stops a locked week's guide being read by typing
    its URL. Unscheduled content is left to the module-level paywall.
    """
    from .models import ModuleMaterial

    if lesson is not None:
        rows = ModuleMaterial.objects.filter(lesson=lesson)
    elif assessment is not None:
        rows = ModuleMaterial.objects.filter(assessment=assessment)
    else:
        return None
    rows = list(rows.filter(is_published=True, phase__is_active=True)
                .select_related('phase__programme_module', 'product'))
    if not rows:
        return None
    gates = {}
    for material in rows:
        offering = material.phase.programme_module
        if offering.pk not in gates:
            gates[offering.pk] = Gate(user, offering)
        if gates[offering.pk].for_material(material).open:
            return None
    return rows[0]
