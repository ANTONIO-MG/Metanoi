/**
 * @file Metanoia UI motion for the hub shell (see css/metanoia-ui.css), and the
 * Metanoia alert dialog.
 *
 *  - [data-t-nav]          a horizontal pill nav: the white highlight sits on the
 *                          current page and glides to whatever the pointer is on.
 *  - .dash-side .nav       the sidebar lists get the same highlight, vertically.
 *
 * The shell survives htmx-boosted navigation, so everything is re-placed after
 * each swap, and a list is only wired once.
 *
 * Metanoia alert: ``MetanoiaUI.alert(message, {title, tone})`` shows the Metanoia
 * dialog instead of the browser's grey "<site> says" box.
 * ``tone`` is ``'error'`` (the default, also for plain ``alert()``),
 * ``'success'`` or ``'info'``. It returns a promise that resolves when the
 * dialog is dismissed; dialogs queue one at a time, in order, like the native
 * one. ``window.alert()`` is routed here too, so every old call site gets the
 * same look; nothing in the app relies on ``alert()`` blocking.
 *
 * Exports ``window.MetanoiaUI.alert`` and replaces ``window.alert``.
 */
(function () {
  'use strict';

  /** True when ``href`` points at the current page (trailing slashes ignored). */
  function samePath(href) {
    try {
      var url = new URL(href, window.location.origin);
      var here = window.location.pathname.replace(/\/+$/, '/') || '/';
      var there = url.pathname.replace(/\/+$/, '/') || '/';
      return there === here;
    } catch (e) { return false; }
  }

  /**
   * Wire a horizontal pill nav ([data-t-nav]): a glide highlight that sits on
   * the current item and follows hover/focus. Sets ``nav._tPlace`` to
   * re-place it; listeners are only added once.
   * @param {Element} nav
   */
  function wireNav(nav) {
    var glide = nav.querySelector('.t-nav__glide');
    if (!glide) {
      glide = document.createElement('span');
      glide.className = 't-nav__glide';
      glide.setAttribute('aria-hidden', 'true');
      nav.insertBefore(glide, nav.firstChild);
    }
    var items = Array.prototype.slice.call(nav.querySelectorAll(':scope > a, :scope > button, :scope > .dropdown > button'));
    /**
     * The item for the page we are on, which wins (the shell outlives htmx
     * swaps, so a mark set for the previous page must not stick); otherwise
     * whatever the server marked ``is-on``.
     */
    function current() {
      var byPath = items.filter(function (a) { return a.href && samePath(a.href); })[0];
      if (byPath) return byPath;
      return items.filter(function (a) { return a.classList.contains('is-on'); })[0] || null;
    }
    /** Move the glide to ``el`` (hide it when there is none or it is hidden). */
    function to(el) {
      if (!el || !el.offsetWidth) { glide.classList.remove('is-on'); return; }
      var box = el.getBoundingClientRect(), outer = nav.getBoundingClientRect();
      glide.style.setProperty('--x', (box.left - outer.left) + 'px');
      glide.style.setProperty('--w', box.width + 'px');
      glide.classList.add('is-on');
    }
    nav._tPlace = function () {
      var c = current();
      items.forEach(function (a) {
        if (a.tagName !== 'A') return;
        if (a === c) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
      });
      to(c);
    };
    if (!nav._tWired) {
      items.forEach(function (a) {
        a.addEventListener('mouseenter', function () { to(a); });
        a.addEventListener('focus', function () { to(a); });
      });
      nav.addEventListener('mouseleave', function () { to(current()); });
      nav._tWired = true;
    }
    nav._tPlace();
    requestAnimationFrame(function () { nav.classList.add('is-ready'); });
  }

  /**
   * Give a sidebar list the vertical hover highlight; wired only once.
   * @param {Element} list
   */
  function wireList(list) {
    if (list._tWired) { list._tPlace && list._tPlace(); return; }
    var glide = document.createElement('span');
    glide.className = 't-vglide';
    glide.setAttribute('aria-hidden', 'true');
    list.insertBefore(glide, list.firstChild);
    function links() { return Array.prototype.slice.call(list.querySelectorAll(':scope > li > .nav-link')); }
    function active() { return links().filter(function (a) { return a.classList.contains('is-active'); })[0] || null; }
    /** Move the vertical glide to ``el``, or hide it. */
    function to(el) {
      if (!el || !el.offsetHeight) { glide.classList.remove('is-on'); return; }
      glide.style.setProperty('--y', el.offsetTop + 'px');
      glide.style.setProperty('--h', el.offsetHeight + 'px');
      glide.classList.add('is-on');
    }
    list.addEventListener('mouseover', function (e) {
      var a = e.target.closest('.nav-link');
      if (a && a.parentElement && a.parentElement.parentElement === list) to(a);
    });
    list.addEventListener('mouseleave', function () { to(null); });
    list._tPlace = function () { to(null); };
    list._tWired = true;
  }

  /** Wire every pill nav and sidebar list on the page. */
  function init() {
    document.querySelectorAll('[data-t-nav]').forEach(wireNav);
    document.querySelectorAll('.dash-side .nav.flex-column').forEach(wireList);
  }

  document.addEventListener('DOMContentLoaded', init);
  document.addEventListener('htmx:afterSettle', function () {
    document.querySelectorAll('[data-t-nav]').forEach(function (nav) { nav._tPlace ? nav._tPlace() : wireNav(nav); });
  });
  window.addEventListener('resize', function () {
    document.querySelectorAll('[data-t-nav]').forEach(function (nav) { nav._tPlace && nav._tPlace(); });
  });
  if (document.fonts && document.fonts.ready) {
    document.fonts.ready.then(function () {
      document.querySelectorAll('[data-t-nav]').forEach(function (nav) { nav._tPlace && nav._tPlace(); });
    });
  }
})();

