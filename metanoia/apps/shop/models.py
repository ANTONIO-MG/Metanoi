"""Shop models: the catalogue, orders/carts, discounts, 1-on-1 bookings, account
credit and delivery. ``OrderItem.expiry_date`` derives from a service's
``validity_days`` so term fees expire automatically."""

import re
import uuid
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.urls import reverse
from django.utils import timezone
from django.utils.html import escape
from django.utils.safestring import mark_safe
from django.utils.text import Truncator, slugify

from core import validators as v


STATUS_CHOICES = [
    ('active', 'Active'),
    ('inactive', 'Inactive'),
]


class TimeStampedModel(models.Model):
    """Abstract base adding ``created_at`` / ``updated_at`` timestamps."""

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class ProductCategory(TimeStampedModel):
    """A shelf grouping for products (slug generated from the name)."""

    name = models.CharField(max_length=120, unique=True)
    slug = models.SlugField(max_length=140, unique=True, blank=True)
    description = models.TextField(blank=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='active')

    class Meta:
        ordering = ['name']
        verbose_name = 'Product category'
        verbose_name_plural = 'Product categories'

    def __str__(self):
        """The category name."""
        return self.name

    def save(self, *args, **kwargs):
        """Generate the slug from the name on first save."""
        if not self.slug:
            self.slug = slugify(self.name)[:140]
        super().save(*args, **kwargs)


