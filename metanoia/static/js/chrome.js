/**
 * @file chrome.js — shared app-chrome behaviour, loaded on every page after the
 * theme's functions.js (styles in static/css/chrome.css).
 *
 * Owns:
 *  - navbar height measurement (the --app-nav-h custom property);
 *  - sidebar collapse state and scroll position, both remembered in
 *    localStorage across navigation (metanoiahub.sidebar.collapsed /
 *    metanoiahub.sidebar.scroll), and the far-left hover-to-reveal sidebar;
 *  - global loading / upload / download affordances: the top bar on every form
 *    submit, internal link and same-origin fetch(), plus spinners on buttons;
 *  - notification / message / mail / incoming-call sounds;
 *  - the Django messages alert stack and live alerts (WebSocket with a polling
 *    fallback, toasts, desktop Web Push);
 *  - the Metanoia Mail inbox poll.
 * All features degrade gracefully when their target elements are absent.
 *
 * Globals it defines or extends: window.appLoader (adds .progress(pct) to the
 * object from partials/_loader.html), window.appSound, window.appAlert,
 * window.MetanoiaBell, window.MetanoiaDesktop, window.MetanoiaMail; it also wraps
 * window.fetch (marked with window.__appFetchWrapped). It reads
 * window.MetanoiaMail.refresh when present.
 */
