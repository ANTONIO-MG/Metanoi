/**
 * @file exam-guard.js — the focus guard for a proctored assessment attempt.
 *
 * Loaded by templates/assessments/take.html when proctoring is on, with
 * exam-guard.css. Works on #take-form, #guardOverlay / #guardMsg, #guardCount,
 * #guardContent, #guardBanner and #guardFullscreen; defines no globals.
 *
 * Config comes from window.EXAM_GUARD (set inline by assessments/take.html):
 *   { enabled, limit, action, blockCopy, requireFullscreen, graceSeconds,
 *     blurContent, warnings, url, submitUrl, csrf }
 *
 * What it watches, and how each signal is treated:
 *
 *   tab hidden / window blurred  → counted immediately. This is the reliable
 *       signal that the student went to another tab, window or program.
 *   pointer leaves the window    → NOT counted on its own. The pointer crosses
 *       the edge constantly (scrollbars, a second monitor, the OS dock), so
 *       counting every crossing would bury real events in noise. Instead it
 *       shows the overlay at once and only counts if the pointer stays outside
 *       for `graceSeconds` — which is exactly the case where the student has
 *       actually gone somewhere else.
 *   fullscreen exit              → counted when fullscreen is required.
 *   copy / paste / right-click / print → blocked and recorded, but these never
 *       remove focus, so they are reported separately.
 *
 * The browser only reports *what happened*. The tally, the limit and the
 * decision to end an attempt all come back from the server, so editing this
 * file cannot lower a student's warning count. Reports are POSTed to CFG.url
 * (kind, seconds, detail) and the JSON state that comes back drives the tally,
 * the banner and, on `action: "autosubmit"`, the end of the attempt.
 *
 * Further rules:
 *  - Upload questions open a native file dialog, which blurs the window through
 *    no fault of the student, so clicking a file input opens a 60-second
 *    amnesty; coming back to the window closes it.
 *  - Reports are throttled to one every 700ms on the client (fullscreen exits
 *    excepted); the server de-duplicates properly. A failed report is dropped —
 *    when offline, the next event reports again.
 *  - On returning, the whole time away is banked against the attempt as a
 *    "blur" report, because a long absence matters more than a brief one and
 *    the educator sees the total.
 *  - The student may still copy, cut and right-click inside their own answer
 *    boxes; Ctrl/⌘+P and Ctrl/⌘+S (the usual "get a copy of the paper"
 *    shortcuts) are blocked and reported as print.
 *  - Requesting fullscreen needs a user gesture, so a refused request is
 *    ignored and the #guardFullscreen button is offered instead.
 *  - Submitting the form disarms the guard. A resumed attempt seeds the visible
 *    tally from the server's count (CFG.warnings).
 */