class Product(TimeStampedModel):
    """An item in the catalogue — sold in the shop, billed on invoices, or both.

    **Fulfilment** says what happens after payment, and it — not ``kind`` — is
    what the shop acts on: a digital item unlocks its files (see
    :mod:`apps.shop.entitlements`), a booking confirms a 1-on-1 slot, a shipped
    item goes to the courier, and a plain fee just settles. ``kind`` is kept in
    step (booking → service, everything else → product) because older pages
    and reports still group by it.

    **1-on-1 sessions** (booking) — the rate itself lives per educator
    (:class:`EducatorRate`); ``price`` is only the "from" figure on the card.
    The session settings (lengths, buffer, notice, window…) are all staff-set.

    **Parcel** (shipped items) — weight and dimensions are what the courier
    quotes on.

    **Who the item is for** — a blank ``institution`` makes a general item;
    setting it scopes the item to that institution, and ``programme`` narrows it
    further. This is enforced in one place, :func:`apps.shop.scoping.visible_products`.
    ``module`` is a tag for filtering, not a gate: a candidate may buy material
    for a module they are not on.

    **Invoicing** — staff bill from the same catalogue. Tuition is invoiced but
    never sold off the shelf (``shop_listed`` off). A tuition item ``unlocks``
    its module(s) when the invoice is paid, for as many months as the line's
    quantity; its price follows the module fees while ``price_from_modules`` is
    on. See :mod:`apps.shop.catalogue`.

    **Store card** (the website's Blueprint poster) — the ``card_*`` fields are
    all optional: left blank, the card is drawn from the name, codes, price and
    description (see :attr:`card`).
    """

    KIND_PRODUCT = 'product'
    KIND_SERVICE = 'service'
    KIND_CHOICES = [
        (KIND_PRODUCT, 'Product'),
        (KIND_SERVICE, 'Service'),
    ]

    FULFIL_DIGITAL = 'digital'
    FULFIL_BOOKING = 'booking'
    FULFIL_SHIPPED = 'shipped'
    FULFIL_NONE = 'none'
    FULFILMENT_CHOICES = [
        (FULFIL_DIGITAL, 'Digital download'),
        (FULFIL_BOOKING, '1-on-1 session'),
        (FULFIL_SHIPPED, 'Shipped item'),
        (FULFIL_NONE, 'Fee / other'),
    ]
    AISLES = {
        'resources': (FULFIL_DIGITAL, 'Learning resources'),
        'sessions': (FULFIL_BOOKING, '1-on-1 sessions'),
        'store': (FULFIL_SHIPPED, 'Study-materials store'),
    }
    """The storefront's three aisles, keyed by the fulfilment they hold."""

    category = models.ForeignKey(
        ProductCategory, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='products',
    )
    kind = models.CharField(max_length=10, choices=KIND_CHOICES, default=KIND_PRODUCT)
    fulfilment = models.CharField(max_length=10, choices=FULFILMENT_CHOICES,
                                  default=FULFIL_DIGITAL, db_index=True)
    name = models.CharField(max_length=200)
    sku = models.CharField('SKU', max_length=40, unique=True, blank=True)
    description = models.TextField(blank=True)
    price = models.DecimalField(max_digits=12, decimal_places=2, default=0,
                                help_text='Selling price in rand, VAT included where VAT applies.')
    discount_percent = models.DecimalField(
        max_digits=5, decimal_places=2, default=0,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
        help_text='Percentage off the price, shown as a sale on the card. 0 = no discount.')
    image = models.ImageField(upload_to='shop/products/', blank=True, null=True,
                              validators=v.validate_image)
    stock = models.PositiveIntegerField(default=0, help_text='Units in stock (products only).')
    track_stock = models.BooleanField(
        default=False,
        help_text='Stop selling when stock (or a size\'s stock) reaches zero.')

    session_lengths = models.CharField(
        max_length=40, default='30,60,90,120',
        help_text='Minutes a student may book, comma-separated.')
    buffer_minutes = models.PositiveSmallIntegerField(
        default=15, help_text='Gap kept free either side of every session.')
    min_notice_hours = models.PositiveSmallIntegerField(
        default=24, help_text='How far ahead a session must be booked.')
    reschedule_cutoff_hours = models.PositiveSmallIntegerField(
        default=24, help_text='A student may move a session until this many hours before it.')
    booking_window_days = models.PositiveSmallIntegerField(
        default=28, help_text='How far into the future sessions can be booked.')

    weight_kg = models.DecimalField(max_digits=6, decimal_places=2, default=0,
                                    help_text='Packed weight of one unit, in kg.')
    length_cm = models.PositiveIntegerField(default=0)
    width_cm = models.PositiveIntegerField(default=0)
    height_cm = models.PositiveIntegerField(default=0)
    validity_days = models.PositiveIntegerField(
        null=True, blank=True,
        help_text='Days until a purchased service expires (services only).',
    )

    institution = models.ForeignKey(
        'learning.Institution', on_delete=models.CASCADE, null=True, blank=True,
        related_name='shop_products',
        help_text='Blank = a general item everyone can see (merchandise, open resources).',
    )
    programme = models.ForeignKey(
        'learning.Programme', on_delete=models.CASCADE, null=True, blank=True,
        related_name='shop_products',
        help_text='Narrows the item to one programme at that institution. Blank = all of them.',
    )
    module = models.ForeignKey(
        'learning.ProgrammeModule', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='shop_products',
    )

    UNLOCKS_MODULE = 'module'
    UNLOCKS_PROGRAMME = 'programme'
    UNLOCKS_CHOICES = [
        ('', 'Nothing'),
        (UNLOCKS_MODULE, 'Its module'),
        (UNLOCKS_PROGRAMME, 'Every module in its programme'),
    ]
    shop_listed = models.BooleanField(
        'Show in the shop', default=True, db_index=True,
        help_text='Off = only staff can use it, on invoices and estimates.')
    unit = models.CharField(
        max_length=20, blank=True,
        help_text='What one of it is, printed after the price: "month", "hour", "each".')
    unlocks = models.CharField(
        'Paying for it unlocks', max_length=10, choices=UNLOCKS_CHOICES, blank=True,
        help_text='Opens the module(s) for the customer when the invoice is paid, '
                  'one month per unit invoiced.')
    price_from_modules = models.BooleanField(
        'Price follows the module fees', default=False,
        help_text='Keep the price equal to the module fee (or the sum of the programme\'s '
                  'module fees). Untick to set your own price.')

    digital_file = models.FileField(
        upload_to='shop/downloads/', blank=True, null=True,
        help_text='The zip or document the buyer downloads once the order is paid.',
        validators=v.validate_download,
    )

    @property
    def is_digital(self):
        """True when there is a file to deliver rather than something to post."""
        if self.digital_file:
            return True
        if self.pk is None:
            return False
        return self.files.exists()

    def deliverables(self):
        """Every file a buyer receives, as ``(label, fieldfile, file_id)``.

        ``file_id`` is ``None`` for the legacy single ``digital_file``, which is
        still honoured so nothing uploaded before multi-file support is lost.
        """
        out = []
        if self.digital_file:
            out.append((self.digital_file.name.rsplit('/', 1)[-1], self.digital_file, None))
        if self.pk is not None:
            for f in self.files.all():
                out.append((f.label, f.file, f.pk))
        return out

    LEVEL_CHOICES = [
        ('', '—'), ('beginner', 'Beginner'), ('intermediate', 'Intermediate'),
        ('advanced', 'Advanced'), ('all', 'All levels'),
    ]
    summary = models.CharField(max_length=300, blank=True, help_text='Short one-line tagline shown on the card.')
    educator = models.ForeignKey(
        'accounts.Person', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='taught_products', help_text='The instructor for this item.',
    )
    duration = models.CharField(max_length=60, blank=True, help_text='e.g. "12 weeks", "3 Months".')
    level = models.CharField(max_length=12, choices=LEVEL_CHOICES, blank=True)
    rating = models.DecimalField(max_digits=3, decimal_places=2, default=0,
                                 help_text='Average review rating (0–5), recomputed from reviews.')
    rating_count = models.PositiveIntegerField(default=0)
    students_count = models.PositiveIntegerField(default=0, help_text='Enrolled / sold count (display).')
    featured = models.BooleanField(default=False)

    card_tag = models.CharField(
        'Card tag', max_length=60, blank=True,
        help_text='Small pill at the top of the store card, e.g. "Test 3 preparation bundle". '
                  'Blank = the category.')
    card_headline = models.CharField(
        'Card headline', max_length=120, blank=True,
        help_text='Big text on the store card. Put *asterisks* round the word(s) to show in green, '
                  'e.g. "CGAU Test 3. Win the first *fifteen*". Blank = the item name.')
    card_price_label = models.CharField(
        'Price badge label', max_length=30, blank=True,
        help_text='Small line above the price on the card, e.g. "Full bundle".')
    card_points = models.TextField(
        'Card bullet points', blank=True,
        help_text='Up to three short lines, one per line, shown ticked on the card. '
                  'Blank = drawn from the module, files and access period.')
    card_cta = models.CharField(
        'Card button text', max_length=40, blank=True,
        help_text='e.g. "Get the bundle". Blank = "Buy now" / "Book now".')
    card_use_image = models.BooleanField(
        'Use the cover image as the store card', default=False,
        help_text='On = the uploaded cover image is the card (for finished artwork). '
                  'Off = the card is drawn in the Blueprint style and the image shows on the item page.')

    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='active')
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='+',
    )

    class Meta:
        ordering = ['name']

    def __str__(self):
        """The product name."""
        return self.name

    @property
    def is_service(self):
        """Whether ``kind`` is service."""
        return self.kind == self.KIND_SERVICE

    @property
    def is_shipped(self):
        """Whether the item is delivered by courier."""
        return self.fulfilment == self.FULFIL_SHIPPED

    @property
    def is_booking(self):
        """Whether buying it books a 1-on-1 session."""
        return self.fulfilment == self.FULFIL_BOOKING

    @property
    def has_discount(self):
        """Whether the product carries its own sale percentage on a non-zero price."""
        return bool(self.discount_percent) and self.discount_percent > 0 and self.price > 0

    def sale_price_for(self, unit_price):
        """``unit_price`` less this product's own discount %, to the cent."""
        unit_price = Decimal(str(unit_price or 0))
        if not self.has_discount:
            return unit_price
        off = unit_price * Decimal(str(self.discount_percent)) / Decimal('100')
        return (unit_price - off).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

    @property
    def sale_price(self):
        """The price after the product's own discount."""
        return self.sale_price_for(self.price)

    @property
    def lengths(self):
        """Bookable session lengths in minutes, sorted, never empty."""
        out = []
        for part in (self.session_lengths or '').split(','):
            try:
                value = int(part.strip())
            except ValueError:
                continue
            if 15 <= value <= 480 and value not in out:
                out.append(value)
        return sorted(out) or [60]

    @property
    def slot_step_minutes(self):
        """How often a session may start: on the hour for an hour-only service
        (the hourly 1-on-1 on the week calendar), else on the half hour."""
        return 60 if self.lengths == [60] else 30

    @property
    def active_rates(self):
        """The product's active educator rates (``[]`` before it is saved)."""
        if self.pk is None:
            return []
        return [rate for rate in self.educator_rates.all() if rate.is_active]

    @property
    def lowest_rate(self):
        """The cheapest active hourly rate, or ``price`` when there are none."""
        rates = [rate.hourly_rate for rate in self.active_rates]
        return min(rates) if rates else self.price

    @property
    def active_variants(self):
        """The product's active variants (``[]`` before it is saved)."""
        if self.pk is None:
            return []
        return [variant for variant in self.variants.all() if variant.is_active]

    @property
    def in_stock(self):
        """False only when stock is tracked and nothing is left to sell."""
        if not self.track_stock:
            return True
        variants = self.active_variants
        if variants:
            return any(variant.stock > 0 for variant in variants)
        return self.stock > 0

    @property
    def is_bookable(self):
        """Whether it is a booking with at least one active educator rate."""
        return self.is_booking and bool(self.active_rates)

    @property
    def max_quantity(self):
        """How many one buyer may put in a cart: one of a file, one of a slot."""
        if self.fulfilment in (self.FULFIL_DIGITAL, self.FULFIL_BOOKING):
            return 1
        return None

    @property
    def educator_name(self):
        """The linked educator's name, or ``''``."""
        if self.educator:
            return str(self.educator)
        return ''

    @property
    def star_range(self):
        """List for rendering star icons: 'full' / 'half' / 'empty' per star."""
        value = float(self.rating or 0)
        stars = []
        for position in range(1, 6):
            if value >= position:
                stars.append('full')
            elif value >= position - 0.5:
                stars.append('half')
            else:
                stars.append('empty')
        return stars

    def recompute_rating(self):
        """Recalculate the cached rating + count from this product's reviews."""
        aggregated = self.reviews.aggregate(avg=models.Avg('rating'), n=models.Count('id'))
        self.rating = round(aggregated['avg'] or 0, 2)
        self.rating_count = aggregated['n'] or 0
        self.save(update_fields=['rating', 'rating_count', 'updated_at'])

    def save(self, *args, **kwargs):
        """Generate a SKU if blank and keep ``kind`` in step with ``fulfilment``.

        Only a fee (no delivery) is free to be either kind; the rest follow their
        fulfilment.
        """
        if not self.sku:
            self.sku = f'SKU-{uuid.uuid4().hex[:8].upper()}'
        if self.fulfilment == self.FULFIL_BOOKING:
            self.kind = self.KIND_SERVICE
        elif self.fulfilment != self.FULFIL_NONE:
            self.kind = self.KIND_PRODUCT
        super().save(*args, **kwargs)

    def get_absolute_url(self):
        """The product's storefront page."""
        return reverse('shop:product-detail', args=[self.pk])

    @property
    def card(self):
        """Everything the Blueprint-style store card prints, with the blanks
        filled in from the rest of the record. Cached per instance; it reads
        only related rows the storefront already selects (and ``files`` when
        prefetched)."""
        cached = getattr(self, '_card_cache', None)
        if cached is None:
            cached = self._card_cache = self._build_card()
        return cached

    def _build_card(self):
        """Work out everything the store card draws (see :attr:`card`).

        The top-right meta reads like "MILPARK PGDA · CGAU" and the caption under the
        card like "PGDA · CGAU". In the headline, ``*accent*`` marks the green words;
        left blank, the name is used with its last word in green. The trailing full
        stop is drawn as the lime dot.
        """
        programme, module, institution = self.programme, self.module, self.institution
        prog_code = programme.short_code if programme else ''
        mod_code = module.code if module else ''

        lead = ' '.join(x for x in [institution.code if institution else '', prog_code] if x)
        meta = ' · '.join(x for x in [lead, mod_code] if x)
        caption = ' · '.join(x for x in [
            prog_code or (institution.code if institution else ''),
            mod_code or (self.category.name if self.category else ''),
        ] if x) or self.get_fulfilment_display()

        tag = self.card_tag or (self.category.name if self.category else self.get_fulfilment_display())

        text = (self.card_headline or self.name or '').strip().rstrip('.').strip()
        match = re.search(r'\*([^*]+)\*', text)
        if match:
            head = (text[:match.start()].strip(), match.group(1).strip(), text[match.end():].strip().rstrip('.'))
        else:
            words = text.rsplit(' ', 1)
            head = (words[0], words[1], '') if len(words) == 2 else ('', text, '')
        head = tuple(part.replace('*', '') for part in head)

        if self.card_price_label:
            price_label = self.card_price_label
        elif self.is_booking:
            price_label = 'From'
        elif self.has_discount:
            price_label = 'Sale'
        else:
            price_label = {self.FULFIL_DIGITAL: 'Download', self.FULFIL_SHIPPED: 'Delivered'}.get(
                self.fulfilment, '')
        if self.is_booking:
            price_note = 'per hour'
        elif self.unit:
            price_note = f'per {self.unit}'
        else:
            price_note = ''

        blurb = self.summary or ''
        if not blurb and self.description:
            blurb = Truncator(' '.join(self.description.split())).words(26)

        points = [line.strip(' -•*\t') for line in (self.card_points or '').splitlines()]
        points = [p for p in points if p][:3]
        if not points:
            points = self._derived_points()

        if self.card_cta:
            cta = self.card_cta
        elif self.is_booking:
            cta = 'Book now'
        elif self.active_variants:
            cta = 'Choose your size'
        elif not self.in_stock:
            cta = 'View'
        else:
            cta = 'Buy now'

        return {
            'meta': meta, 'caption': caption, 'tag': tag,
            'headline': self._headline_html(*head),
            'price_label': price_label, 'price_note': price_note,
            'blurb': blurb, 'points': points, 'cta': cta,
            'use_image': bool(self.card_use_image and self.image),
        }

    @staticmethod
    def _headline_html(lead, accent, tail):
        """``CGAU Test 3. Win the first`` + ``fifteen`` → escaped HTML with
        each sentence-ending full stop and the closing one as the lime dot,
        and the accent in green italics."""
        def dots(text):
            """Escape ``text`` and turn each sentence-ending full stop into the lime dot."""
            return re.sub(r'\.(?=\s|$)', '<span class="tb-dot">.</span>', str(escape(text)))
        parts = []
        if lead:
            parts.append(dots(lead))
        if accent:
            parts.append(f'<em>{dots(accent)}</em>')
        if tail:
            parts.append(dots(tail))
        return mark_safe(' '.join(parts) + '<span class="tb-dot">.</span>')

    def _derived_points(self):
        """Up to three card bullet points drawn from the record: module, session lengths, files, delivery, access period, duration, educator."""
        out = []
        if self.module:
            out.append(f'{self.module.code} · {self.module.display_name}')
        if self.is_booking:
            lengths = self.lengths
            longest = lengths[-1]
            longest = f'{longest // 60} hour{"s" if longest > 60 else ""}' if longest % 60 == 0 else f'{longest} min'
            out.append(f'{lengths[0]} min to {longest}' if len(lengths) > 1 else longest)
            out.append('Live on Microsoft Teams')
        files = None
        cache = getattr(self, '_prefetched_objects_cache', {})
        if 'files' in cache:
            files = len(cache['files']) + (1 if self.digital_file else 0)
        elif self.digital_file:
            files = 1
        if files:
            out.append(f'{files} file{"s" if files != 1 else ""}, yours to keep')
        elif self.fulfilment == self.FULFIL_DIGITAL:
            out.append('Instant download after payment')
        if self.fulfilment == self.FULFIL_SHIPPED:
            out.append('Courier or Pudo locker delivery')
        if self.validity_days:
            out.append(f'Access for {self.validity_days} days')
        if self.duration:
            out.append(self.duration)
        if self.educator_id:
            out.append(f'With {self.educator}')
        return out[:3]