(function () {
  'use strict';

  var doc = document;
  var body = doc.body;
  var root = doc.documentElement;

  var KEY_COLLAPSE = 'metanoiahub.sidebar.collapsed';
  var KEY_SCROLL   = 'metanoiahub.sidebar.scroll';

  /** localStorage that never throws (private mode, blocked storage). */
  var ls = {
    get: function (k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
    set: function (k, v) { try { localStorage.setItem(k, v); } catch (e) {} }
  };

  /** Keep --app-nav-h in sync with the real navbar height (66px fallback). */
  function syncNavHeight() {
    var header = doc.querySelector('header.navbar-light');
    var h = header ? Math.round(header.getBoundingClientRect().height) : 66;
    if (h > 0) root.style.setProperty('--app-nav-h', h + 'px');
  }

  /**
   * Re-apply a remembered collapsed sidebar. The theme's functions.js toggles
   * body.sidebar-start-enabled on .sidebar-start-toggle; this file only
   * remembers it. Run before DOMContentLoaded, ahead of the first paint it
   * controls.
   */
  function restoreCollapse() {
    if (ls.get(KEY_COLLAPSE) === '1') body.classList.add('sidebar-start-enabled');
  }
  /**
   * Remember the collapse state whenever the toggle is clicked. Saved on the
   * next frame, after the theme's handler has flipped the class.
   */
  function watchCollapse() {
    doc.addEventListener('click', function (e) {
      var t = e.target.closest && e.target.closest('.sidebar-start-toggle');
      if (!t) return;
      requestAnimationFrame(function () {
        ls.set(KEY_COLLAPSE, body.classList.contains('sidebar-start-enabled') ? '1' : '0');
      });
    });
  }

  /**
   * The sidebar column. Layouts mark their rail with .app-sidebar-col; the
   * dashboard layout also carries the older #appSidebarCol id. Matching either
   * means every page with a sidebar collapses — several used to ignore the
   * navbar button entirely.
   * @returns {?HTMLElement}
   */
  function sidebarScroller() {
    return doc.getElementById('appSidebarCol') || doc.querySelector('.app-sidebar-col');
  }

  /**
   * Mark <body> with .no-sidebar on pages without a rail, so chrome.css hides
   * the collapse button rather than showing one that does nothing.
   */
  function flagSidebarPresence() {
    body.classList.toggle('no-sidebar', !sidebarScroller());
  }

  /**
   * Restore the rail's scroll position (the rail is the same on every page) and
   * keep saving it, debounced, and on unload. The restore waits a frame so the
   * sidebar has its final height first.
   */
  function restoreSidebarScroll() {
    var el = sidebarScroller();
    if (!el) return;
    var y = parseInt(ls.get(KEY_SCROLL) || '0', 10);
    if (y > 0) {
      requestAnimationFrame(function () { el.scrollTop = y; });
    }
    var save = debounce(function () { ls.set(KEY_SCROLL, String(el.scrollTop)); }, 150);
    el.addEventListener('scroll', save, { passive: true });
    window.addEventListener('beforeunload', function () { ls.set(KEY_SCROLL, String(el.scrollTop)); });
  }

  /**
   * Add the far-left #sidebarHoverZone strip. While the sidebar is collapsed,
   * hovering it (or the rail) reveals the rail via body.sidebar-hover-open,
   * closing 220ms after the pointer leaves. Clicking the collapse toggle always
   * drops the hover overlay.
   */
  function setupHoverReveal() {
    if (doc.getElementById('sidebarHoverZone')) return;
    var zone = doc.createElement('div');
    zone.id = 'sidebarHoverZone';
    zone.setAttribute('aria-hidden', 'true');
    body.appendChild(zone);

    var col = sidebarScroller();
    var hideTimer = null;

    function open() {
      if (!body.classList.contains('sidebar-start-enabled')) return;
      clearTimeout(hideTimer);
      body.classList.add('sidebar-hover-open');
    }
    function scheduleClose() {
      clearTimeout(hideTimer);
      hideTimer = setTimeout(function () { body.classList.remove('sidebar-hover-open'); }, 220);
    }

    zone.addEventListener('mouseenter', open);
    zone.addEventListener('mouseleave', scheduleClose);
    if (col) {
      col.addEventListener('mouseenter', open);
      col.addEventListener('mouseleave', scheduleClose);
    }
    doc.addEventListener('click', function (e) {
      if (e.target.closest && e.target.closest('.sidebar-start-toggle')) {
        body.classList.remove('sidebar-hover-open');
      }
    });
  }

  /**
   * Extend the window.appLoader defined in partials/_loader.html with a
   * determinate .progress(pct) (0–100) for uploads and downloads; at 100 the
   * bar finishes and resets. Buttons opted in with data-loading show a spinner
   * (.is-loading) on click.
   */
  function setupLoader() {
    var el = doc.getElementById('appLoader');
    var bar = el && el.querySelector('.app-loader-bar');
    var base = window.appLoader || {};

    window.appLoader = Object.assign({}, base, {
      progress: function (pct) {
        if (!el || !bar) return;
        pct = Math.max(0, Math.min(100, pct));
        el.classList.remove('done');
        el.classList.add('on');
        bar.classList.add('determinate');
        bar.style.width = pct + '%';
        bar.style.opacity = '1';
        if (pct >= 100) {
          setTimeout(function () {
            el.classList.remove('on');
            el.classList.add('done');
            setTimeout(function () { bar.classList.remove('determinate'); bar.style.width = ''; }, 320);
          }, 200);
        }
      }
    });

    doc.addEventListener('click', function (e) {
      var b = e.target.closest && e.target.closest('[data-loading]');
      if (b && !b.classList.contains('is-loading')) b.classList.add('is-loading');
    });
  }

  /**
   * Global "everything feels async" affordances, with no per-page wiring:
   * every real form submit shows the top bar and spins its submit button, and
   * every same-origin link click in the same tab shows the top bar. Opt out with
   * data-no-loader on the form / link (used by the nav search and any AJAX form
   * that manages its own state); role="search" forms, downloads, toggles and
   * modified clicks are skipped. The submit listener runs in the bubble phase,
   * after the form's own handler, so e.defaultPrevented reliably means the form
   * was handled in JS. A back/forward cache restore clears a stuck bar.
   * fetch() is wrapped separately in wrapFetch().
   */
  function setupAsync() {
    doc.addEventListener('submit', function (e) {
      var form = e.target;
      if (!form || form.tagName !== 'FORM') return;
      if (form.hasAttribute('data-no-loader') || form.getAttribute('role') === 'search') return;
      if (e.defaultPrevented) return;
      try { if (window.appLoader) window.appLoader.show(false); } catch (err) {}
      var btn = form.querySelector('button[type=submit], input[type=submit], button:not([type])');
      if (btn && !btn.classList.contains('is-loading')) btn.classList.add('is-loading');
    });

    doc.addEventListener('click', function (e) {
      if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
      var a = e.target.closest && e.target.closest('a[href]');
      if (!a) return;
      var href = a.getAttribute('href') || '';
      if (a.target && a.target !== '_self') return;
      if (a.hasAttribute('download') || a.hasAttribute('data-no-loader')) return;
      if (a.hasAttribute('data-bs-toggle') || a.getAttribute('role') === 'button') return;
      if (!href || href.charAt(0) === '#' || href.indexOf('javascript:') === 0 ||
          href.indexOf('mailto:') === 0 || href.indexOf('tel:') === 0) return;
      if (/^https?:\/\//i.test(href) && href.indexOf(location.origin) !== 0) return;
      try { if (window.appLoader) window.appLoader.show(false); } catch (err) {}
    });
    window.addEventListener('pageshow', function () { try { window.appLoader && window.appLoader.hide(); } catch (e) {} });
  }

  /**
   * Show the top bar around every fetch() automatically. Pass
   * `{ appQuiet: true }` in the init object to keep a background request quiet.
   */
  function wrapFetch() {
    if (!window.fetch || window.__appFetchWrapped) return;
    window.__appFetchWrapped = true;
    var orig = window.fetch;
    window.fetch = function () {
      var quiet = arguments[1] && arguments[1].appQuiet;
      try { if (!quiet && window.appLoader) window.appLoader.show(false); } catch (e) {}
      return orig.apply(this, arguments).finally(function () {
        try { if (!quiet && window.appLoader) window.appLoader.hide(); } catch (e) {}
      });
    };
  }

  /**
   * Define window.appSound — message(), mail(), notify(), call() and
   * stopCall() — synthesised with WebAudio, so no sound assets are needed.
   * Browsers keep audio muted until the person has clicked or typed on the
   * page, so the audio context is woken on the first gesture and later alerts
   * can sound.
   */
  function setupSound() {
    var Ctx = window.AudioContext || window.webkitAudioContext;
    var ctx = null, callTimer = null;
    /** The shared AudioContext, created lazily (null if unsupported). */
    function ac() { if (!ctx && Ctx) ctx = new Ctx(); return ctx; }
    /**
     * Play one enveloped tone.
     * @param {number} freq Hz.
     * @param {number} [dur=0.15] Seconds.
     * @param {number} [when=0] Delay in seconds.
     * @param {string} [type='sine'] Oscillator type.
     * @param {number} [gain=0.12] Peak gain.
     */
    function beep(freq, dur, when, type, gain) {
      var c = ac(); if (!c) return;
      if (c.state === 'suspended') { try { c.resume(); } catch (e) {} }
      var o = c.createOscillator(), g = c.createGain();
      o.type = type || 'sine';
      o.frequency.value = freq;
      var t0 = c.currentTime + (when || 0);
      g.gain.setValueAtTime(0.0001, t0);
      g.gain.exponentialRampToValueAtTime(gain || 0.12, t0 + 0.02);
      g.gain.exponentialRampToValueAtTime(0.0001, t0 + (dur || 0.15));
      o.connect(g); g.connect(c.destination);
      o.start(t0); o.stop(t0 + (dur || 0.15) + 0.02);
    }
    /** Resume the audio context on the first gesture, then stop listening. */
    function unlock() {
      var c = ac(); if (c && c.state === 'suspended') { try { c.resume(); } catch (e) {} }
      doc.removeEventListener('pointerdown', unlock, true);
      doc.removeEventListener('keydown', unlock, true);
    }
    doc.addEventListener('pointerdown', unlock, true);
    doc.addEventListener('keydown', unlock, true);
    window.appSound = {
      message: function () { beep(660, 0.12, 0, 'sine', 0.10); },
      mail:    function () { beep(784, 0.10, 0, 'sine', 0.11); beep(1047, 0.10, 0.11, 'sine', 0.11); beep(1319, 0.18, 0.22, 'sine', 0.09); },
      notify:  function () { beep(880, 0.10, 0, 'triangle', 0.10); beep(1180, 0.12, 0.10, 'triangle', 0.09); },
      call: function () {
        this.stopCall();
        var ring = function () { beep(520, 0.4, 0, 'sine', 0.12); beep(660, 0.4, 0.15, 'sine', 0.12); };
        ring(); callTimer = setInterval(ring, 1600);
      },
      stopCall: function () { if (callTimer) { clearInterval(callTimer); callTimer = null; } }
    };
  }

  var ALERT_TTL = 3000;

  /**
   * Fade an alert out and remove it.
   * @param {HTMLElement} el
   */
  function dismissAlert(el) {
    if (!el || el.classList.contains('hiding')) return;
    el.classList.add('hiding');
    setTimeout(function () { if (el.parentNode) el.parentNode.removeChild(el); }, 320);
  }
  /**
   * Auto-dismiss an alert after ALERT_TTL (3s), pausing the countdown while it
   * is hovered, and wire its close button. Safe to call twice.
   * @param {HTMLElement} el
   */
  function wireAlert(el) {
    if (el.__wired) return; el.__wired = true;
    var timer = setTimeout(function () { dismissAlert(el); }, ALERT_TTL);
    var btn = el.querySelector('.btn-close');
    if (btn) btn.addEventListener('click', function () { clearTimeout(timer); dismissAlert(el); });
    el.addEventListener('mouseenter', function () { clearTimeout(timer); });
    el.addEventListener('mouseleave', function () { timer = setTimeout(function () { dismissAlert(el); }, ALERT_TTL); });
  }
  /**
   * Wire the server-rendered alerts in #appAlerts (partials/_alerts.html) and
   * define window.appAlert(msg, level) to push one into the same stack from
   * anywhere, e.g. appAlert('Saved!', 'success'). level is success, error
   * (shown as danger), danger, warning or info (default); returns the element.
   */
  function setupAlerts() {
    var stack = doc.getElementById('appAlerts');
    if (!stack) return;
    Array.prototype.forEach.call(stack.querySelectorAll('.app-alert'), wireAlert);

    var ICONS = { success: 'check-circle-fill', error: 'exclamation-octagon-fill',
                  danger: 'exclamation-octagon-fill', warning: 'exclamation-triangle-fill',
                  info: 'info-circle-fill' };
    window.appAlert = function (msg, level) {
      level = level || 'info';
      var color = level === 'error' ? 'danger' : level;
      var el = doc.createElement('div');
      el.className = 'app-alert alert alert-' + color + ' d-flex align-items-start';
      el.setAttribute('role', 'alert');
      el.innerHTML = '<i class="bi bi-' + (ICONS[level] || ICONS.info) + ' me-2 mt-1"></i>' +
                     '<div class="flex-grow-1"></div>' +
                     '<button type="button" class="btn-close ms-2" aria-label="Close"></button>';
      el.querySelector('.flex-grow-1').textContent = msg;
      stack.appendChild(el);
      wireAlert(el);
      return el;
    };
  }

  /**
   * @param {Function} fn
   * @param {number} ms
   * @returns {Function} fn, called only once calls have stopped for ms.
   */
  function debounce(fn, ms) {
    var t; return function () { clearTimeout(t); var a = arguments, self = this;
      t = setTimeout(function () { fn.apply(self, a); }, ms); };
  }

  /**
   * Live alerts for signed-in users (only they get the #suNotif bell), so a new
   * message, mail or notification is heard and seen the moment it lands.
   *
   * Preferred path: a WebSocket on /ws/alerts/ pushes each event; on close it
   * reconnects after 5s. Fallback: every 20s while the tab is visible, poll the
   * cheap two-count endpoint and chime when a count rises; the poller is silent
   * while the socket is live, and its first tick only sets the baseline. Each
   * alert plays its sound (window.appSound), redraws the bell's list and badge,
   * toasts the newest item once, offers it to MetanoiaDesktop and, for mail,
   * refreshes MetanoiaMail. Defines window.MetanoiaBell.refresh and starts the
   * desktop-alerts switch.
   */
  function setupAlertPoll() {
    var bell = doc.getElementById('suNotif');
    if (!bell || !window.fetch) return;
    var COUNTS_URL = '/communication/alerts/counts/';
    var BELL_URL = '/communication/alerts/bell/';
    var INTERVAL = 20000;
    var last = null;
    var socketLive = false;
    var announced = {};

    /** Play the sound for an alert kind: mail, message or anything else. */
    function playFor(kind) {
      if (!window.appSound) return;
      try {
        if (kind === 'mail') window.appSound.mail();
        else if (kind === 'message') window.appSound.message();
        else window.appSound.notify();
      } catch (e) {}
    }
    /** Show or remove the bell's unread dot. */
    function setDot(on) {
      var dot = bell.querySelector('.su-dot');
      if (on && !dot) { dot = doc.createElement('span'); dot.className = 'su-dot'; bell.insertBefore(dot, bell.querySelector('[data-bell-count]')); }
      if (!on && dot) dot.remove();
    }
    /** Set the bell's count badge (99+ cap) and dot. */
    function setCount(n) {
      var badge = bell.querySelector('[data-bell-count]');
      if (badge) { badge.textContent = n > 99 ? '99+' : (n || ''); badge.hidden = !(n > 0); }
      setDot(n > 0);
    }

    /**
     * Show a small card in the corner — icon, title, a line of text — that opens
     * the item on click. It leaves after 8s (4s after a hover ends).
     * @param {{title: string, body?: string, url?: string, kind?: string}} item
     */
    function toast(item) {
      if (!item || !item.title) return;
      var stack = doc.getElementById('suToasts');
      if (!stack) { stack = doc.createElement('div'); stack.id = 'suToasts'; stack.className = 'su-toasts'; stack.setAttribute('aria-live', 'polite'); doc.body.appendChild(stack); }
      var el = doc.createElement('a');
      el.className = 'su-toast su-toast--' + (item.kind || 'notification');
      el.href = item.url || '/communication/notifications/';
      var icon = item.kind === 'mail' ? 'bi-envelope-fill' : item.kind === 'message' ? 'bi-chat-left-text-fill' : 'bi-bell-fill';
      el.innerHTML = '<span class="su-toast__ico"><i class="bi ' + icon + '"></i></span>' +
        '<span class="su-toast__main"><strong></strong><span></span></span>' +
        '<button type="button" class="btn-close" aria-label="Dismiss"></button>';
      el.querySelector('strong').textContent = item.title;
      el.querySelector('.su-toast__main span').textContent = item.body || '';
      el.querySelector('.btn-close').addEventListener('click', function (e) { e.preventDefault(); e.stopPropagation(); close(); });
      stack.appendChild(el);
      requestAnimationFrame(function () { el.classList.add('is-in'); });
      var timer = setTimeout(close, 8000);
      el.addEventListener('mouseenter', function () { clearTimeout(timer); });
      el.addEventListener('mouseleave', function () { timer = setTimeout(close, 4000); });
      function close() { el.classList.remove('is-in'); setTimeout(function () { el.remove(); }, 300); }
    }

    /**
     * Redraw the bell's list and badge from the server.
     * @param {boolean} announce Also toast the latest item if not yet shown.
     * @param {Object} [pushed] The pushed event, toasted when the server has no
     *   latest item.
     * @returns {Promise}
     */
    function refreshBell(announce, pushed) {
      return fetch(BELL_URL, { headers: { 'X-Requested-With': 'XMLHttpRequest' }, credentials: 'same-origin' })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (d) {
          if (!d) return;
          var box = doc.querySelector('[data-bell-items]');
          if (box) box.innerHTML = d.html;
          setCount(d.total);
          if (last) last.notifications = Math.max(last.notifications, d.total);
          var item = d.latest;
          if (announce && item && !announced[item.key]) {
            announced[item.key] = 1;
            toast(item);
            if (window.MetanoiaDesktop) window.MetanoiaDesktop.local(item);
          } else if (announce && pushed && !item) {
            toast(pushed);
          }
        })
        .catch(function () {});
    }
    window.MetanoiaBell = { refresh: refreshBell };

    /** Handle one incoming alert of the given kind (with an optional payload). */
    function onAlert(kind, payload) {
      playFor(kind);
      setDot(true);
      refreshBell(true, payload);
      if (kind === 'mail' && window.MetanoiaMail && window.MetanoiaMail.refresh) window.MetanoiaMail.refresh();
    }

    /** Open the alerts WebSocket; polling covers any gap while it reconnects. */
    function openSocket() {
      if (!window.WebSocket) return;
      var proto = location.protocol === 'https:' ? 'wss' : 'ws';
      var ws;
      try { ws = new WebSocket(proto + '://' + location.host + '/ws/alerts/'); }
      catch (e) { return; }
      ws.onopen = function () { socketLive = true; };
      ws.onmessage = function (ev) {
        var d = {}; try { d = JSON.parse(ev.data); } catch (e) {}
        onAlert(d.kind || 'notification', d.title ? { title: d.title, body: d.body, url: d.url, kind: d.kind } : null);
      };
      ws.onclose = function () {
        socketLive = false;
        setTimeout(openSocket, 5000);
      };
      ws.onerror = function () { try { ws.close(); } catch (e) {} };
    }

    /** Poll the unread counts and alert on a rise. */
    function tick() {
      if (doc.hidden || socketLive) return;
      fetch(COUNTS_URL, { headers: { 'X-Requested-With': 'XMLHttpRequest' }, credentials: 'same-origin' })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (d) {
          if (!d) return;
          if (last) {
            if (d.notifications > last.notifications) onAlert('notification');
            else if (d.messages > last.messages) onAlert('message');
            else if (d.notifications !== last.notifications) refreshBell(false);
          }
          last = d;
        })
        .catch(function () {});
    }

    tick();
    setInterval(tick, INTERVAL);
    doc.addEventListener('visibilitychange', function () { if (!doc.hidden) { tick(); refreshBell(false); } });
    openSocket();
    setupDesktopAlerts();
  }

  /**
   * The bell's "Desktop alerts" switch ([data-desktop-alerts]). Subscribes this
   * browser to Web Push (service worker /sw.js, VAPID key from data-key-url),
   * so new mail shows as a system notification even when no Metanoia tab is open.
   * Where push is not available but notifications are, a hidden Metanoia tab
   * shows them itself via window.MetanoiaDesktop.local(item). An existing
   * subscription is re-sent on load, because this account may be new to the
   * browser or the server row may have been cleared.
   */
  function setupDesktopAlerts() {
    var box = doc.querySelector('[data-desktop-alerts]');
    var canNotify = 'Notification' in window;
    var canPush = canNotify && 'serviceWorker' in navigator && 'PushManager' in window && window.isSecureContext;
    var subscribed = false;
    window.MetanoiaDesktop = {
      local: function (item) {
        if (!canNotify || subscribed || Notification.permission !== 'granted' || !doc.hidden) return;
        try {
          var n = new Notification(item.title, { body: item.body || '', tag: item.key, icon: '/static/images/brand/metanoia-tick-app.png' });
          n.onclick = function () { window.focus(); location.href = item.url; n.close(); };
        } catch (e) {}
      }
    };
    if (!box || !canNotify) return;
    var label = box.querySelector('[data-desktop-label]'), btn = box.querySelector('[data-desktop-toggle]');
    /** The CSRF token from Django's csrftoken cookie. */
    function csrf() { var m = doc.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/); return m ? decodeURIComponent(m[1]) : ''; }
    /** POST JSON with the CSRF token; resolves to the parsed reply. */
    function post(url, body) {
      return fetch(url, { method: 'POST', credentials: 'same-origin', body: JSON.stringify(body || {}),
        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf(), 'X-Requested-With': 'XMLHttpRequest' } })
        .then(function (r) { return r.json(); });
    }
    /** Decode a base64url VAPID key into the bytes PushManager expects. */
    function keyBytes(b64) {
      var pad = '='.repeat((4 - b64.length % 4) % 4), raw = atob((b64 + pad).replace(/-/g, '+').replace(/_/g, '/'));
      var out = new Uint8Array(raw.length); for (var i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i); return out;
    }
    /** Draw the switch for state "on", "off" or "blocked". */
    function show(state) {
      box.hidden = false;
      btn.hidden = state === 'blocked';
      btn.disabled = false;
      if (state === 'on') { label.textContent = 'Desktop alerts are on'; btn.textContent = 'Turn off'; btn.className = 'btn btn-sm btn-light mb-0'; }
      else if (state === 'blocked') { label.textContent = 'Desktop alerts are blocked — allow notifications for this site in the browser'; }
      else { label.textContent = 'Desktop alerts for new mail'; btn.textContent = 'Turn on'; btn.className = 'btn btn-sm btn-primary mb-0'; }
    }
    var key = '';
    /** Register (or reuse) the push service worker. */
    function registration() { return navigator.serviceWorker.register('/sw.js', { scope: '/' }); }
    /** This browser's push subscription, or null. */
    function current() {
      if (!canPush) return Promise.resolve(null);
      return registration().then(function (reg) { return reg.pushManager.getSubscription(); });
    }
    fetch(box.dataset.keyUrl, { credentials: 'same-origin' }).then(function (r) { return r.json(); }).then(function (d) {
      key = d.key || '';
      if (!key) canPush = false;
      if (Notification.permission === 'denied') return show('blocked');
      return current().then(function (sub) {
        if (!sub) { show(canPush || Notification.permission !== 'granted' ? 'off' : 'on'); return; }
        return post(box.dataset.subscribeUrl, sub.toJSON()).then(function () { subscribed = true; show('on'); });
      });
    }).catch(function () {});

    btn.addEventListener('click', function (e) {
      e.preventDefault(); e.stopPropagation();
      btn.disabled = true;
      if (subscribed) {
        current().then(function (sub) {
          return post(box.dataset.unsubscribeUrl, { endpoint: sub ? sub.endpoint : '' }).then(function () {
            return sub ? sub.unsubscribe() : null;
          });
        }).then(function () { subscribed = false; show('off'); }, function () { show('on'); });
        return;
      }
      Notification.requestPermission().then(function (perm) {
        if (perm !== 'granted') { show(perm === 'denied' ? 'blocked' : 'off'); return null; }
        if (!canPush) { show('on'); return null; }
        return registration().then(function (reg) {
          return navigator.serviceWorker.ready.then(function () {
            return reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: keyBytes(key) });
          });
        }).then(function (sub) { return post(box.dataset.subscribeUrl, sub.toJSON()); })
          .then(function (d) {
            if (d && d.ok) {
              subscribed = true; show('on');
              try { new Notification('Desktop alerts are on', { body: 'New Metanoia mail will pop up here, even with the site closed.', icon: '/static/images/brand/metanoia-tick-app.png' }); } catch (e2) {}
            } else show('off');
          });
      }).catch(function () { show('off'); if (window.appAlert) window.appAlert('Desktop alerts could not be switched on in this browser.', 'warning'); });
    });
  }

  /**
   * Metanoia Mail, for admin / staff / educators with a mailbox
   * (<body data-mail-poll="<url>">): 3s after load and then once a minute while
   * the tab is visible, ask the server to check their inbox (it does so in the
   * background, throttled per mailbox) and refresh the sidebar Email badge
   * (.js-mail-badge). New mail also raises a platform notification, which the
   * bell announces with its own sound. Defines window.MetanoiaMail.setBadge(n)
   * and window.MetanoiaMail.refresh().
   */
  function setupMailPoll() {
    var url = doc.body && doc.body.getAttribute('data-mail-poll');
    if (!url || !window.fetch) return;
    window.MetanoiaMail = window.MetanoiaMail || {};
    window.MetanoiaMail.setBadge = function (n) {
      doc.querySelectorAll('.js-mail-badge').forEach(function (b) { b.textContent = n > 0 ? n : ''; b.hidden = !(n > 0); });
    };
    function tick() {
      if (doc.hidden) return;
      fetch(url, { headers: { 'X-Requested-With': 'XMLHttpRequest' }, credentials: 'same-origin' })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (d) { if (d) window.MetanoiaMail.setBadge(d.unread); })
        .catch(function () {});
    }
    window.MetanoiaMail.refresh = tick;
    setTimeout(tick, 3000);
    setInterval(tick, 60000);
    doc.addEventListener('visibilitychange', function () { if (!doc.hidden) tick(); });
  }

  restoreCollapse();

  /** Start every feature once the DOM is ready. */
  function init() {
    syncNavHeight();
    flagSidebarPresence();
    watchCollapse();
    restoreSidebarScroll();
    setupHoverReveal();
    setupLoader();
    setupAsync();
    wrapFetch();
    setupSound();
    setupAlerts();
    setupAlertPoll();
    setupMailPoll();
    window.addEventListener('resize', debounce(syncNavHeight, 120));
  }
  if (doc.readyState === 'loading') doc.addEventListener('DOMContentLoaded', init);
  else init();
})();