(function () {
  'use strict';
  var TONES = {
    error:   { icon: 'bi-exclamation-circle', title: 'Something went wrong' },
    success: { icon: 'bi-check-circle',       title: 'Done' },
    info:    { icon: 'bi-info-circle',        title: 'Please note' }
  };
  var queue = Promise.resolve();

  /**
   * Show one alert dialog; focus is trapped on OK and restored afterwards.
   * Escape or Enter closes it, as does a click on the backdrop.
   * @param {*} message Shown as text, never HTML.
   * @param {{title?: string, tone?: string}} [opts]
   * @returns {Promise<void>} Resolves when dismissed.
   */
  function show(message, opts) {
    opts = opts || {};
    var tone = TONES[opts.tone] ? opts.tone : 'error';
    return new Promise(function (resolve) {
      var back = document.createElement('div');
      back.className = 't-alert t-alert--' + tone;
      back.setAttribute('role', 'alertdialog');
      back.setAttribute('aria-modal', 'true');
      var card = document.createElement('div');
      card.className = 't-alert__card';
      card.innerHTML =
        '<span class="t-alert__icon"><i class="bi ' + TONES[tone].icon + '"></i></span>' +
        '<h5 class="t-alert__title"></h5><p class="t-alert__msg"></p>' +
        '<button type="button" class="t-alert__ok">OK<span><i class="bi bi-arrow-right"></i></span></button>' +
        '<div class="t-alert__strip"><i></i><i></i><i></i><i></i></div>';
      card.querySelector('.t-alert__title').textContent = opts.title || TONES[tone].title;
      card.querySelector('.t-alert__msg').textContent = message == null ? '' : String(message);
      back.appendChild(card);
      document.body.appendChild(back);
      var last = document.activeElement, ok = card.querySelector('.t-alert__ok');
      setTimeout(function () { back.classList.add('is-in'); ok.focus(); }, 16);
      function close() {
        document.removeEventListener('keydown', onKey, true);
        back.classList.remove('is-in');
        setTimeout(function () { back.remove(); if (last && last.focus) last.focus(); resolve(); }, 160);
      }
      function onKey(e) {
        if (e.key === 'Escape' || e.key === 'Enter') { e.preventDefault(); close(); }
        else if (e.key === 'Tab') { e.preventDefault(); ok.focus(); }
      }
      ok.addEventListener('click', close);
      back.addEventListener('click', function (e) { if (e.target === back) close(); });
      document.addEventListener('keydown', onKey, true);
    });
  }

  window.MetanoiaUI = window.MetanoiaUI || {};
  /**
   * Queue an alert after any already showing.
   * @param {*} message
   * @param {{title?: string, tone?: string}} [opts]
   * @returns {Promise<void>}
   */
  window.MetanoiaUI.alert = function (message, opts) {
    queue = queue.then(function () { return show(message, opts); });
    return queue;
  };
  window.alert = function (message) { window.MetanoiaUI.alert(message); };
})();