class ProductFile(TimeStampedModel):
    """One downloadable file of a digital product — a pack is often several."""

    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name='files')
    file = models.FileField(upload_to='shop/downloads/',
                            validators=v.validate_download)
    title = models.CharField(max_length=200, blank=True,
                             help_text='Shown to the buyer. Blank = the file name.')
    original_name = models.CharField(max_length=255, blank=True)
    size = models.PositiveBigIntegerField(default=0)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ['order', 'id']

    def __str__(self):
        """The file's label."""
        return self.label

    @property
    def label(self):
        """Display name: the title, else the uploaded name, else the stored file name."""
        return self.title or self.original_name or self.file.name.rsplit('/', 1)[-1]

    @property
    def extension(self):
        """Lower-case file extension."""
        return (self.original_name or self.file.name).rsplit('.', 1)[-1].lower()


class ProductImage(TimeStampedModel):
    """A gallery picture. The product's own ``image`` stays the cover."""

    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name='gallery')
    image = models.ImageField(upload_to='shop/gallery/', validators=v.validate_image)
    alt = models.CharField(max_length=200, blank=True)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ['order', 'id']

    def __str__(self):
        """The alt text, or a generic label."""
        return self.alt or f'{self.product} image'


class ProductVariant(TimeStampedModel):
    """A size or colour of a shipped item — the hoodie in L, in navy.

    Stock lives here when the product has variants, because "we have hoodies"
    is not the same as "we have a large".
    """

    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name='variants')
    name = models.CharField(max_length=60, help_text='e.g. "L", "Navy / XL".')
    sku = models.CharField('SKU', max_length=40, blank=True)
    price_adjustment = models.DecimalField(
        max_digits=10, decimal_places=2, default=0,
        help_text='Added to the product price for this option (negative to reduce).')
    stock = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ['order', 'id']

    def __str__(self):
        """Product and variant name."""
        return f'{self.product.name} — {self.name}'

    @property
    def unit_price(self):
        """The product price plus this variant's adjustment."""
        return (self.product.price or Decimal('0')) + (self.price_adjustment or Decimal('0'))


