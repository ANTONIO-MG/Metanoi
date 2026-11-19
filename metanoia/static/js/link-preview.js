/**
 * @file Link preview cards — the WhatsApp-style box under a message with a link.
 *
 * The server marks the first link of a message with data-preview-url
 * (apps.communication.links). Once that link scrolls into view this asks
 * /communication/link-preview/ for the card *this viewer* may see — LMS pages
 * only when they may open them, Teams links, or an external page's OpenGraph
 * title/description/image (fetched and cached server-side) — and draws it
 * after the text, inside the nearest [data-link-host] (or the link's parent).
 *
 * Every value is written with textContent / attributes, never innerHTML, and a
 * remote image is loaded with referrerpolicy="no-referrer". No card → the link
 * simply stays a link.
 *
 * Pages that render text themselves (the chat) call MetanoiaLinks.scan(el) after
 * drawing; server-rendered pages are scanned on load and on every HTMX swap.
 * Exports ``window.MetanoiaLinks = {scan}``. Requests are cached per URL and at
 * most three run at once; links are only fetched once they come within 200px
 * of the viewport (or at once without ``IntersectionObserver``).
 */
(function () {
  if (window.MetanoiaLinks) return;

  var ENDPOINT = '/communication/link-preview/';
  var cache = {};
  var inflight = 0, queue = [], MAX_INFLIGHT = 3;

  /** Start queued preview requests while fewer than ``MAX_INFLIGHT`` are running. */
  function pump() {
    while (inflight < MAX_INFLIGHT && queue.length) {
      var job = queue.shift();
      inflight++;
      fetch(ENDPOINT + '?url=' + encodeURIComponent(job.url),
            {credentials: 'same-origin', headers: {'X-Requested-With': 'XMLHttpRequest'}, appQuiet: true})
        .then(function (r) { return r.ok ? r.json() : {card: null}; })
        .then(function (d) { return (d && d.card) || null; }, function () { return null; })
        .then(job.resolve)
        .finally(function () { inflight--; pump(); });
    }
  }

  /**
   * The (cached) card for ``url``.
   * @param {string} url
   * @returns {Promise<Object|null>} The card, or null when there is none.
   */
  function cardFor(url) {
    if (!cache[url]) {
      cache[url] = new Promise(function (resolve) { queue.push({url: url, resolve: resolve}); pump(); });
    }
    return cache[url];
  }

  /**
   * Allow only same-site paths and http(s) URLs as the card's link target.
   * @param {*} href
   * @returns {string} The href, or '' when it is not safe to use.
   */
  function safeHref(href) {
    if (typeof href !== 'string') return '';
    if (href.charAt(0) === '/' && href.charAt(1) !== '/') return href;
    return /^https?:\/\//i.test(href) ? href : '';
  }

  /**
   * Build the card element with DOM APIs only (never innerHTML). Only https or
   * same-site images are shown; a broken image is removed.
   * @param {Object} card Card JSON from the endpoint.
   * @returns {HTMLElement}
   */
  function build(card) {
    var href = safeHref(card.href);
    var a = document.createElement(href ? 'a' : 'div');
    a.className = 'link-card link-card--' + String(card.kind || 'external').replace(/[^a-z-]/g, '');
    if (href) {
      a.href = href;
      if (!card.same_tab) { a.target = '_blank'; a.rel = 'noopener noreferrer nofollow ugc'; }
    }
    var img = typeof card.image === 'string' ? card.image : '';
    if (img && (/^https:\/\//i.test(img) || (img.charAt(0) === '/' && img.charAt(1) !== '/'))) {
      var pic = document.createElement('img');
      pic.className = 'link-card__img';
      pic.src = img; pic.alt = ''; pic.loading = 'lazy'; pic.decoding = 'async';
      pic.referrerPolicy = 'no-referrer';
      pic.addEventListener('error', function () { pic.remove(); });
      a.appendChild(pic);
    } else if (card.kind === 'teams' || card.kind === 'session') {
      var ico = document.createElement('span');
      ico.className = 'link-card__icon';
      ico.setAttribute('aria-hidden', 'true');
      var i = document.createElement('i');
      i.className = 'bi bi-camera-video-fill';
      ico.appendChild(i);
      a.appendChild(ico);
    }
    var body = document.createElement('span');
    body.className = 'link-card__body';
    if (card.site) {
      var site = document.createElement('span');
      site.className = 'link-card__site'; site.textContent = card.site;
      body.appendChild(site);
    }
    var title = document.createElement('span');
    title.className = 'link-card__title'; title.textContent = card.title || '';
    body.appendChild(title);
    if (card.description) {
      var desc = document.createElement('span');
      desc.className = 'link-card__desc'; desc.textContent = card.description;
      body.appendChild(desc);
    }
    a.appendChild(body);
    return a;
  }

  /** Fetch the card for one link and append it to its host, once. */
  function attach(link) {
    if (link.__lp) return;
    link.__lp = 1;
    var url = link.getAttribute('data-preview-url');
    if (!url) return;
    cardFor(url).then(function (card) {
      if (!card || !card.title || !link.isConnected) return;
      var host = link.closest('[data-link-host]') || link.parentElement;
      if (!host || host.querySelector(':scope > .link-card')) return;
      host.appendChild(build(card));
    });
  }

  var io = ('IntersectionObserver' in window) ? new IntersectionObserver(function (entries) {
    entries.forEach(function (e) { if (e.isIntersecting) { io.unobserve(e.target); attach(e.target); } });
  }, {rootMargin: '200px'}) : null;

  /**
   * Find preview links under ``root`` and queue each one once.
   * @param {Element|Document} [root=document]
   */
  function scan(root) {
    (root || document).querySelectorAll('a[data-preview-url]').forEach(function (link) {
      if (link.__lpSeen) return;
      link.__lpSeen = 1;
      if (io) io.observe(link); else attach(link);
    });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', function () { scan(); });
  else scan();
  document.addEventListener('htmx:load', function (e) { scan(e.target); });

  window.MetanoiaLinks = { scan: scan };
})();
