/**
 * @file lesson-player.js — behaviour for the learner's lesson page.
 *
 * Config comes from window.LESSON_PLAYER (set inline by lesson_view.html):
 *   { lessonId, csrf, isAuthor, urls:{...}, notes:{ "<sectionId>": "…" } }
 *
 * Responsibilities:
 *   · tear-drops    — remember which section is open, mark sections complete,
 *                     unlock the next one without a page reload
 *   · media         — build Plyr players (file, YouTube, Vimeo) and expose the
 *                     current time so a moment can be bookmarked
 *   · notes         — swap the rail's note to whichever section is open, autosave
 *   · bookmarks     — save a passage / a moment / a quiz, then jump back to it
 *   · rail          — tab switching, progress ring, live counts
 *   · extras        — lesson completion, Teams meetings, tracked embeds and
 *                     study time banked per section
 *
 * ``players`` maps a block id to its Plyr instance (or the raw media element);
 * ``activeSection`` is the open section's id, ``''`` meaning the whole-lesson
 * note. Exports ``window.lessonPlayerShowRail(key)``.
 */
(function () {
  'use strict';
  var CFG = window.LESSON_PLAYER;
  if (!CFG) return;

  var players = {};
  var noteCache = CFG.notes || {};
  var activeSection = '';

  /**
   * POST ``fields`` as form data with the CSRF header.
   * @param {string} url
   * @param {Object} [fields] Null/undefined values are skipped.
   * @returns {Promise<Object>} The JSON response.
   */
  function post(url, fields) {
    var body = new FormData();
    Object.keys(fields || {}).forEach(function (k) {
      if (fields[k] !== undefined && fields[k] !== null) body.append(k, fields[k]);
    });
    return fetch(url, {
      method: 'POST', credentials: 'same-origin',
      headers: { 'X-CSRFToken': CFG.csrf }, body: body
    }).then(function (r) { return r.json(); });
  }
  /** URL ``name`` from the config with its ``0`` placeholder replaced by ``id``. */
  function u(name, id) { return CFG.urls[name].replace('0', id); }
  /** HTML-escape ``s``. */
  function esc(s) { var d = document.createElement('div'); d.textContent = s == null ? '' : String(s); return d.innerHTML; }
  /** Show an app alert (``kind`` defaults to success). */
  function toast(msg, kind) { if (window.appAlert) window.appAlert(msg, kind || 'success'); }

  /**
   * Build Plyr players for uploaded video, YouTube and Vimeo alike (falling
   * back to the raw element) and record each media length once known.
   */
  function initMedia() {
    document.querySelectorAll('[data-media-block]').forEach(function (wrap) {
      var id = wrap.getAttribute('data-media-block');
      var el = wrap.querySelector('.lv-media');
      if (!el) return;
      if (window.Plyr) {
        try {
          players[id] = new Plyr(el, {
            captions: { active: true },
            settings: ['captions', 'quality', 'speed', 'loop'],
            speed: { selected: 1, options: [0.75, 1, 1.25, 1.5, 1.75, 2] },
            youtube: { noCookie: true, rel: 0, modestbranding: 1 },
            invertTime: false
          });
        } catch (e) { players[id] = el; }
      } else {
        players[id] = el;
      }
      var p = players[id];
      if (p && p.on) {
        p.on('loadedmetadata', function () {
          if (p.duration) reportDuration(id, p.duration);
        });
      }
    });
  }

  var reported = {};
  /**
   * Note a media block's length once. The block's own duration is authoring
   * data — only the author's editor writes it — so nothing is sent from here;
   * the player only uses it locally for the bookmark label.
   * @param {string} blockId
   * @param {number} seconds
   */
  function reportDuration(blockId, seconds) {
    if (reported[blockId] || CFG.isAuthor) return;
    reported[blockId] = true;
  }

  /** Current playback position of a media block, in seconds. */
  function currentTime(blockId) {
    var p = players[blockId];
    if (!p) return 0;
    return (p.currentTime !== undefined ? p.currentTime : 0) || 0;
  }
  /**
   * Seek a media block to ``seconds`` and play. A provider that is not ready
   * yet may throw; that is ignored, as the scroll to the block still helps.
   */
  function seekTo(blockId, seconds) {
    var p = players[blockId];
    if (!p) return;
    try {
      if (p.currentTime !== undefined) p.currentTime = seconds;
      if (p.play) p.play();
    } catch (e) { }
  }
  /** Format seconds as ``m:ss``. */
  function stamp(seconds) {
    seconds = Math.max(0, Math.floor(seconds || 0));
    return Math.floor(seconds / 60) + ':' + ('0' + (seconds % 60)).slice(-2);
  }

  var STORE_KEY = 'lp-open-' + CFG.lessonId;

  /**
   * Tear-drops: open/close, remember, complete, unlock.
   *
   * Opening a section loads its note, remembers it in ``sessionStorage`` and
   * (for learners) records progress — opening is progress even before it is
   * finished. On load the section the learner was last reading is re-opened
   * (unless the URL has a hash; ``#sec-<id>`` opens that one). The "Mark
   * complete" buttons post completion, repaint progress and open the next
   * section.
   */
  function initSections() {
    document.querySelectorAll('.lp-sec').forEach(function (sec) {
      var id = sec.getAttribute('data-section');
      var panel = sec.querySelector('.accordion-collapse');
      if (!panel) return;

      panel.addEventListener('show.bs.collapse', function () {
        activeSection = id;
        loadNote(id, sec.querySelector('.sec-title').childNodes[0].textContent.trim());
        try { sessionStorage.setItem(STORE_KEY, id); } catch (e) { }
        if (!CFG.isAuthor) post(u('sectionProgress', id), { completed: 0, seconds: 0 });
      });
      if (panel.classList.contains('show')) {
        activeSection = id;
        loadNote(id, sec.querySelector('.sec-title').childNodes[0].textContent.trim());
      }
    });

    var remembered;
    try { remembered = sessionStorage.getItem(STORE_KEY); } catch (e) { remembered = null; }
    if (remembered && !location.hash) {
      var sec = document.querySelector('.lp-sec[data-section="' + remembered + '"]');
      if (sec && sec.getAttribute('data-locked') !== '1') openSection(remembered, false);
    }
    if (location.hash.indexOf('#sec-') === 0) openSection(location.hash.slice(5), true);

    document.querySelectorAll('[data-complete-section]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var id = btn.getAttribute('data-complete-section');
        btn.disabled = true;
        post(u('sectionProgress', id), { completed: 1 }).then(function (res) {
          if (!res || !res.ok) { btn.disabled = false; return; }
          var sec = document.querySelector('.lp-sec[data-section="' + id + '"]');
          if (sec) sec.classList.add('sec-done');
          btn.innerHTML = '<i class="bi bi-check2-circle me-1"></i>Completed';
          applyProgress(res);
          openNext(id);
        });
      });
    });
  }

  /**
   * Open a section's accordion panel.
   * @param {string} id Section id.
   * @param {boolean} scroll Scroll to it and flash it briefly.
   */
  function openSection(id, scroll) {
    var sec = document.querySelector('.lp-sec[data-section="' + id + '"]');
    if (!sec) return;
    var panel = sec.querySelector('.accordion-collapse');
    if (panel && !panel.classList.contains('show') && window.bootstrap) {
      bootstrap.Collapse.getOrCreateInstance(panel, { toggle: false }).show();
    } else if (panel) {
      panel.classList.add('show');
    }
    if (scroll) {
      sec.scrollIntoView({ behavior: 'smooth', block: 'start' });
      sec.classList.add('lp-flash');
      setTimeout(function () { sec.classList.remove('lp-flash'); }, 1600);
    }
  }

  /** Open the section after ``afterId`` unless it is locked. */
  function openNext(afterId) {
    var all = Array.prototype.slice.call(document.querySelectorAll('.lp-sec'));
    var index = all.findIndex(function (s) { return s.getAttribute('data-section') === String(afterId); });
    var next = all[index + 1];
    if (next && next.getAttribute('data-locked') !== '1') {
      openSection(next.getAttribute('data-section'), true);
    }
  }

  /**
   * Repaint locks + the progress bar/ring from a server response.
   *
   * A newly unlocked section's body was rendered as a padlock placeholder, so
   * a reload is the only honest way to show real content: its header is
   * enabled and clicking it reloads the page, rather than faking the content.
   * @param {{completion?: number, unlocked?: Array}} res
   */
  function applyProgress(res) {
    if (res.completion !== undefined) {
      var bar = document.getElementById('lpBar');
      if (bar) bar.style.width = res.completion + '%';
      var pct = document.getElementById('lpPct');
      if (pct) pct.textContent = res.completion + '%';
      var ring = document.querySelector('.lp-ring');
      if (ring) { ring.style.setProperty('--pct', res.completion); ring.querySelector('span').innerHTML = res.completion + '<small>%</small>'; }
    }
    var done = document.getElementById('lpDone');
    if (done) done.textContent = document.querySelectorAll('.lp-sec.sec-done').length;

    (res.unlocked || []).forEach(function (id) {
      var sec = document.querySelector('.lp-sec[data-section="' + id + '"]');
      if (!sec || sec.getAttribute('data-locked') !== '1') return;
      sec.setAttribute('data-locked', '0');
      sec.classList.remove('sec-locked');
      var lock = sec.querySelector('.sec-lock');
      if (lock) lock.innerHTML = '<i class="bi bi-unlock"></i>';
      var btn = sec.querySelector('.accordion-button');
      if (btn && btn.disabled) {
        btn.disabled = false;
        btn.addEventListener('click', function () { location.reload(); }, { once: true });
      }
    });
  }

  /** Rail tab switching (notes / bookmarks / …). */
  function initTabs() {
    var tabs = document.querySelectorAll('.lp-tabs button');
    tabs.forEach(function (tab) {
      tab.addEventListener('click', function () {
        tabs.forEach(function (t) { t.classList.remove('active'); });
        tab.classList.add('active');
        var key = tab.getAttribute('data-rail');
        document.querySelectorAll('.lp-pane').forEach(function (p) {
          p.classList.toggle('show', p.getAttribute('data-pane') === key);
        });
      });
    });
    refreshBookmarkCount();
  }
  /** Switch the rail to the tab ``key``. */
  function showRail(key) {
    var tab = document.querySelector('.lp-tabs button[data-rail="' + key + '"]');
    if (tab) tab.click();
  }

  var noteBox, noteChip, noteTimer;

  /** Show the cached note for a section in the rail's note box. */
  function loadNote(sectionId, title) {
    if (!noteBox) return;
    noteBox.value = noteCache[sectionId] || '';
    var label = document.getElementById('lpNoteFor');
    if (label) label.textContent = title || 'this section';
  }

  /** Autosave the note (800 ms after typing stops) for the active section. */
  function initNotes() {
    noteBox = document.getElementById('lpNote');
    noteChip = document.getElementById('lpNoteChip');
    if (!noteBox) return;
    noteBox.value = noteCache[''] || '';
    noteBox.addEventListener('input', function () {
      noteCache[activeSection] = noteBox.value;
      if (noteChip) { noteChip.classList.add('saving'); noteChip.innerHTML = '<i class="bi bi-arrow-repeat"></i>Saving…'; }
      clearTimeout(noteTimer);
      noteTimer = setTimeout(function () {
        post(CFG.urls.noteSave, { section: activeSection, body: noteBox.value }).then(function () {
          if (noteChip) { noteChip.classList.remove('saving'); noteChip.innerHTML = '<i class="bi bi-check-circle"></i>Saved'; }
        });
      }, 800);
    });
  }

  /** Update the bookmark count badge. */
  function refreshBookmarkCount() {
    var list = document.getElementById('lpBookmarks');
    var badge = document.getElementById('lpBmCount');
    if (!list || !badge) return;
    var n = list.querySelectorAll('li[data-id]').length;
    badge.textContent = n ? n : '';
    if (n) badge.removeAttribute('data-zero'); else badge.setAttribute('data-zero', '1');
  }

  /** Prepend a saved bookmark to the rail list. */
  function addBookmarkRow(bm) {
    var list = document.getElementById('lpBookmarks');
    if (!list) return;
    var empty = list.querySelector('[data-empty]');
    if (empty) empty.remove();
    var li = document.createElement('li');
    li.setAttribute('data-id', bm.id);
    li.setAttribute('data-section', bm.section_id || '');
    li.setAttribute('data-block', bm.block_id || '');
    li.setAttribute('data-seconds', (bm.anchor && bm.anchor.seconds) || '');
    li.setAttribute('data-text', (bm.anchor && bm.anchor.text) || '');
    li.innerHTML =
      '<button type="button" class="bm-go"><i class="bi ' + esc(bm.icon) + '"></i>' +
      '<span class="bm-main"><span class="bm-label">' + esc(bm.label || 'Saved spot') + '</span>' +
      '<span class="bm-meta">' + esc(bm.section_title || 'Lesson') +
      (bm.timestamp ? ' · ' + esc(bm.timestamp) : '') + '</span></span></button>' +
      '<button type="button" class="bm-del" title="Remove"><i class="bi bi-x-lg"></i></button>';
    list.insertBefore(li, list.firstChild);
    refreshBookmarkCount();
  }

  /**
   * Save a bookmark and add it to the rail.
   * @param {Object} fields Bookmark fields (kind, block, section, label, …).
   * @param {Function} [onDone] Called with the saved bookmark.
   */
  function saveBookmark(fields, onDone) {
    post(CFG.urls.bookmarkAdd, fields).then(function (res) {
      if (!res || !res.ok) { toast('Could not save that bookmark', 'error'); return; }
      addBookmarkRow(res.bookmark);
      toast('Bookmarked — find it in the panel on the right');
      if (onDone) onDone(res.bookmark);
    });
  }

  /** Id of the section containing ``el``, or ''. */
  function sectionOf(el) {
    var sec = el.closest('.lp-sec');
    return sec ? sec.getAttribute('data-section') : '';
  }

  /**
   * Bookmark buttons: media (the current playback position), quizzes /
   * questions, and whole sections. Jump-to and delete in the rail list are
   * delegated so new rows work too.
   */
  function initBookmarks() {
    document.querySelectorAll('[data-bookmark-media]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var blockId = btn.getAttribute('data-bookmark-media');
        var seconds = currentTime(blockId);
        saveBookmark({
          kind: 'media', block: blockId, section: sectionOf(btn),
          seconds: seconds, label: 'Moment at ' + stamp(seconds)
        }, function () { btn.classList.add('is-saved'); });
      });
    });

    document.querySelectorAll('[data-bookmark-block]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        saveBookmark({
          kind: btn.getAttribute('data-kind') || 'question',
          block: btn.getAttribute('data-bookmark-block'),
          section: sectionOf(btn),
          label: btn.getAttribute('data-label') || 'Question'
        }, function () { btn.classList.add('is-saved'); });
      });
    });

    document.querySelectorAll('[data-bookmark-section]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        saveBookmark({
          kind: 'section', section: btn.getAttribute('data-bookmark-section'),
          label: btn.getAttribute('data-label') || 'Section'
        }, function () { btn.classList.add('is-saved'); });
      });
    });

    var list = document.getElementById('lpBookmarks');
    if (list) list.addEventListener('click', function (e) {
      var li = e.target.closest('li[data-id]');
      if (!li) return;
      if (e.target.closest('.bm-del')) {
        post(u('bookmarkDelete', li.getAttribute('data-id')), {}).then(function () {
          li.remove(); refreshBookmarkCount();
        });
        return;
      }
      if (e.target.closest('.bm-go')) gotoBookmark(li);
    });
  }

  /**
   * Jump to a bookmark: open its section, then (after the accordion's
   * transition) scroll to the block, seek the media and re-highlight text.
   * @param {HTMLLIElement} li
   */
  function gotoBookmark(li) {
    var sectionId = li.getAttribute('data-section');
    var blockId = li.getAttribute('data-block');
    var seconds = parseFloat(li.getAttribute('data-seconds'));
    var text = li.getAttribute('data-text');

    if (sectionId) openSection(sectionId, !blockId);
    setTimeout(function () {
      if (blockId) {
        var block = document.querySelector('[data-block-id="' + blockId + '"]');
        if (block) block.scrollIntoView({ behavior: 'smooth', block: 'center' });
      }
      if (!isNaN(seconds) && blockId) seekTo(blockId, seconds);
      if (text) highlight(text, sectionId);
    }, 380);
  }

  /** Re-highlight a saved passage by finding its text in the open section. */
  function highlight(text, sectionId) {
    var scope = sectionId
      ? document.querySelector('.lp-sec[data-section="' + sectionId + '"]')
      : document.querySelector('.lp-main');
    if (!scope || !text) return;
    document.querySelectorAll('.lp-highlight').forEach(function (m) {
      m.replaceWith(document.createTextNode(m.textContent));
    });
    var needle = text.trim().slice(0, 120);
    var walker = document.createTreeWalker(scope, NodeFilter.SHOW_TEXT);
    var node;
    while ((node = walker.nextNode())) {
      var at = node.nodeValue.indexOf(needle);
      if (at === -1) continue;
      var range = document.createRange();
      range.setStart(node, at);
      range.setEnd(node, at + needle.length);
      var mark = document.createElement('mark');
      mark.className = 'lp-highlight';
      try { range.surroundContents(mark); } catch (e) { return; }
      mark.scrollIntoView({ behavior: 'smooth', block: 'center' });
      return;
    }
  }

  /** Selecting text in a bookmarkable block offers a "bookmark passage" button. */
  function initSelection() {
    var btn = document.getElementById('lpSelBtn');
    if (!btn) return;
    var pending = null;

    document.addEventListener('mouseup', function (e) {
      if (e.target.closest('#lpSelBtn')) return;
      var sel = window.getSelection();
      var text = sel ? String(sel).trim() : '';
      if (!text || text.length < 4) { btn.hidden = true; pending = null; return; }
      var host = sel.anchorNode && sel.anchorNode.parentElement
        ? sel.anchorNode.parentElement.closest('[data-bookmarkable]') : null;
      if (!host) { btn.hidden = true; pending = null; return; }

      var block = host.closest('[data-block-id]');
      pending = {
        kind: 'text',
        text: text.slice(0, 400),
        label: text.length > 90 ? text.slice(0, 90) + '…' : text,
        block: block ? block.getAttribute('data-block-id') : '',
        section: sectionOf(host)
      };
      var rect = sel.getRangeAt(0).getBoundingClientRect();
      btn.hidden = false;
      btn.style.top = (window.scrollY + rect.top - 42) + 'px';
      btn.style.left = (window.scrollX + rect.left + rect.width / 2 - btn.offsetWidth / 2) + 'px';
    });

    btn.addEventListener('click', function () {
      if (!pending) return;
      saveBookmark(pending);
      btn.hidden = true;
      window.getSelection().removeAllRanges();
      pending = null;
    });
    document.addEventListener('scroll', function () { btn.hidden = true; }, { passive: true });
  }

  /**
   * Lesson completion, meetings, tracked embeds and study time.
   *
   * - Live sessions run in Microsoft Teams, which cannot be framed: the join
   *   button opens the session's launch page (same-site paths only), which
   *   hands over to Teams and records who went in.
   * - Trackable embeds: while one is on screen, 30 s of viewing is reported to
   *   the LRS every 30 s — never from a background tab, which earns no
   *   study-time credit.
   * - Learners bank time spent in the open section every minute.
   */
  function initExtras() {
    var complete = document.getElementById('lvComplete');
    if (complete) complete.addEventListener('click', function () {
      complete.disabled = true;
      post(CFG.urls.lessonComplete, {}).then(function () {
        complete.innerHTML = '<i class="bi bi-check2-circle me-1"></i>Lesson completed';
        toast('Lesson completed — nice work!');
      });
    });

    document.querySelectorAll('[data-join-meeting]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var url = btn.getAttribute('data-join-meeting') || '';
        if (url.charAt(0) === '/' && url.charAt(1) !== '/') window.location.href = url;
      });
    });

    var tracked = document.querySelectorAll('[data-track-block]');
    if (tracked.length && 'IntersectionObserver' in window) {
      var visible = {};
      var io = new IntersectionObserver(function (entries) {
        entries.forEach(function (en) { visible[en.target.getAttribute('data-track-block')] = en.isIntersecting; });
      }, { threshold: 0.4 });
      tracked.forEach(function (el) { io.observe(el); });
      setInterval(function () {
        if (document.hidden) return;
        Object.keys(visible).forEach(function (id) {
          if (!visible[id]) return;
          post(u('blockTrack', id), { seconds: 30, verb: 'progressed' });
        });
      }, 30000);
    }

    if (!CFG.isAuthor) setInterval(function () {
      if (!activeSection || document.hidden) return;
      post(u('sectionProgress', activeSection), { completed: 0, seconds: 60 });
    }, 60000);
  }

  initMedia();
  initSections();
  initTabs();
  initNotes();
  initBookmarks();
  initSelection();
  initExtras();
  window.lessonPlayerShowRail = showRail;
})();