class DownloadLog(models.Model):
    """Who downloaded which purchased file, and when — the audit trail that
    makes "unlimited downloads" safe to offer."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                             related_name='shop_downloads')
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name='download_logs')
    file = models.ForeignKey(ProductFile, on_delete=models.SET_NULL, null=True, blank=True,
                             related_name='download_logs')
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']


class ProductReview(TimeStampedModel):
    """A buyer's star rating + comment on a product/course."""

    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name='reviews')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='product_reviews')
    rating = models.PositiveSmallIntegerField(default=5, help_text='1–5 stars.')
    comment = models.TextField(blank=True)

    class Meta:
        ordering = ['-created_at']
        unique_together = ('product', 'user')

    def __str__(self):
        """Label shown in the admin."""
        return f'{self.user} · {self.product} ({self.rating}★)'


class Order(TimeStampedModel):
    """A buyer's order — first their cart, then (after checkout) what they are billed.

    * ``checked_out`` — an order starts as the buyer's cart; checkout converts it
      into an invoice and flips this flag, so the next add-to-cart starts a fresh
      basket.
    * ``subtotal`` / discount / shipping / VAT fields — a snapshot of the priced
      quote, frozen at checkout (see :mod:`apps.shop.checkout`). While the order
      is still a cart these stay zero and the cart is priced live.
    * ``paid_processed_at`` — set once the paid-order side effects (stock, coupon
      count, notifications) have run, so a second settlement callback cannot run
      them twice.
    * Delivery (``delivery_method``, ``ship_to``, ``delivery_label``) — frozen at
      checkout for orders with shipped items. ``ship_to`` is a snapshot, not a
      link to the address book: editing a saved address later must not move a
      parcel that is already on its way.
    """

    STATUS_PENDING = 'pending'
    STATUS_PAID = 'paid'
    STATUS_CANCELLED = 'cancelled'
    STATUS_REFUNDED = 'refunded'
    ORDER_STATUS = [
        (STATUS_PENDING, 'Pending'),
        (STATUS_PAID, 'Paid'),
        (STATUS_CANCELLED, 'Cancelled'),
        (STATUS_REFUNDED, 'Refunded'),
    ]

    order_no = models.CharField(max_length=40, unique=True, blank=True)
    buyer = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='orders',
    )
    status = models.CharField(max_length=10, choices=ORDER_STATUS, default=STATUS_PENDING)
    total = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    note = models.TextField(blank=True)
    checked_out = models.BooleanField(default=False)

    subtotal = models.DecimalField(max_digits=12, decimal_places=2, default=0,
                                   help_text='Sum of lines before discounts.')
    discount_total = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    shipping_total = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    tax_total = models.DecimalField(max_digits=12, decimal_places=2, default=0,
                                    help_text='VAT contained in the total (prices are VAT-inclusive).')
    coupon = models.ForeignKey('Discount', on_delete=models.SET_NULL, null=True, blank=True,
                               related_name='orders')
    paid_processed_at = models.DateTimeField(null=True, blank=True)

    DELIVERY_DOOR = 'door'
    DELIVERY_LOCKER = 'locker'
    DELIVERY_CHOICES = [(DELIVERY_DOOR, 'Door-to-door'), (DELIVERY_LOCKER, 'Pudo locker')]
    delivery_method = models.CharField(max_length=10, choices=DELIVERY_CHOICES, blank=True)
    ship_to = models.JSONField(default=dict, blank=True)
    delivery_label = models.CharField(max_length=160, blank=True,
                                      help_text='What the buyer chose, e.g. "Courier Guy Economy".')

    @property
    def needs_delivery(self):
        """Whether any line is a shipped item."""
        return any(item.product.is_shipped for item in self.items.all())

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        """The order number."""
        return self.order_no

    def save(self, *args, **kwargs):
        """Assign a unique ``ORD-…`` order number on first save."""
        if not self.order_no:
            self.order_no = f'ORD-{uuid.uuid4().hex[:10].upper()}'
        super().save(*args, **kwargs)

    @classmethod
    def get_cart(cls, user):
        """Return the user's active cart (an un-checked-out order), creating one."""
        cart = cls.objects.filter(buyer=user, checked_out=False).order_by('-created_at').first()
        if cart is None:
            cart = cls.objects.create(buyer=user)
        return cart

    @property
    def item_count(self):
        """Total quantity across the lines."""
        return sum(item.quantity for item in self.items.all())

    @property
    def amount_paid(self):
        """Sum of completed payments."""
        aggregated = self.payments.filter(status=Payment.STATUS_COMPLETED).aggregate(
            total=models.Sum('amount'))
        return aggregated['total'] or 0

    @property
    def balance(self):
        """What is still owed on the order."""
        return (self.total or 0) - self.amount_paid

    @property
    def is_paid(self):
        """Whether the order is marked paid or its payments cover a non-zero total."""
        return self.status == self.STATUS_PAID or (self.total > 0 and self.balance <= 0)

    def recalc_total(self):
        """Total payable: the (net) lines plus delivery."""
        aggregated = self.items.aggregate(total=models.Sum('line_total'))
        self.total = (aggregated['total'] or 0) + (self.shipping_total or 0)
        self.save(update_fields=['total', 'updated_at'])

    def get_absolute_url(self):
        """The buyer's order page."""
        return reverse('shop:order-detail', args=[self.pk])


