/**
 * @file HTMX app glue for the boosted (single-page-style) hub.
 *
 * Loaded in ``<head>`` (after ``htmx.min.js``) so ``window.Metanoia`` exists
 * before any page content script runs — a content script can always register
 * its teardown. The ``Metanoia`` helpers are defined synchronously for the same
 * reason: content scripts run during parse, before ``<body>`` exists.
 *
 * Exports on ``window.Metanoia``:
 * - ``onTeardown(fn)`` — cleanup that runs once, on the next content swap (or
 *   full unload), then clears. Pages with sockets/timers register here so
 *   nothing leaks when the user navigates away.
 * - ``whenReady(cb)`` — run page init once jQuery/Bootstrap are present.
 * - ``ensureScript(src, cb)`` — load a library at most once per document.
 *
 * When ``window.htmx`` is present it also adds the CSRF header to every htmx
 * request, draws a thin top progress bar (``#htmxBar``), re-initialises
 * per-element widgets idempotently after a swap (Bootstrap 5 dropdowns and
 * collapse use the data-API and need nothing), and keeps the profile/module
 * tab strip's ``active`` class in step. Without htmx the app is a normal MPA.
 */
(function () {
  var teardowns = [];
  window.Metanoia = window.Metanoia || {};
  window.Metanoia.onTeardown = function (fn) { if (typeof fn === 'function') teardowns.push(fn); };
  /** Run and clear every registered teardown; a failing one does not stop the rest. */
  function runTeardowns() {
    var list = teardowns; teardowns = [];
    list.forEach(function (fn) { try { fn(); } catch (e) {} });
  }

  /**
   * Run ``cb`` once the DOM and the base scripts (jQuery/Bootstrap, loaded near
   * the end of ``<body>``) are ready.
   *
   * On a full page load a content script runs during parse — before those base
   * scripts — so it waits for ``DOMContentLoaded``, by which point the
   * synchronous base ``<script>``s have executed. On an HTMX boost the document
   * is already complete and jQuery et al. are present, so ``cb`` runs at once.
   * This lets a page's init live in its content block, still find jQuery and
   * re-run on every swap-in without a leak: the full-load path adds a single
   * one-shot listener, the boost path adds none.
   *
   * @param {Function} cb Page initialiser; exceptions are swallowed.
   */
  window.Metanoia.whenReady = function (cb) {
    if (typeof cb !== 'function') return;
    if (document.readyState !== 'loading') { try { cb(); } catch (e) {} }
    else document.addEventListener('DOMContentLoaded', function () { try { cb(); } catch (e) {} }, { once: true });
  };

  var scriptState = {};
  /**
   * Load a script by URL at most once per document, then call ``cb``.
   *
   * If it is already loaded (e.g. the page was boosted back to), ``cb`` runs
   * immediately with no re-fetch and no re-execution. This replaces putting a
   * ``<script src>`` in a content block, where an HTMX swap would load it
   * asynchronously and the inline init after it could run before the library
   * was defined. ``scriptState`` maps each src to ``'done'`` or
   * ``{pending: [cb, ...]}``; a load error resets it to allow a later retry.
   *
   * @param {string} src Script URL.
   * @param {Function} [cb] Called once the script has executed.
   */
  window.Metanoia.ensureScript = function (src, cb) {
    cb = (typeof cb === 'function') ? cb : function () {};
    var st = scriptState[src];
    if (st === 'done') { try { cb(); } catch (e) {} return; }
    if (st && st.pending) { st.pending.push(cb); return; }
    scriptState[src] = { pending: [cb] };
    var s = document.createElement('script');
    s.src = src;
    s.onload = function () {
      var q = (scriptState[src] || { pending: [] }).pending;
      scriptState[src] = 'done';
      q.forEach(function (f) { try { f(); } catch (e) {} });
    };
    s.onerror = function () { scriptState[src] = null; };
    document.head.appendChild(s);
  };

  /** Call ``fn`` as soon as ``document.body`` exists. */
  function ready(fn) {
    if (document.body) fn();
    else document.addEventListener('DOMContentLoaded', fn);
  }

  ready(function () {
    if (!window.htmx) return;
    var doc = document, body = doc.body;

    /** Read a cookie value by name (``''`` when absent). */
    function cookie(name) {
      var m = doc.cookie.match('(?:^|;\\s*)' + name + '=([^;]*)');
      return m ? decodeURIComponent(m[1]) : '';
    }
    body.addEventListener('htmx:configRequest', function (e) {
      e.detail.headers['X-CSRFToken'] = cookie('csrftoken');
    });

    var bar = doc.createElement('div');
    bar.id = 'htmxBar';
    body.appendChild(bar);
    body.addEventListener('htmx:beforeRequest', function () { bar.className = 'run'; });
    body.addEventListener('htmx:afterRequest', function () {
      bar.className = 'done'; setTimeout(function () { bar.className = ''; }, 260);
    });

    body.addEventListener('htmx:beforeSwap', runTeardowns);
    window.addEventListener('beforeunload', runTeardowns);

    /**
     * Initialise per-element widgets (tooltips, auto-growing textareas capped
     * at 352px) inside ``root``; idempotent, so safe after every swap.
     * @param {Element|Document} [root]
     */
    function initWidgets(root) {
      root = root || doc;
      if (window.bootstrap && bootstrap.Tooltip) {
        root.querySelectorAll('[data-bs-toggle="tooltip"]').forEach(function (el) {
          try { bootstrap.Tooltip.getOrCreateInstance(el); } catch (e) {}
        });
      }
      root.querySelectorAll('textarea[data-autoresize]').forEach(function (el) {
        if (el.dataset.thInit) return;
        el.dataset.thInit = '1';
        var grow = function () { el.style.height = 'auto'; el.style.height = Math.min(el.scrollHeight, 352) + 'px'; };
        el.addEventListener('input', grow); grow();
      });
    }
    body.addEventListener('htmx:load', function (e) { initWidgets(e.target); });

    body.addEventListener('click', function (e) {
      var tab = e.target.closest ? e.target.closest('.js-prof-tab') : null;
      if (!tab) return;
      var strip = tab.closest('.prof-tabs');
      if (strip) strip.querySelectorAll('.js-prof-tab').forEach(function (a) { a.classList.remove('active'); });
      tab.classList.add('active');
    });
  });
})();