(function () {
  'use strict';
  var CFG = window.EXAM_GUARD;
  if (!CFG || !CFG.enabled) return;

  var form = document.getElementById('take-form');
  var overlay = document.getElementById('guardOverlay');
  var overlayMsg = document.getElementById('guardMsg');
  var counter = document.getElementById('guardCount');
  var content = document.getElementById('guardContent');

  var awaySince = null;
  var pointerTimer = null;
  var ended = false;
  var lastSentAt = 0;
  var dialogUntil = 0;

  document.addEventListener('click', function (e) {
    if (e.target && e.target.matches && e.target.matches('input[type=file]')) {
      dialogUntil = Date.now() + 60000;
    }
  }, true);
  /** @returns {boolean} Whether the file-dialog amnesty is still running. */
  function inDialog() { return Date.now() < dialogUntil; }

  /**
   * Report one guard event to the server and apply the state it returns.
   * Does nothing once the attempt has ended, or within 700ms of the last report
   * (except for fullscreen exits).
   * @param {string} kind Event kind, e.g. "hidden", "blur", "pointer_out",
   *   "fullscreen", "copy", "paste", "contextmenu", "print".
   * @param {number} [secondsAway] Seconds spent away, if known.
   * @param {string} [note] Free-text detail for the integrity report.
   */
  function report(kind, secondsAway, note) {
    if (ended) return;
    var now = Date.now();
    if (now - lastSentAt < 700 && kind !== 'fullscreen') return;
    lastSentAt = now;

    var body = new FormData();
    body.append('kind', kind);
    body.append('seconds', Math.round(secondsAway || 0));
    if (note) body.append('detail', note);

    fetch(CFG.url, {
      method: 'POST', credentials: 'same-origin',
      headers: { 'X-CSRFToken': CFG.csrf }, body: body
    }).then(function (r) { return r.json(); })
      .then(handleState)
      .catch(function () {});
  }

  /**
   * Apply the server's verdict: update the tally, show its message and, on
   * "autosubmit", lock the paper and submit what the student has after 2.5s so
   * their work is not simply lost.
   * @param {?{ok: boolean, warnings?: number, limit?: number, message?: string,
   *   action?: string}} state
   */
  function handleState(state) {
    if (!state || !state.ok) return;
    if (typeof state.warnings === 'number') setCount(state.warnings, state.limit);
    if (state.message) showBanner(state.message, state.action);

    if (state.action === 'autosubmit') {
      ended = true;
      lockDown(state.message);
      setTimeout(function () { if (form) form.submit(); }, 2500);
    }
  }

  /**
   * Show the warning tally in the header, marked as warned or over the limit.
   * @param {number} warnings
   * @param {number} [limit] 0 / absent when there is no limit.
   */
  function setCount(warnings, limit) {
    if (!counter) return;
    counter.hidden = false;
    counter.textContent = limit
      ? 'Warnings ' + warnings + '/' + limit
      : 'Warnings ' + warnings;
    counter.className = 'guard-count' + (limit && warnings >= limit ? ' is-over'
      : warnings > 0 ? ' is-warn' : '');
  }

  /**
   * Cover the paper (and blur it when configured) while the student is away.
   * @param {string} [text] Message to show on the cover.
   */
  function showOverlay(text) {
    if (!overlay || ended) return;
    if (overlayMsg && text) overlayMsg.textContent = text;
    overlay.hidden = false;
    if (CFG.blurContent && content) content.classList.add('guard-blurred');
  }
  /** Remove the cover and the blur, unless the attempt has ended. */
  function hideOverlay() {
    if (!overlay || ended) return;
    overlay.hidden = true;
    if (content) content.classList.remove('guard-blurred');
  }
  /**
   * Show the final, permanent cover once the attempt has been ended.
   * @param {string} [text]
   */
  function lockDown(text) {
    if (!overlay) return;
    overlay.hidden = false;
    overlay.classList.add('is-final');
    if (overlayMsg) overlayMsg.textContent = text || 'This attempt has ended.';
    if (content) content.classList.add('guard-blurred');
  }

  var bannerTimer = null;
  /**
   * Show a transient message for 7 seconds.
   * @param {string} text
   * @param {string} [action] Any server action marks the banner as severe.
   */
  function showBanner(text, action) {
    var el = document.getElementById('guardBanner');
    if (!el) return;
    el.textContent = text;
    el.hidden = false;
    el.className = 'guard-banner' + (action ? ' is-severe' : '');
    clearTimeout(bannerTimer);
    bannerTimer = setTimeout(function () { el.hidden = true; }, 7000);
  }

  /**
   * The window lost focus or was hidden: start the away clock, cover the paper
   * and report it at once (ignored during the file-dialog amnesty).
   * @param {string} kind
   * @param {string} [note]
   */
  function goneAway(kind, note) {
    if (ended || inDialog()) return;
    if (awaySince === null) awaySince = Date.now();
    showOverlay();
    report(kind, 0, note);
  }

  /**
   * The student is back: uncover the paper, end the file-dialog amnesty and
   * bank any absence of a second or more as a "blur" report with its duration.
   */
  function cameBack() {
    if (ended) return;
    hideOverlay();
    dialogUntil = 0;
    if (awaySince === null) return;
    var seconds = (Date.now() - awaySince) / 1000;
    awaySince = null;
    if (seconds >= 1) {
      var body = new FormData();
      body.append('kind', 'blur');
      body.append('seconds', Math.round(seconds));
      body.append('detail', 'returned after ' + Math.round(seconds) + 's');
      fetch(CFG.url, {
        method: 'POST', credentials: 'same-origin',
        headers: { 'X-CSRFToken': CFG.csrf }, body: body
      }).then(function (r) { return r.json(); }).then(handleState).catch(function () {});
    }
  }

  document.addEventListener('visibilitychange', function () {
    if (document.hidden) goneAway('hidden');
    else cameBack();
  });

  window.addEventListener('blur', function () { goneAway('blur'); });
  window.addEventListener('focus', cameBack);

  document.addEventListener('mouseout', function (e) {
    if (ended || inDialog()) return;
    if (e.relatedTarget || e.toElement) return;
    showOverlay('Your pointer has left the assessment window.');
    clearTimeout(pointerTimer);
    pointerTimer = setTimeout(function () {
      if (document.hasFocus && document.hasFocus()) {
        report('pointer_out', CFG.graceSeconds || 3,
               'pointer outside for ' + (CFG.graceSeconds || 3) + 's');
      }
    }, (CFG.graceSeconds || 3) * 1000);
  });
  document.addEventListener('mouseover', function () {
    clearTimeout(pointerTimer);
    if (awaySince === null) hideOverlay();
  });

  if (CFG.requireFullscreen) {
    var askBtn = document.getElementById('guardFullscreen');
    var request = function () {
      var el = document.documentElement;
      var fn = el.requestFullscreen || el.webkitRequestFullscreen || el.msRequestFullscreen;
      if (fn) { try { fn.call(el); } catch (e) { } }
    };
    if (askBtn) askBtn.addEventListener('click', function () { request(); askBtn.hidden = true; });
    document.addEventListener('fullscreenchange', function () {
      if (document.fullscreenElement) { hideOverlay(); if (askBtn) askBtn.hidden = true; return; }
      if (ended) return;
      showOverlay('Fullscreen is required for this assessment.');
      if (askBtn) askBtn.hidden = false;
      report('fullscreen', 0);
    });
  }

  if (CFG.blockCopy) {
    ['copy', 'cut'].forEach(function (evt) {
      document.addEventListener(evt, function (e) {
        if (e.target && e.target.closest && e.target.closest('input, textarea')) return;
        e.preventDefault();
        report('copy', 0);
      });
    });
    document.addEventListener('paste', function (e) {
      e.preventDefault();
      report('paste', 0);
    });
    document.addEventListener('contextmenu', function (e) {
      if (e.target && e.target.closest && e.target.closest('input, textarea')) return;
      e.preventDefault();
      report('contextmenu', 0);
    });
    window.addEventListener('beforeprint', function () { report('print', 0); });
    document.addEventListener('keydown', function (e) {
      if ((e.ctrlKey || e.metaKey) && ['p', 's'].indexOf((e.key || '').toLowerCase()) !== -1) {
        e.preventDefault();
        report('print', 0);
      }
    });
  }

  if (form) form.addEventListener('submit', function () {
    ended = true;
    hideOverlay();
  });

  if (CFG.warnings) setCount(CFG.warnings, CFG.limit);
})();