class OrderItem(models.Model):
    """One line of an order: a product (and variant) at a quantity.

    ``discount_amount`` and ``discount_label`` are frozen at checkout from the
    priced quote; they stay zero while the order is a cart. ``expiry_date`` is
    derived from a service's ``validity_days``.
    """

    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name='items')
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name='order_items')
    variant = models.ForeignKey(ProductVariant, on_delete=models.PROTECT, null=True, blank=True,
                                related_name='order_items')
    quantity = models.PositiveIntegerField(default=1)
    unit_price = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    discount_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    discount_label = models.CharField(max_length=160, blank=True)
    line_total = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    """Net: ``unit_price × quantity − discount_amount``."""
    expiry_date = models.DateField(null=True, blank=True)

    def __str__(self):
        """Quantity and description."""
        return f'{self.quantity} x {self.description}'

    @property
    def description(self):
        """Product name, with the variant when there is one."""
        if self.variant_id:
            return f'{self.product.name} — {self.variant.name}'
        return self.product.name

    @property
    def gross_total(self):
        """``unit_price × quantity`` before discount."""
        return (self.unit_price or 0) * (self.quantity or 0)

    @property
    def is_expired(self):
        """Whether the line's validity has run out."""
        return bool(self.expiry_date and self.expiry_date < timezone.now().date())

    def save(self, *args, **kwargs):
        """Default the unit price and expiry from the product, and recompute the line total."""
        if self.product_id:
            if not self.unit_price:
                self.unit_price = self.variant.unit_price if self.variant_id else self.product.price
            if not self.expiry_date and self.product.is_service and self.product.validity_days:
                self.expiry_date = timezone.now().date() + timedelta(days=self.product.validity_days)
        self.line_total = self.gross_total - (self.discount_amount or 0)
        super().save(*args, **kwargs)


class Payment(TimeStampedModel):
    """Money received against an order (any method); completed payments settle it."""

    STATUS_PENDING = 'pending'
    STATUS_COMPLETED = 'completed'
    STATUS_FAILED = 'failed'
    PAYMENT_STATUS = [
        (STATUS_PENDING, 'Pending'),
        (STATUS_COMPLETED, 'Completed'),
        (STATUS_FAILED, 'Failed'),
    ]
    METHOD_CHOICES = [
        ('cash', 'Cash'),
        ('card', 'Card'),
        ('bank', 'Bank Transfer'),
        ('online', 'Online'),
        ('cheque', 'Cheque'),
    ]

    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name='payments')
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    method = models.CharField(max_length=20, choices=METHOD_CHOICES, default='online')
    status = models.CharField(max_length=10, choices=PAYMENT_STATUS, default=STATUS_COMPLETED)
    reference = models.CharField(max_length=80, blank=True)
    paid_at = models.DateTimeField(default=timezone.now)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='+',
    )

    class Meta:
        ordering = ['-paid_at']

    def __str__(self):
        """Amount and order."""
        return f'{self.amount} for {self.order}'


