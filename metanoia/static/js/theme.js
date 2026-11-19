/**
 * @file theme.js — the light / dark / auto switch, everywhere.
 *
 * The navbar's Mode buttons set ``data-bs-theme-value``; this script is what
 * listens to them, so the toggle works and the preference saved on the
 * settings page reaches the browser:
 *
 * - apply — write ``data-bs-theme`` on ``<html>`` (for "auto", the device's
 *   ``prefers-color-scheme``, re-applied when the OS setting changes while
 *   the page is open) and ``data-theme-choice`` with the chosen mode.
 * - persist — POST to ``/community/preferences/theme/`` so the choice follows
 *   the user to their next session and their other devices. ``localStorage``
 *   (key ``metanoiahub.theme``) is kept in step too, purely so a signed-out page
 *   paints right.
 * - sync — the settings page's Theme ``<select>`` and the navbar buttons drive
 *   each other; changing either one changes the page immediately, so "save"
 *   confirms a choice already visible rather than announcing one.
 *
 * The first paint is already correct because the layout stamps the attribute
 * server-side from the user's row; this script only handles changes made after
 * load, and the signed-out case. Guarded by ``window.__themeInit`` so a second
 * inclusion is a no-op. Exports ``window.setAppTheme(mode)``.
 */
(function () {
  'use strict';
  if (window.__themeInit) return;
  window.__themeInit = true;

  var KEY = 'metanoiahub.theme';
  var ENDPOINT = '/community/preferences/theme/';
  var VALID = { light: 1, dark: 1, auto: 1 };
  var root = document.documentElement;
  var media = window.matchMedia ? window.matchMedia('(prefers-color-scheme: dark)') : null;

  /** The mode this browser last saved, or null. */
  function stored() {
    try { var v = localStorage.getItem(KEY); return VALID[v] ? v : null; } catch (e) { return null; }
  }

  /**
   * The mode in force now.
   *
   * The server-rendered attribute is the truth on load. When the attribute was
   * set in the browser (``data-theme-client``: no saved choice on the server,
   * "auto" or signed out), the choice is this browser's. No choice at all
   * means light, the platform's default.
   * @returns {string} ``'light'``, ``'dark'`` or ``'auto'``.
   */
  function current() {
    if (root.hasAttribute('data-theme-client')) return stored() || 'light';
    var attr = root.getAttribute('data-bs-theme');
    if (VALID[attr]) return attr;
    return stored() || 'light';
  }

  /**
   * Paint ``mode`` on the page. "auto" follows the device; Bootstrap only reads
   * the attribute, so it is always set (removing it would just mean light).
   * @param {string} mode
   */
  function apply(mode) {
    if (mode === 'auto') root.setAttribute('data-bs-theme', media && media.matches ? 'dark' : 'light');
    else root.setAttribute('data-bs-theme', mode);
    root.setAttribute('data-theme-choice', mode);
    reflect(mode);
  }

  /** Mark the matching navbar buttons and Theme selects as chosen. */
  function reflect(mode) {
    document.querySelectorAll('[data-bs-theme-value]').forEach(function (btn) {
      var on = btn.getAttribute('data-bs-theme-value') === mode;
      btn.classList.toggle('active', on);
      btn.setAttribute('aria-pressed', on ? 'true' : 'false');
    });
    document.querySelectorAll('select[name="theme"]').forEach(function (sel) {
      if (sel.value !== mode) sel.value = mode;
    });
  }

  /** The CSRF token from the page or cookie; empty when signed out. */
  function csrf() {
    var el = document.querySelector('[name=csrfmiddlewaretoken]');
    if (el) return el.value;
    var m = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
    return m ? decodeURIComponent(m[1]) : '';
  }

  /**
   * Save ``mode`` in ``localStorage`` (ignored in private mode) and, when
   * signed in, on the server. Signed out, ``localStorage`` is enough. A failed
   * POST is ignored: the page is already the right colour.
   * @param {string} mode
   */
  function persist(mode) {
    try { localStorage.setItem(KEY, mode); } catch (e) { }
    var token = csrf();
    if (!token) return;
    var body = new FormData();
    body.append('theme', mode);
    fetch(ENDPOINT, {
      method: 'POST', body: body, credentials: 'same-origin',
      headers: { 'X-CSRFToken': token }, appQuiet: true
    }).catch(function () { });
  }

  /**
   * Apply and save a mode chosen by the user; unknown values mean "auto".
   * @param {string} mode
   */
  function set(mode) {
    if (!VALID[mode]) mode = 'auto';
    apply(mode);
    persist(mode);
  }
  window.setAppTheme = set;

  document.addEventListener('click', function (e) {
    var btn = e.target.closest('[data-bs-theme-value]');
    if (!btn) return;
    e.preventDefault();
    set(btn.getAttribute('data-bs-theme-value'));
  });

  document.addEventListener('change', function (e) {
    var sel = e.target;
    if (sel && sel.tagName === 'SELECT' && sel.name === 'theme') set(sel.value);
  });

  if (media && media.addEventListener) {
    media.addEventListener('change', function () {
      if (current() === 'auto') apply('auto');
    });
  }

  apply(current());
})();