class Discount(TimeStampedModel):
    """A price reduction: a promotion (no ``code``, applies automatically) or a
    coupon (``code`` typed at checkout).

    Blank targets apply to the whole cart; set targets bite only on matching
    lines — the same scoping rule as :mod:`apps.shop.scoping`.

    Discounts are created by admin / staff only (see ``shop.views.staff_required``).
    All scope fields blank means "the whole catalogue".
    """

    TYPE_PERCENT = 'percent'
    TYPE_AMOUNT = 'amount'
    TYPE_CHOICES = [(TYPE_PERCENT, 'Percentage off'), (TYPE_AMOUNT, 'Fixed amount off')]

    name = models.CharField(max_length=120, help_text='Shown to the buyer, e.g. "Launch week – 20% off".')
    code = models.CharField(
        max_length=32, blank=True, unique=True, null=True,
        help_text='Leave blank for an automatic promotion; set it to require a coupon code.')
    discount_type = models.CharField(max_length=8, choices=TYPE_CHOICES, default=TYPE_PERCENT)
    value = models.DecimalField(max_digits=8, decimal_places=2,
                                help_text='Percent (0–100) or a rand amount, per discount_type.')

    institution = models.ForeignKey('learning.Institution', on_delete=models.CASCADE,
                                    null=True, blank=True, related_name='discounts')
    programme = models.ForeignKey('learning.Programme', on_delete=models.CASCADE,
                                  null=True, blank=True, related_name='discounts')
    category = models.ForeignKey(ProductCategory, on_delete=models.CASCADE,
                                 null=True, blank=True, related_name='discounts')
    products = models.ManyToManyField(Product, blank=True, related_name='discounts',
                                      help_text='Specific items. Blank = anything else in scope.')

    starts_at = models.DateTimeField(null=True, blank=True)
    ends_at = models.DateTimeField(null=True, blank=True)
    max_uses = models.PositiveIntegerField(null=True, blank=True, help_text='Blank = unlimited.')
    times_used = models.PositiveIntegerField(default=0)
    min_spend = models.DecimalField(max_digits=12, decimal_places=2, default=0,
                                    help_text='Cart total the discount needs before it applies.')
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                   null=True, blank=True, related_name='+')

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        """Name, with the code for a coupon."""
        return f'{self.name} ({self.code})' if self.code else self.name

    def save(self, *args, **kwargs):
        """Normalise the code to upper case, or to ``NULL`` for a promotion.

        ``unique=True`` on a blank string collides between promotions; ``NULL`` does not.
        """
        if not self.code:
            self.code = None
        else:
            self.code = self.code.strip().upper()
        super().save(*args, **kwargs)

    @property
    def is_coupon(self):
        """Whether the discount needs a code typed at checkout."""
        return bool(self.code)

    @property
    def is_live(self):
        """Active, inside its window, and not used up."""
        now = timezone.now()
        if not self.is_active:
            return False
        if self.starts_at and now < self.starts_at:
            return False
        if self.ends_at and now > self.ends_at:
            return False
        if self.max_uses is not None and self.times_used >= self.max_uses:
            return False
        return True

    def covers(self, product):
        """True when this discount applies to ``product``."""
        if self.products.exists():
            return self.products.filter(pk=product.pk).exists()
        if self.category_id and product.category_id != self.category_id:
            return False
        if self.institution_id and product.institution_id != self.institution_id:
            return False
        if self.programme_id and product.programme_id != self.programme_id:
            return False
        return True

    def amount_for(self, subtotal):
        """The rand value this discount takes off ``subtotal``, never below zero."""
        from decimal import Decimal
        if subtotal <= 0:
            return Decimal('0')
        if self.discount_type == self.TYPE_PERCENT:
            off = (subtotal * self.value) / Decimal('100')
        else:
            off = self.value
        return min(max(off, Decimal('0')), subtotal).quantize(Decimal('0.01'))


class EducatorRate(TimeStampedModel):
    """What one educator charges per hour for one 1-on-1 service. Staff-set."""

    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name='educator_rates')
    educator = models.ForeignKey('accounts.Person', on_delete=models.CASCADE,
                                 related_name='session_rates')
    hourly_rate = models.DecimalField(max_digits=10, decimal_places=2,
                                      validators=[MinValueValidator(0)])
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['hourly_rate', 'id']
        constraints = [models.UniqueConstraint(fields=['product', 'educator'],
                                               name='uniq_rate_per_educator_per_service')]

    def __str__(self):
        """Label shown in the admin."""
        return f'{self.educator} · {self.product} · R{self.hourly_rate}/h'

    def price_for(self, minutes):
        """Price of a ``minutes``-long session at this rate, to the cent."""
        return (Decimal(str(self.hourly_rate)) * Decimal(minutes) / Decimal(60)).quantize(
            Decimal('0.01'), rounding=ROUND_HALF_UP)


class EducatorAvailability(models.Model):
    """A weekly window in which an educator takes 1-on-1s (e.g. Tue 14:00–19:00)."""

    WEEKDAYS = [(0, 'Monday'), (1, 'Tuesday'), (2, 'Wednesday'), (3, 'Thursday'),
                (4, 'Friday'), (5, 'Saturday'), (6, 'Sunday')]

    educator = models.ForeignKey('accounts.Person', on_delete=models.CASCADE,
                                 related_name='availability')
    weekday = models.PositiveSmallIntegerField(choices=WEEKDAYS)
    start_time = models.TimeField()
    end_time = models.TimeField()

    class Meta:
        ordering = ['educator', 'weekday', 'start_time']
        verbose_name_plural = 'Educator availability'

    def __str__(self):
        """Label shown in the admin."""
        return f'{self.educator} · {self.get_weekday_display()} {self.start_time:%H:%M}–{self.end_time:%H:%M}'


class EducatorTimeOff(models.Model):
    """A stretch an educator is away — leave, exam invigilation, a conference."""

    educator = models.ForeignKey('accounts.Person', on_delete=models.CASCADE, related_name='time_off')
    start = models.DateTimeField()
    end = models.DateTimeField()
    reason = models.CharField(max_length=160, blank=True)

    class Meta:
        ordering = ['start']
        verbose_name_plural = 'Educator time off'

    def __str__(self):
        """Label shown in the admin."""
        return f'{self.educator} away {self.start:%d %b} – {self.end:%d %b}'


class Booking(TimeStampedModel):
    """One 1-on-1 session a student has chosen.

    Life: **held** (in a cart, the slot kept for them for a few minutes) →
    **confirmed** (paid; the live session exists) → **completed**, or
    **cancelled**. A hold that runs out is **expired** and frees the slot.
    """

    STATUS_HELD = 'held'
    STATUS_CONFIRMED = 'confirmed'
    STATUS_COMPLETED = 'completed'
    STATUS_CANCELLED = 'cancelled'
    STATUS_EXPIRED = 'expired'
    STATUS_CHOICES = [
        (STATUS_HELD, 'Held — awaiting payment'),
        (STATUS_CONFIRMED, 'Confirmed'),
        (STATUS_COMPLETED, 'Completed'),
        (STATUS_CANCELLED, 'Cancelled'),
        (STATUS_EXPIRED, 'Expired'),
    ]
    BLOCKING = (STATUS_HELD, STATUS_CONFIRMED)
    """Statuses that occupy the educator's time."""

    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name='bookings')
    educator = models.ForeignKey('accounts.Person', on_delete=models.PROTECT,
                                 related_name='bookings_taught')
    student = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                                related_name='session_bookings')
    start = models.DateTimeField(db_index=True)
    end = models.DateTimeField()
    minutes = models.PositiveSmallIntegerField()
    price = models.DecimalField(max_digits=10, decimal_places=2)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_HELD,
                              db_index=True)
    hold_expires_at = models.DateTimeField(null=True, blank=True)
    order_item = models.OneToOneField(OrderItem, on_delete=models.SET_NULL, null=True, blank=True,
                                      related_name='booking')
    meeting = models.ForeignKey('communication.MeetingRoom', on_delete=models.SET_NULL,
                                null=True, blank=True, related_name='bookings')
    reschedule_count = models.PositiveSmallIntegerField(default=0)
    note = models.TextField(blank=True, help_text='What the student would like to cover.')
    cancelled_reason = models.CharField(max_length=255, blank=True)
    cancelled_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                     null=True, blank=True, related_name='+')

    PAID_CART = 'cart'
    PAID_HOURS = 'hours'
    PAID_ACCOUNT = 'account'
    PAID_BY_CHOICES = [(PAID_CART, 'Shop checkout'), (PAID_HOURS, 'Prepaid hours'),
                       (PAID_ACCOUNT, 'On account (invoiced)')]
    """How the session is paid (one-on-one track, :mod:`apps.shop.one_on_one`):
    through the shop cart (anyone), with hours the student bought in advance, or
    on account — staff scheduled it and sent an invoice the student settles (they
    may still attend first).
    """
    paid_by = models.CharField(max_length=8, choices=PAID_BY_CHOICES, default=PAID_CART)
    programme_module = models.ForeignKey(
        'learning.ProgrammeModule', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='one_on_one_bookings', help_text='The module the session is for.')
    invoice = models.ForeignKey(
        'finance.Invoice', on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
        help_text='The invoice for a session booked on account.')
    overtime_invoice = models.ForeignKey(
        'finance.Invoice', on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
        help_text='The extra charge raised when the session ran long.')
    measured_minutes = models.PositiveSmallIntegerField(
        null=True, blank=True, help_text='How long the call really ran (Teams attendance).')
    invoice_resent_at = models.DateTimeField(null=True, blank=True)
    overtime_checked_at = models.DateTimeField(null=True, blank=True)
    booked_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                  null=True, blank=True, related_name='+')

    class Meta:
        ordering = ['start']
        indexes = [models.Index(fields=['educator', 'start'])]

    def __str__(self):
        """Label shown in the admin."""
        return f'{self.student} with {self.educator} · {self.start:%d %b %H:%M}'

    @property
    def label(self):
        """Local day and time span, e.g. ``Mon 3 Nov, 14:00–15:00``."""
        local = timezone.localtime(self.start)
        return f'{local:%a %-d %b, %H:%M}–{timezone.localtime(self.end):%H:%M}'

    @property
    def is_upcoming(self):
        """Whether the booking is confirmed and still to come."""
        return self.status == self.STATUS_CONFIRMED and self.start > timezone.now()

    def can_reschedule(self, now=None):
        """Whether a confirmed booking is still before the product's reschedule cut-off."""
        now = now or timezone.now()
        cutoff = self.start - timedelta(hours=self.product.reschedule_cutoff_hours)
        return self.status == self.STATUS_CONFIRMED and now < cutoff


class CreditEntry(models.Model):
    """Account credit: one line of a student's ledger.

    Positive when credit is granted (a session staff or the educator cancelled),
    negative when it is spent at checkout. The balance is the sum — a ledger
    rather than a stored number, so every rand can be traced to its reason.
    """

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                             related_name='credit_entries')
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    reason = models.CharField(max_length=255)
    booking = models.ForeignKey(Booking, on_delete=models.SET_NULL, null=True, blank=True,
                                related_name='credit_entries')
    invoice = models.ForeignKey('finance.Invoice', on_delete=models.SET_NULL, null=True, blank=True,
                                related_name='credit_entries')
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                   null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name_plural = 'Credit entries'

    def __str__(self):
        """Label shown in the admin."""
        return f'{self.user} {self.amount:+} · {self.reason}'

    @classmethod
    def balance_for(cls, user):
        """``user``'s account-credit balance (never negative)."""
        total = cls.objects.filter(user=user).aggregate(total=models.Sum('amount'))['total']
        return max(total or Decimal('0'), Decimal('0'))


class HourPurchase(TimeStampedModel):
    """One-on-one hours a student is buying: one invoice, a line per module.

    The hours reach the student's balance (:class:`SessionHours`) only when the
    invoice is paid — :func:`apps.shop.one_on_one.credit_paid_purchase`.
    """

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                             related_name='hour_purchases')
    invoice = models.OneToOneField('finance.Invoice', on_delete=models.SET_NULL, null=True,
                                   blank=True, related_name='hour_purchase')
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                   null=True, blank=True, related_name='+')
    credited_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        """Label shown in the admin."""
        return f'{self.user} · hours · {self.invoice.number if self.invoice else "no invoice"}'


class HourPurchaseLine(models.Model):
    """Hours of one module on an hour purchase, at a unit price."""

    purchase = models.ForeignKey(HourPurchase, on_delete=models.CASCADE, related_name='lines')
    programme_module = models.ForeignKey('learning.ProgrammeModule', on_delete=models.PROTECT,
                                         related_name='+')
    hours = models.PositiveSmallIntegerField(validators=[MinValueValidator(1)])
    unit_price = models.DecimalField(max_digits=10, decimal_places=2)

    def __str__(self):
        """Label shown in the admin."""
        return f'{self.programme_module.code} × {self.hours} h'


class SessionHours(models.Model):
    """The one-on-one hours ledger: one line per change to a student's balance
    on one module. Bought hours are positive; a booked session is negative; a
    cancellation that hands the time back is positive again. The balance is the
    sum, so every hour can be traced to its reason."""

    KIND_PURCHASE = 'purchase'
    KIND_BOOKING = 'booking'
    KIND_RETURN = 'return'
    KIND_ADJUST = 'adjust'
    KIND_CHOICES = [(KIND_PURCHASE, 'Bought'), (KIND_BOOKING, 'Session booked'),
                    (KIND_RETURN, 'Session cancelled — hours back'), (KIND_ADJUST, 'Adjusted by staff')]

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                             related_name='session_hours')
    programme_module = models.ForeignKey('learning.ProgrammeModule', on_delete=models.CASCADE,
                                         related_name='+')
    hours = models.DecimalField(max_digits=6, decimal_places=2)
    kind = models.CharField(max_length=10, choices=KIND_CHOICES)
    reason = models.CharField(max_length=255, blank=True)
    booking = models.ForeignKey(Booking, on_delete=models.SET_NULL, null=True, blank=True,
                                related_name='hour_entries')
    purchase = models.ForeignKey(HourPurchase, on_delete=models.SET_NULL, null=True, blank=True,
                                 related_name='entries')
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                   null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name_plural = 'Session hours'

    def __str__(self):
        """Label shown in the admin."""
        return f'{self.user} {self.hours:+} h · {self.programme_module_id} · {self.kind}'


PROVINCES = [
    ('GP', 'Gauteng'), ('WC', 'Western Cape'), ('KZN', 'KwaZulu-Natal'), ('EC', 'Eastern Cape'),
    ('FS', 'Free State'), ('LP', 'Limpopo'), ('MP', 'Mpumalanga'), ('NW', 'North West'),
    ('NC', 'Northern Cape'),
]


class Address(TimeStampedModel):
    """A delivery address in a buyer's address book."""

    TYPE_CHOICES = [('residential', 'Home'), ('business', 'Work / business')]

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='addresses')
    recipient = models.CharField('Recipient name', max_length=120)
    phone = models.CharField('Mobile number', max_length=30,
                             help_text='The courier SMSes delivery updates to this number.')
    street_address = models.CharField(max_length=200, help_text='Street number and name.')
    complex = models.CharField('Unit / complex / building', max_length=120, blank=True)
    suburb = models.CharField(max_length=100)
    city = models.CharField(max_length=100)
    province = models.CharField(max_length=3, choices=PROVINCES)
    postal_code = models.CharField(max_length=4)
    address_type = models.CharField(max_length=12, choices=TYPE_CHOICES, default='residential')
    company = models.CharField(max_length=120, blank=True)
    is_default = models.BooleanField(default=False)

    class Meta:
        ordering = ['-is_default', '-updated_at']
        verbose_name_plural = 'Addresses'

    def __str__(self):
        """The address on one line, with recipient."""
        return f'{self.recipient}, {self.street_address}, {self.suburb}, {self.city} {self.postal_code}'

    @property
    def one_line(self):
        """The street address on one line, blanks skipped."""
        parts = [self.complex, self.street_address, self.suburb, self.city, self.postal_code]
        return ', '.join(p for p in parts if p)

    def snapshot(self):
        """The address as a dict, frozen onto an order as ``ship_to``."""
        return {'recipient': self.recipient, 'phone': self.phone, 'street_address': self.street_address,
                'complex': self.complex, 'suburb': self.suburb, 'city': self.city,
                'province': self.province, 'postal_code': self.postal_code,
                'address_type': self.address_type, 'company': self.company}


class Shipment(TimeStampedModel):
    """Getting one order's parcel to the buyer.

    Created at checkout (``awaiting_payment``), ready to pack once paid
    (``ready``), ``booked`` when a courier waybill exists, then ``in_transit`` /
    ``delivered`` as tracking reports. With no courier API configured staff
    record the tracking number themselves (``carrier = manual``).
    """

    STATUS_AWAITING = 'awaiting_payment'
    STATUS_READY = 'ready'
    STATUS_BOOKED = 'booked'
    STATUS_IN_TRANSIT = 'in_transit'
    STATUS_DELIVERED = 'delivered'
    STATUS_FAILED = 'failed'
    STATUS_CANCELLED = 'cancelled'
    STATUS_CHOICES = [
        (STATUS_AWAITING, 'Awaiting payment'),
        (STATUS_READY, 'Ready to pack'),
        (STATUS_BOOKED, 'Courier booked'),
        (STATUS_IN_TRANSIT, 'On its way'),
        (STATUS_DELIVERED, 'Delivered'),
        (STATUS_FAILED, 'Delivery problem'),
        (STATUS_CANCELLED, 'Cancelled'),
    ]
    CARRIER_COURIER_GUY = 'courier_guy'
    CARRIER_MANUAL = 'manual'
    CARRIER_CHOICES = [(CARRIER_COURIER_GUY, 'The Courier Guy'), (CARRIER_MANUAL, 'Other / manual')]

    order = models.OneToOneField(Order, on_delete=models.CASCADE, related_name='shipment')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_AWAITING, db_index=True)
    method = models.CharField(max_length=10, choices=Order.DELIVERY_CHOICES)
    carrier = models.CharField(max_length=20, choices=CARRIER_CHOICES, default=CARRIER_COURIER_GUY)
    service_level = models.CharField(max_length=40, blank=True, help_text='Courier service code, e.g. ECO.')
    locker_code = models.CharField(max_length=60, blank=True)
    locker_name = models.CharField(max_length=200, blank=True)
    charged = models.DecimalField(max_digits=10, decimal_places=2, default=0,
                                  help_text='What the buyer paid for delivery.')
    courier_cost = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True,
                                       help_text='What the courier charged us (from the booking).')
    tracking_reference = models.CharField(max_length=80, blank=True, db_index=True)
    courier_shipment_id = models.CharField(max_length=80, blank=True)
    courier_name = models.CharField(max_length=80, blank=True, help_text='For manual shipments.')
    tracking_url = models.URLField(blank=True)
    events = models.JSONField(default=list, blank=True, help_text='Tracking history, newest last.')
    booked_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    last_tracked_at = models.DateTimeField(null=True, blank=True)
    booked_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                  null=True, blank=True, related_name='+')

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        """Label shown in the admin."""
        return f'{self.order.order_no} · {self.get_status_display()}'

    @property
    def is_open(self):
        """Whether the parcel is still ready, booked or in transit."""
        return self.status in (self.STATUS_READY, self.STATUS_BOOKED, self.STATUS_IN_TRANSIT)

    def log(self, status, message, when=None):
        """Append a timestamped tracking event (not saved)."""
        self.events = list(self.events or []) + [{
            'at': (when or timezone.now()).isoformat(), 'status': status, 'message': message}]
