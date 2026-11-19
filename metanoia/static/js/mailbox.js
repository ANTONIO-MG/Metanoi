/**
 * @file Metanoia Mail (templates/mailbox/mailbox.html).
 *
 * Swaps the message list and the reading pane in place from the fragment
 * views in apps/mailbox/views.py, keeps the address bar in step so a refresh
 * or a shared link lands on the same message, and runs the composer:
 * recipient chips with suggestions, a formatting toolbar, autosave, and
 * drag-and-drop attachments. Loaded once via Metanoia.ensureScript; init() runs
 * on every swap-in and registers its own teardown.
 *
 * Exports ``window.MetanoiaMail.init`` and ``window.MetanoiaMail.setBadge(n)``
 * (the unread badge in the shell, ``.js-mail-badge``). Keyboard shortcuts on
 * the list: ↑/↓ open the next/previous message, Delete deletes, ``r`` /
 * Shift+``r`` reply / reply all, ``n`` new mail.
 */
(function () {
  'use strict';

  var app, state, timers = [];

  /** First match of ``sel`` in ``root`` (default: document). */
  function $(sel, root) { return (root || document).querySelector(sel); }
  /** All matches of ``sel`` in ``root`` as an array. */
  function $$(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }
  /** HTML-escape ``s``. */
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
    return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); }

  /**
   * Fetch a mailbox endpoint; with ``opts.body`` it POSTs (plain objects are
   * turned into form data, arrays as repeated keys) with the CSRF header.
   * Never rejects: a bad or missing reply resolves to ``{ok: false, error}``.
   * @param {string} url
   * @param {{body?: (FormData|Object)}} [opts]
   * @returns {Promise<Object>}
   */
  function request(url, opts) {
    opts = opts || {};
    var init = { credentials: 'same-origin', headers: { 'X-Requested-With': 'XMLHttpRequest' } };
    if (opts.body) {
      init.method = 'POST';
      init.headers['X-CSRFToken'] = app.dataset.csrf;
      if (opts.body instanceof FormData) init.body = opts.body;
      else { var fd = new FormData(); Object.keys(opts.body).forEach(function (k) {
        var v = opts.body[k]; (Array.isArray(v) ? v : [v]).forEach(function (x) { fd.append(k, x); }); }); init.body = fd; }
    }
    return fetch(url, init).then(function (r) {
      return r.json().catch(function () { return { ok: false, error: 'The server sent an unexpected reply (' + r.status + ').' }; });
    }).catch(function () { return { ok: false, error: 'You appear to be offline.' }; });
  }

  /** Show a status line; non-error messages clear themselves after 4 s. */
  function status(text, isError) {
    var el = $('#mbStatus'); if (!el) return;
    el.textContent = text || ''; el.classList.toggle('is-error', !!isError);
    if (text && !isError) setTimeout(function () { if (el.textContent === text) el.textContent = ''; }, 4000);
  }

  /** Size the app to fill the viewport below it (from 768px up, at least 520px). */
  function fit() {
    if (!app || window.innerWidth < 768) { if (app) app.style.height = ''; return; }
    var top = app.getBoundingClientRect().top + window.scrollY;
    app.style.height = Math.max(520, window.innerHeight - top - 14) + 'px';
  }

  /** Put the folder/message/draft in the address bar so a refresh or shared link lands here. */
  function pushUrl(params) {
    var u = new URL(app.dataset.homeUrl, location.origin);
    Object.keys(params).forEach(function (k) { if (params[k]) u.searchParams.set(k, params[k]); });
    history.pushState({ mail: true }, '', u.pathname + u.search);
  }

  /**
   * Repaint folder unread counts (none for Sent and Trash), draft counts and
   * the shell badge from a server ``counts`` object.
   */
  function applyCounts(counts) {
    if (!counts) return;
    $$('[data-count]', app).forEach(function (b) {
      var n = counts[b.dataset.count], role = b.dataset.role;
      if (n === undefined) return;
      b.textContent = (n > 0 && role !== 'sent' && role !== 'trash') ? n : '';
    });
    $$('[data-count-draft]', app).forEach(function (b) {
      var n = counts[b.dataset.countDraft]; if (n !== undefined) b.textContent = n > 0 ? '[' + n + ']' : '';
    });
    if (counts.inbox_total !== undefined) window.MetanoiaMail.setBadge(counts.inbox_total);
  }

  /** Query parameters for the list view from the current state. */
  function listParams(offset) {
    var p = new URLSearchParams({ folder: state.folder, filter: state.filter, q: state.q, scope: state.scope });
    if (offset) p.set('offset', offset);
    return p;
  }

  /**
   * Reload the message list, or append the next page.
   * @param {{offset?: number, keepScroll?: boolean}} [opts]
   */
  function loadList(opts) {
    opts = opts || {};
    var rows = $('#mbRows');
    var offset = opts.offset || 0;
    return request(app.dataset.listUrl + '?' + listParams(offset).toString()).then(function (d) {
      if (!d.ok) { status(d.error, true); return; }
      if (offset) {
        var more = $('[data-mb="more"]', rows); if (more) more.remove();
        rows.insertAdjacentHTML('beforeend', d.html);
      } else {
        var keep = rows.scrollTop;
        rows.innerHTML = d.html;
        if (opts.keepScroll) rows.scrollTop = keep;
      }
      $('#mbListTitle').textContent = d.title;
      markSelected();
      applyCounts(d.counts);
      updateCommands();
    });
  }

  /** Highlight the row of the open message or draft. */
  function markSelected() {
    $$('.mb-row', app).forEach(function (r) {
      r.classList.toggle('is-on', (!!state.selected && r.dataset.id === String(state.selected)) ||
                                  (!!state.draft && r.dataset.draft === String(state.draft)));
    });
  }

  /** Ids of the ticked rows. */
  function checkedIds() { return $$('.mb-sel:checked', app).map(function (c) { return c.value; }); }
  /** Ids an action applies to: ``explicit``, else the ticked rows, else the open message. */
  function targetIds(explicit) {
    if (explicit) return [explicit];
    var ids = checkedIds();
    return ids.length ? ids : (state.selected ? [String(state.selected)] : []);
  }

  /** Enable/disable the command bar for the current selection. */
  function updateCommands() {
    var ids = targetIds(), one = !!state.selected || ids.length === 1;
    $$('.mb-commands [data-needs]', app).forEach(function (b) {
      b.disabled = b.dataset.needs === 'one' ? !one : ids.length === 0;
    });
    app.classList.toggle('is-selecting', checkedIds().length > 0);
    $$('.mb-row', app).forEach(function (r) {
      var c = $('.mb-sel', r); r.classList.toggle('is-checked', !!(c && c.checked));
    });
  }

  /** Fill the reading pane and wire its message frame or composer. */
  function setRead(html) {
    var pane = $('#mbRead');
    pane.innerHTML = html;
    pane.scrollTop = 0;
    app.classList.add('is-reading');
    var frame = $('.mb-frame', pane); if (frame) wireFrame(frame);
    var form = $('.mb-compose', pane); if (form) Composer(form);
  }

  /** Open message ``id`` in the reading pane (marking its row read at once). */
  function openMessage(id) {
    state.selected = id; state.draft = null;
    markSelected();
    var row = $('.mb-row[data-id="' + id + '"]', app);
    if (row) { row.classList.remove('is-unread'); row.dataset.read = '1'; }
    pushUrl({ folder: state.folder, m: id });
    updateCommands();
    return request('/mail/m/' + id + '/').then(function (d) {
      if (!d.ok) { status(d.error, true); return; }
      setRead(d.html);
      applyCounts(d.counts);
    });
  }

  /** Open draft ``id`` in the composer. */
  function openDraft(id) {
    state.draft = id; state.selected = null; markSelected();
    pushUrl({ folder: state.folder, draft: id });
    return request('/mail/draft/' + id + '/').then(function (d) { if (d.ok) setRead(d.html); else status(d.error, true); });
  }

  /** Empty the reading pane and deselect. */
  function clearReading() {
    state.selected = null; state.draft = null;
    $('#mbRead').innerHTML = '<div class="mb-empty"><i class="bi bi-envelope-open-heart"></i><h3>Select an item to read</h3><p>Nothing is selected.</p></div>';
    app.classList.remove('is-reading');
    pushUrl({ folder: state.folder });
    updateCommands();
  }

  /**
   * Size the message iframe to its content (re-measured as images load) and
   * show the "images blocked" bar when the body says it blocked any.
   * @param {HTMLIFrameElement} frame
   */
  function wireFrame(frame) {
    function size() {
      try {
        var doc = frame.contentDocument; if (!doc || !doc.body) return;
        frame.style.height = (doc.documentElement.scrollHeight + 4) + 'px';
      } catch (e) {}
    }
    frame.addEventListener('load', function () {
      size();
      try {
        var doc = frame.contentDocument;
        var blocked = parseInt(doc.body.getAttribute('data-blocked') || '0', 10);
        var bar = $('[data-images-bar]', frame.closest('.mb-msg')); if (bar) bar.hidden = !blocked;
        $$('img', doc).forEach(function (img) { img.addEventListener('load', size); });
        setTimeout(size, 400); setTimeout(size, 1500);
      } catch (e) {}
    });
  }

  /**
   * Run a list action (delete, archive, junk, inbox, move, read, unread, flag,
   * unflag). Optimistic: the list reacts at once and the server catches up;
   * on failure the list is reloaded.
   * @param {string} action
   * @param {{id?: string, folder?: string}} [opts]
   */
  function act(action, opts) {
    opts = opts || {};
    var ids = targetIds(opts.id);
    if (!ids.length) return;
    var removes = ['delete', 'archive', 'junk', 'inbox', 'move'].indexOf(action) !== -1;
    ids.forEach(function (id) {
      var row = $('.mb-row[data-id="' + id + '"]', app); if (!row) return;
      if (removes) row.style.display = 'none';
      if (action === 'read' || action === 'unread') { row.classList.toggle('is-unread', action === 'unread'); row.dataset.read = action === 'read' ? '1' : '0'; }
      if (action === 'flag' || action === 'unflag') { row.classList.toggle('is-flagged', action === 'flag'); row.dataset.flagged = action === 'flag' ? '1' : '0'; }
    });
    if (removes && ids.indexOf(String(state.selected)) !== -1) clearReading();
    status({ delete: 'Deleting…', archive: 'Archiving…', junk: 'Moving to Junk…', move: 'Moving…', inbox: 'Moving to Inbox…' }[action] || 'Updating…');
    var body = { action: action, ids: ids };
    if (opts.folder) body.folder = opts.folder;
    return request(app.dataset.actionUrl, { body: body }).then(function (d) {
      if (!d.ok) { status(d.error, true); loadList({ keepScroll: true }); return; }
      status('Done');
      if (removes) $$('.mb-row', app).forEach(function (r) { if (r.style.display === 'none') r.remove(); });
      var all = $('#mbSelectAll'); if (all) all.checked = false;
      $$('.mb-sel:checked', app).forEach(function (c) { c.checked = false; });
      applyCounts(d.counts); updateCommands();
      if (action === 'unread' || action === 'flag' || action === 'unflag') {
        if (state.selected && ids.indexOf(String(state.selected)) !== -1 && action === 'unread') clearReading();
      }
    });
  }

  /** Start a reply / reply-all / forward draft (``mode``) to a message. */
  function respond(mode, id) {
    id = id || state.selected || checkedIds()[0];
    if (!id) return;
    status('Opening…');
    request('/mail/m/' + id + '/respond/' + mode + '/', { body: { go: 1 } }).then(function (d) {
      if (!d.ok) { status(d.error, true); return; }
      status(''); state.draft = d.id; setRead(d.html);
      pushUrl({ folder: state.folder, draft: d.id });
    });
  }

  /** Create a new draft for the current account and focus its To box. */
  function newMail() {
    request(app.dataset.newUrl, { body: { account: app.dataset.account } }).then(function (d) {
      if (!d.ok) { status(d.error, true); return; }
      state.draft = d.id; state.selected = null; markSelected(); setRead(d.html);
      pushUrl({ folder: state.folder, draft: d.id });
      var to = $('.mb-recips__input', $('#mbRead')); if (to) to.focus();
    });
  }

  /**
   * Check the mail server for new messages.
   * @param {boolean} quiet Background check of the open folder (no status line).
   */
  function syncNow(quiet) {
    var btns = $$('[data-mb="sync"] i', app);
    btns.forEach(function (i) { i.classList.add('mb-spin'); });
    if (!quiet) status('Checking for new mail…');
    return request(app.dataset.syncUrl, { body: { folder: quiet ? state.folder : '' } }).then(function (d) {
      btns.forEach(function (i) { i.classList.remove('mb-spin'); });
      if (!d.ok && d.errors && d.errors.length) { status(d.errors.join(' · '), true); }
      else if (!quiet) status(d.new ? d.new + ' new message' + (d.new === 1 ? '' : 's') : 'You are up to date');
      if (d.new || !quiet) loadList({ keepScroll: true });
      else applyCounts(d.counts);
    });
  }

  /**
   * Run one composer form: autosave (1.5 s after a change, and on teardown),
   * recipient chips with suggestions, the formatting toolbar, attachments
   * (picker, paste and drag-and-drop), discard and send.
   *
   * - Changing From swaps the signature for that mailbox's own.
   * - A long recipient list (a pasted Bcc of hundreds) shows its first few
   *   chips and a "+N more" chip; the whole list is still what is sent.
   * - Pasting a whole list at once (a spreadsheet column, an Outlook or Gmail
   *   list, addresses split by commas, semicolons, tabs, spaces or new lines)
   *   makes every address a chip, skipping duplicates; a single address is
   *   left to type normally.
   * - Pasting into the body keeps it from dragging in a web page's styles: a
   *   very large HTML paste goes in as plain text, pasted files are attached.
   * - Send first flushes any half-typed address into a chip.
   *
   * @param {HTMLFormElement} form The ``.mb-compose`` form.
   */
  function Composer(form) {
    var editor = $('[data-editor]', form), saveTimer = null, saving = null, sent = false;

    /** The draft's fields as a plain object. */
    function fields() {
      var data = { body_html: editor.innerHTML, subject: form.subject.value, account: form.account.value };
      ['to', 'cc', 'bcc'].forEach(function (k) { data[k] = form.querySelector('input[name="' + k + '"]').value; });
      return data;
    }
    /** Save the draft now (not once sent or discarded). */
    function save() {
      clearTimeout(saveTimer);
      if (sent) return Promise.resolve();
      var saved = $('[data-saved]', form);
      saving = request(form.dataset.saveUrl, { body: fields() }).then(function (d) {
        if (d.ok && saved) saved.textContent = 'Draft saved at ' + d.saved;
        return d;
      });
      return saving;
    }
    /** Save the draft 1.5 s after the last change. */
    function soon() { clearTimeout(saveTimer); saveTimer = setTimeout(save, 1500); }
    form.account.addEventListener('change', function () {
      var tpl = $('template[data-mb-sig="' + form.account.value + '"]', form);
      var current = $('.mb-signature', editor);
      if (!tpl) return;
      var fresh = document.createElement('div');
      fresh.innerHTML = tpl.innerHTML.trim();
      var sig = fresh.firstElementChild;
      if (!sig) { if (current) current.remove(); return; }
      if (current) current.replaceWith(sig);
      else {
        var quote = $('.mb-quote', editor);
        if (quote) editor.insertBefore(sig, quote); else editor.appendChild(sig);
      }
    });
    form.addEventListener('input', soon);
    form.addEventListener('change', soon);

    $$('[data-recips]', form).forEach(function (box) {
      var hidden = form.querySelector('input[name="' + box.dataset.recips + '"]');
      var input = $('.mb-recips__input', box), list = null, active = -1, results = [], timer = null;
      var chips = splitAddr(hidden.value), expanded = false, note = null;
      var FOLD_AT = 12, FOLD_SHOW = 8;
      /** Redraw the chips (folded when long) and the "Remove N invalid" chip. */
      function render() {
        $$('.mb-chip', box).forEach(function (c) { c.remove(); });
        var folded = !expanded && chips.length > FOLD_AT;
        chips.forEach(function (c, i) {
          if (folded && i >= FOLD_SHOW) return;
          var el = document.createElement('span');
          el.className = 'mb-chip' + (validAddr(c) ? '' : ' is-bad');
          el.title = c;
          el.innerHTML = '<span>' + esc(chipLabel(c)) + '</span><button type="button" aria-label="Remove">&times;</button>';
          el.querySelector('button').addEventListener('click', function () { chips.splice(i, 1); render(); sync(); });
          box.insertBefore(el, input);
        });
        if (folded) {
          var more = document.createElement('button');
          more.type = 'button'; more.className = 'mb-chip mb-chip--more';
          more.textContent = '+' + (chips.length - FOLD_SHOW) + ' more';
          more.addEventListener('click', function () { expanded = true; render(); });
          box.insertBefore(more, input);
        }
        var bad = chips.filter(function (c) { return !validAddr(c); }).length;
        if (bad) {
          var fix = document.createElement('button');
          fix.type = 'button'; fix.className = 'mb-chip mb-chip--fix';
          fix.textContent = 'Remove ' + bad + ' invalid';
          fix.addEventListener('click', function () { chips = chips.filter(validAddr); render(); sync(); });
          box.insertBefore(fix, input);
        }
      }
      /** Write the chips back into the hidden field and schedule a save. */
      function sync() { hidden.value = chips.join('; '); soon(); }
      /** True when address ``a`` is already a chip (case-insensitive). */
      function has(a) {
        var key = addrOf(a).toLowerCase();
        return chips.some(function (c) { return addrOf(c).toLowerCase() === key; });
      }
      /** Add new addresses; returns how many were added and how many were duplicates. */
      function add(list) {
        var added = 0, dupes = 0;
        list.forEach(function (a) { if (has(a)) dupes++; else { chips.push(a); added++; } });
        return { added: added, dupes: dupes };
      }
      /** Turn typed text into chips and clear the input. */
      function commit(text) {
        add(splitAddr(text));
        input.value = ''; closeList(); render(); sync();
      }
      /** Show a short note under the box for 5 s. */
      function tell(msg) {
        if (!note) { note = document.createElement('div'); note.className = 'mb-recips__note'; box.appendChild(note); }
        note.textContent = msg; clearTimeout(note._t);
        note._t = setTimeout(function () { if (note) { note.remove(); note = null; } }, 5000);
      }
      input.addEventListener('paste', function (e) {
        var text = (e.clipboardData || window.clipboardData).getData('text') || '';
        var found = extractAddr(text);
        if (found.length < 2 && !/[\n\t;,]/.test(text)) return;
        e.preventDefault();
        var r = add(found);
        input.value = ''; closeList(); render(); sync();
        tell(r.added + ' address' + (r.added === 1 ? '' : 'es') + ' added' +
             (r.dupes ? ' · ' + r.dupes + ' already on the list' : '') +
             (found.length ? '' : ' · no e-mail addresses found in what you pasted'));
      });
      /** Close the suggestion list. */
      function closeList() { if (list) { list.remove(); list = null; } active = -1; }
      /** Draw the contact suggestions. */
      function showList() {
        closeList();
        if (!results.length) return;
        list = document.createElement('div'); list.className = 'mb-suggest';
        results.forEach(function (r, i) {
          var b = document.createElement('button'); b.type = 'button';
          b.innerHTML = '<span><strong>' + esc(r.name || r.email) + '</strong><small>' + esc(r.email) + '</small></span><em>' + esc(r.source) + '</em>';
          b.addEventListener('mousedown', function (e) { e.preventDefault(); commit(r.name ? r.name + ' <' + r.email + '>' : r.email); });
          if (i === active) b.classList.add('is-active');
          list.appendChild(b);
        });
        box.appendChild(list);
      }
      input.addEventListener('input', function () {
        clearTimeout(timer);
        var q = input.value.trim();
        if (/[,;]$/.test(input.value)) { commit(input.value); return; }
        if (q.length < 2) { results = []; closeList(); return; }
        timer = setTimeout(function () {
          request(app.dataset.contactsUrl + '?q=' + encodeURIComponent(q)).then(function (d) {
            results = (d && d.results) || []; active = results.length ? 0 : -1; showList();
          });
        }, 180);
      });
      input.addEventListener('keydown', function (e) {
        if (list && (e.key === 'ArrowDown' || e.key === 'ArrowUp')) {
          e.preventDefault(); active = (active + (e.key === 'ArrowDown' ? 1 : results.length - 1)) % results.length; showList(); return;
        }
        if (e.key === 'Enter' || e.key === 'Tab' || e.key === ',' || e.key === ';') {
          if (list && active >= 0 && results[active]) {
            e.preventDefault(); var r = results[active]; commit(r.name ? r.name + ' <' + r.email + '>' : r.email); return;
          }
          if (input.value.trim()) { if (e.key !== 'Tab') e.preventDefault(); commit(input.value); }
        }
        if (e.key === 'Backspace' && !input.value && chips.length) { chips.pop(); render(); sync(); }
        if (e.key === 'Escape') closeList();
      });
      input.addEventListener('blur', function () { setTimeout(function () { if (input.value.trim()) commit(input.value); closeList(); }, 150); });
      box.addEventListener('click', function (e) { if (e.target === box) input.focus(); });
      render();
    });

    $$('.mb-toolbar [data-cmd]', form).forEach(function (el) {
      var cmd = el.dataset.cmd;
      var run = function (value) {
        editor.focus();
        if (cmd === 'createLink') {
          value = prompt('Link address', 'https://'); if (!value) return;
          if (!/^(https?:|mailto:|tel:)/i.test(value)) value = 'https://' + value;
        }
        try { document.execCommand('styleWithCSS', false, cmd === 'hiliteColor' || cmd === 'foreColor'); } catch (e) {}
        document.execCommand(cmd, false, value || null);
        soon(); refreshState();
      };
      if (el.tagName === 'SELECT' || el.type === 'color') el.addEventListener('change', function () { run(el.value); });
      else el.addEventListener('mousedown', function (e) { e.preventDefault(); run(); });
    });
    /** Mark the toolbar buttons whose style applies at the caret. */
    function refreshState() {
      ['bold', 'italic', 'underline', 'strikeThrough', 'insertUnorderedList', 'insertOrderedList'].forEach(function (c) {
        var b = $('.mb-toolbar [data-cmd="' + c + '"]', form);
        try { if (b) b.classList.toggle('is-on', document.queryCommandState(c)); } catch (e) {}
      });
    }
    editor.addEventListener('keyup', refreshState);
    editor.addEventListener('mouseup', refreshState);
    editor.addEventListener('paste', function (e) {
      var cd = e.clipboardData; if (!cd) return;
      if (cd.files && cd.files.length) { e.preventDefault(); upload(cd.files); return; }
      var text = cd.getData('text/plain'), html = cd.getData('text/html');
      if (html && html.length > 50000) { e.preventDefault(); document.execCommand('insertText', false, text); }
    });

    var fileInput = $('[data-mb-file]', form), drop = $('[data-drop]', form), atts = $('[data-atts]', form);
    /** Upload files as attachments to the draft. */
    function upload(files) {
      if (!files || !files.length) return;
      var fd = new FormData();
      Array.prototype.forEach.call(files, function (f) { fd.append('files', f); });
      var saved = $('[data-saved]', form); if (saved) saved.textContent = 'Uploading ' + files.length + ' file' + (files.length > 1 ? 's' : '') + '…';
      request(form.dataset.attachUrl, { body: fd }).then(function (d) {
        if (!d.ok) { alert(d.error); if (saved) saved.textContent = ''; return; }
        atts.innerHTML = d.html; if (saved) saved.textContent = 'Attached';
      });
    }
    fileInput.addEventListener('change', function () { upload(fileInput.files); fileInput.value = ''; });
    var depth = 0;
    form.addEventListener('dragenter', function (e) { if (hasFiles(e)) { depth++; drop.hidden = false; e.preventDefault(); } });
    form.addEventListener('dragover', function (e) { if (hasFiles(e)) e.preventDefault(); });
    form.addEventListener('dragleave', function () { depth = Math.max(0, depth - 1); if (!depth) drop.hidden = true; });
    form.addEventListener('drop', function (e) {
      if (!hasFiles(e)) return; e.preventDefault(); depth = 0; drop.hidden = true; upload(e.dataTransfer.files);
    });
    /** True when a drag event carries files. */
    function hasFiles(e) { return e.dataTransfer && Array.prototype.indexOf.call(e.dataTransfer.types || [], 'Files') !== -1; }

    form.addEventListener('click', function (e) {
      var b = e.target.closest('[data-mb]'); if (!b || !form.contains(b)) return;
      var what = b.dataset.mb;
      if (what === 'attach') fileInput.click();
      else if (what === 'remove-att') {
        request(b.dataset.url, { body: { go: 1 } }).then(function (d) { if (d.ok) atts.innerHTML = d.html; });
      } else if (what === 'show-cc') {
        $$('[data-cc-row]', form).forEach(function (r) { r.hidden = false; }); b.hidden = true;
      } else if (what === 'discard') {
        if (!confirm('Discard this message?')) return;
        sent = true; clearTimeout(saveTimer);
        request(form.dataset.discardUrl, { body: { go: 1 } }).then(function () { clearReading(); loadList({ keepScroll: true }); });
      } else if (what === 'send') {
        $$('.mb-recips__input', form).forEach(function (i) { if (i.value.trim()) i.dispatchEvent(new Event('blur')); });
        setTimeout(function () {
          if (!form.subject.value.trim() && !confirm('Send this message without a subject?')) return;
          b.disabled = true; clearTimeout(saveTimer);
          var saved = $('[data-saved]', form); if (saved) saved.textContent = 'Sending…';
          request(form.dataset.sendUrl, { body: fields() }).then(function (d) {
            b.disabled = false;
            if (!d.ok) { if (saved) saved.textContent = ''; alert(d.error); return; }
            sent = true; clearReading(); status('Message sent'); applyCounts(d.counts);
            loadList({ keepScroll: true });
          });
        }, 200);
      }
    });
    timers.push(function () { if (!sent) { clearTimeout(saveTimer); save(); } });
  }

  /**
   * Split a recipient string on commas, semicolons and new lines, but not
   * inside quotes or angle brackets.
   * @param {string} text
   * @returns {string[]}
   */
  function splitAddr(text) {
    var out = [], buf = '', quoted = false, angle = false;
    String(text || '').split('').forEach(function (ch) {
      if (ch === '"') quoted = !quoted;
      if (ch === '<') angle = true; if (ch === '>') angle = false;
      if ((ch === ',' || ch === ';' || ch === '\n') && !quoted && !angle) { if (buf.trim()) out.push(buf.trim()); buf = ''; }
      else buf += ch;
    });
    if (buf.trim()) out.push(buf.trim());
    return out;
  }
  /**
   * Every address in a block of pasted text, keeping "Name <a@b>" together and
   * dropping duplicates. A name with a comma in it stays quoted, so the list
   * still splits right.
   * @param {string} text
   * @returns {string[]}
   */
  function extractAddr(text) {
    var out = [], seen = {}, m,
        re = /(?:"([^"]*)"|([^"<>,;\t\n]*?))\s*<([^<>\s,;]+@[^<>\s,;]+)>|([^\s<>,;:"'()\[\]]+@[^\s<>,;:"'()\[\]]+\.[A-Za-z]{2,})/g;
    while ((m = re.exec(String(text || '')))) {
      var email = (m[3] || m[4] || '').replace(/[.]+$/, ''), name = (m[1] || m[2] || '').trim();
      var key = email.toLowerCase();
      if (!email || seen[key]) continue;
      seen[key] = 1;
      out.push(name && m[3] ? (/[,;]/.test(name) ? '"' + name + '"' : name) + ' <' + email + '>' : email);
    }
    return out;
  }
  /** The bare address of a recipient ("Name <a@b>" → "a@b"). */
  function addrOf(c) { var m = /<([^>]+)>/.exec(c); return (m ? m[1] : c).trim(); }
  /** True when the recipient's address looks valid. */
  function validAddr(c) { return /^[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+$/.test(addrOf(c)); }
  /** Display name for a chip (the name, else the address). */
  function chipLabel(c) { var m = /^\s*"?([^"<]*?)"?\s*</.exec(c); return (m && m[1].trim()) || addrOf(c); }

  /**
   * The "Send drafts" dialog: list every draft, tick some and send them. They
   * go one at a time — each is an ordinary Send, so a slow mail server never
   * times a batch out. Drafts with a problem cannot be ticked.
   */
  function sendDrafts() {
    var modalEl = document.getElementById('mbSendDrafts');
    if (!modalEl || !window.bootstrap) return;
    var rowsBox = $('[data-bulk-rows]', modalEl), all = $('[data-bulk-all]', modalEl),
        go = $('[data-bulk-send]', modalEl), summary = $('[data-bulk-summary]', modalEl), busy = false;
    var modal = bootstrap.Modal.getOrCreateInstance(modalEl);
    /** The enabled draft tick boxes. */
    function boxes() { return $$('input[data-bulk-id]:not(:disabled)', rowsBox); }
    /** Update the Send button, select-all and summary from the ticks. */
    function refresh() {
      var n = boxes().filter(function (c) { return c.checked; }).length;
      go.disabled = busy || !n;
      $('span', go).textContent = n ? 'Send ' + n + ' selected' : 'Send selected';
      all.checked = n > 0 && n === boxes().length;
      if (!busy) summary.textContent = n ? n + ' of ' + boxes().length + ' ticked' : '';
    }
    rowsBox.innerHTML = '<div class="mb-bulk__note">Loading drafts…</div>';
    go.disabled = true; all.checked = false; summary.textContent = '';
    modal.show();
    request(app.dataset.outboxUrl).then(function (d) {
      if (!d.ok) { rowsBox.innerHTML = '<div class="mb-bulk__note text-danger">' + esc(d.error) + '</div>'; return; }
      if (!d.drafts.length) { rowsBox.innerHTML = '<div class="mb-bulk__note">There are no drafts to send.</div>'; return; }
      rowsBox.innerHTML = d.drafts.map(function (r) {
        return '<label class="mb-bulk__row' + (r.problem ? ' is-off' : '') + '" data-row="' + r.id + '">' +
          '<span class="mb-check"><input type="checkbox" data-bulk-id="' + r.id + '" data-url="' + esc(r.send_url) + '"' +
          (r.problem ? ' disabled' : '') + '><span></span></span>' +
          '<span class="mb-bulk__col mb-bulk__to" title="' + esc(r.to) + '">' + esc(r.to || '—') + '</span>' +
          '<span class="mb-bulk__col mb-bulk__col--grow">' + esc(r.subject) +
          (r.attachments ? ' <i class="bi bi-paperclip" title="' + r.attachments + ' attachment(s)"></i>' : '') +
          (r.problem ? ' <em class="mb-bulk__problem">' + esc(r.problem) + '</em>' : '') + '</span>' +
          '<span class="mb-bulk__col mb-bulk__col--from">' + esc(r.from) + '</span>' +
          '<span class="mb-bulk__state" data-state></span></label>';
      }).join('');
      refresh();
    });
    rowsBox.onchange = refresh;
    all.onchange = function () { boxes().forEach(function (c) { c.checked = all.checked; }); refresh(); };
    go.onclick = function () {
      var picked = boxes().filter(function (c) { return c.checked; });
      if (!picked.length || busy) return;
      if (!confirm('Send ' + picked.length + ' draft' + (picked.length === 1 ? '' : 's') + ' now?')) return;
      busy = true; refresh();
      var ok = 0, failed = 0;
      picked.reduce(function (chain, c) {
        return chain.then(function () {
          var row = c.closest('.mb-bulk__row'), st = $('[data-state]', row);
          st.innerHTML = '<span class="spinner-border spinner-border-sm"></span>';
          summary.textContent = 'Sending ' + (ok + failed + 1) + ' of ' + picked.length + '…';
          return request(c.dataset.url, { body: { go: 1 } }).then(function (d) {
            if (d.ok) { ok++; c.checked = false; c.disabled = true; row.classList.add('is-sent');
              st.innerHTML = '<i class="bi bi-check-circle-fill text-success" title="Sent"></i>'; }
            else { failed++; st.innerHTML = '<i class="bi bi-exclamation-circle-fill text-danger" title="' + esc(d.error) + '"></i>';
              row.title = d.error; }
          });
        });
      }, Promise.resolve()).then(function () {
        busy = false; refresh();
        summary.textContent = ok + ' sent' + (failed ? ', ' + failed + ' failed (hover the red mark for why)' : '') + '.';
        status(ok + ' draft' + (ok === 1 ? '' : 's') + ' sent', !!failed);
        loadList({ keepScroll: true });
      });
    };
  }

  var TEXT_LIMIT = 512 * 1024, previewSeq = 0;

  /** HTML for a note card in the preview (icon, file name, message). */
  function previewNote(icon, name, text) {
    return '<div class="mb-preview__note"><i class="bi ' + esc(icon) + '" aria-hidden="true"></i>' +
      '<strong>' + esc(name) + '</strong><p>' + esc(text) + '</p></div>';
  }

  /**
   * Preview an attachment in the dialog instead of downloading it.
   *
   * Images, PDFs, audio/video and plain text load from the ``?inline=1``
   * response, which ``views.file_response`` serves with nosniff and a
   * locked-down CSP and only for those types; everything else (Office files,
   * HTML, SVG…) shows its card with Download — nothing is handed to an
   * outside viewer. Text is shown with ``textContent``, never ``innerHTML``,
   * whatever it holds, and only its first 512 KB. ``previewSeq`` makes sure a
   * slow text fetch cannot fill a later preview. Some phone browsers (Chrome
   * on Android) do not draw PDFs inside a frame, hence the hint pointing at
   * Open in new tab / Download. Closing the dialog empties it (stopping a
   * playing video and dropping the PDF frame); Esc and the close button are
   * Bootstrap's, and focus goes back to the chip afterwards.
   *
   * @param {HTMLElement} link The attachment chip (``data-url``, ``data-kind``, …).
   */
  function preview(link) {
    var el = document.getElementById('mbPreview');
    var kind = link.dataset.kind, inlineUrl = link.dataset.url + '?inline=1';
    if (!el || !window.bootstrap) { if (kind) window.open(inlineUrl, '_blank', 'noopener'); else location.href = link.dataset.url; return; }
    var body = $('[data-preview-body]', el), name = link.dataset.name, icon = link.dataset.icon || 'bi-file-earmark';
    var open = $('[data-preview-open]', el), dl = $('[data-preview-download]', el);
    $('[data-preview-name]', el).textContent = name;
    $('[data-preview-size]', el).textContent = link.dataset.size || '';
    $('[data-preview-icon]', el).className = 'bi ' + icon + ' mb-preview__icon';
    dl.href = link.dataset.url; dl.setAttribute('download', name);
    open.href = inlineUrl; open.hidden = !kind;
    body.innerHTML = '';
    var seq = ++previewSeq;
    var fail = function () { body.innerHTML = previewNote(icon, name, 'This file could not be shown here. Download it to open it on your device.'); };

    if (kind === 'image') {
      var img = new Image(); img.alt = name; img.onerror = fail; img.src = inlineUrl; body.appendChild(img);
    } else if (kind === 'pdf') {
      var wrap = document.createElement('div'); wrap.style.cssText = 'display:flex;flex-direction:column;width:100%;height:100%';
      var frame = document.createElement('iframe'); frame.title = name; frame.src = inlineUrl; frame.style.flex = '1';
      var hint = document.createElement('div'); hint.className = 'mb-preview__more';
      hint.textContent = 'PDF not showing? Use Open in new tab or Download.';
      wrap.appendChild(frame); wrap.appendChild(hint); body.appendChild(wrap);
    } else if (kind === 'audio' || kind === 'video') {
      var media = document.createElement(kind); media.controls = true; media.preload = 'metadata';
      media.onerror = fail; media.src = inlineUrl; body.appendChild(media);
    } else if (kind === 'text') {
      body.innerHTML = '<div class="mb-preview__note"><span class="spinner-border spinner-border-sm"></span></div>';
      fetch(inlineUrl, { credentials: 'same-origin' }).then(function (r) {
        if (!r.ok) throw new Error(r.status); return r.text();
      }).then(function (text) {
        if (seq !== previewSeq) return;
        var pre = document.createElement('pre'); pre.textContent = text.slice(0, TEXT_LIMIT);
        body.innerHTML = ''; body.appendChild(pre);
        if (text.length > TEXT_LIMIT) {
          var more = document.createElement('div'); more.className = 'mb-preview__more';
          more.textContent = 'Showing the first 512 KB. Download the file to see all of it.'; body.appendChild(more);
          body.style.flexDirection = 'column';
        }
      }).catch(function () { if (seq === previewSeq) fail(); });
    } else {
      body.innerHTML = previewNote(icon, name, 'Preview not available for this type of file. Download it to open it on your device.');
      var btn = dl.cloneNode(true); btn.removeAttribute('data-preview-download');
      $('.mb-preview__note', body).appendChild(btn);
    }

    if (!el.dataset.wired) {
      el.dataset.wired = '1';
      el.addEventListener('hidden.bs.modal', function () {
        previewSeq++;
        var b = $('[data-preview-body]', el); b.innerHTML = ''; b.style.flexDirection = '';
        var from = el._mbFrom; el._mbFrom = null;
        if (from && document.body.contains(from)) from.focus();
      });
    }
    el._mbFrom = link;
    bootstrap.Modal.getOrCreateInstance(el).show();
  }

  /**
   * Delegated click handler: folders, draft rows, every ``[data-mb]`` command
   * outside the composer, and message rows (Ctrl/Cmd-click ticks a row; a
   * modified click on an attachment preview is left to the browser, to open
   * a tab).
   */
  function onClick(e) {
    var t = e.target;
    var folderLink = t.closest('a.mb-folder');
    if (folderLink) {
      e.preventDefault();
      state.folder = folderLink.dataset.folder; state.q = ''; state.filter = 'all';
      var s = $('#mbSearch input[type=search]'); if (s) s.value = '';
      var fk = $('#mbSearch input[name=folder]'); if (fk) fk.value = state.folder;
      $('#mbFilterLabel').textContent = 'All';
      $$('a.mb-folder', app).forEach(function (a) { a.classList.toggle('is-on', a === folderLink); });
      app.classList.remove('show-folders');
      var acct = folderLink.closest('.mb-acct');
      if (acct) { var nb = $('.mb-newfolder', acct); if (nb) app.dataset.account = nb.dataset.account; }
      clearReading();
      loadList();
      return;
    }
    var draftRow = t.closest('.mb-row[data-draft]');
    if (draftRow) { e.preventDefault(); openDraft(draftRow.dataset.draft); return; }

    var b = t.closest('[data-mb]');
    if (b && app.contains(b) && !b.closest('.mb-compose')) {
      var what = b.dataset.mb, row = b.closest('.mb-row');
      if (what === 'preview' && (e.ctrlKey || e.metaKey || e.shiftKey)) return;
      e.preventDefault();
      if (what === 'preview') preview(b);
      else if (what === 'new') newMail();
      else if (what === 'send-drafts') sendDrafts();
      else if (what === 'sync') syncNow(false);
      else if (what === 'act') act(b.dataset.act, { id: b.dataset.id, folder: b.dataset.folder });
      else if (what === 'respond') respond(b.dataset.mode, b.dataset.id);
      else if (what === 'toggle-read') {
        var ids = targetIds(), r0 = ids[0] && $('.mb-row[data-id="' + ids[0] + '"]', app);
        act(r0 && r0.dataset.read === '1' ? 'unread' : 'read');
      } else if (what === 'toggle-flag') {
        var ids2 = targetIds(), r1 = ids2[0] && $('.mb-row[data-id="' + ids2[0] + '"]', app);
        act(r1 && r1.dataset.flagged === '1' ? 'unflag' : 'flag');
      } else if (what === 'row-delete') act('delete', { id: row.dataset.id });
      else if (what === 'row-read') act(row.dataset.read === '1' ? 'unread' : 'read', { id: row.dataset.id });
      else if (what === 'row-flag') act(row.dataset.flagged === '1' ? 'unflag' : 'flag', { id: row.dataset.id });
      else if (what === 'filter') {
        state.filter = b.dataset.filter; $('#mbFilterLabel').textContent = b.textContent; loadList();
      } else if (what === 'more') loadList({ offset: parseInt(b.dataset.offset, 10) });
      else if (what === 'show-images') {
        var frame = $('.mb-frame', app); if (frame) { frame.src = frame.src.split('?')[0] + '?images=1'; }
        var bar = b.closest('[data-images-bar]'); if (bar) bar.hidden = true;
      } else if (what === 'back') { app.classList.remove('is-reading'); }
      else if (what === 'toggle-folders') app.classList.toggle('show-folders');
      else if (what === 'new-folder') {
        var name = prompt('Name the new folder'); if (!name) return;
        request(app.dataset.folderUrl, { body: { account: b.dataset.account, name: name } }).then(function (d) {
          if (d.ok) location.reload(); else status(d.error, true);
        });
      }
      return;
    }
    var msgRow = t.closest('.mb-row[data-id]');
    if (msgRow && !t.closest('.mb-check')) {
      if (e.ctrlKey || e.metaKey) { var c = $('.mb-sel', msgRow); c.checked = !c.checked; updateCommands(); return; }
      openMessage(msgRow.dataset.id);
    }
  }

  /** Select-all and row tick boxes. */
  function onChange(e) {
    if (e.target.id === 'mbSelectAll') {
      $$('.mb-sel', app).forEach(function (c) { c.checked = e.target.checked; });
    }
    if (e.target.classList.contains('mb-sel') || e.target.id === 'mbSelectAll') updateCommands();
  }

  /**
   * List keyboard shortcuts, ignored while typing. A dialog (attachment
   * preview, Send drafts) owns the keyboard while it is open: Delete or the
   * arrows must not act on the message behind it.
   */
  function onKey(e) {
    if (!app || e.target.closest('input, textarea, select, [contenteditable="true"]')) return;
    if (document.querySelector('.modal.show')) return;
    var rows = $$('.mb-row[data-id]', app), i = rows.findIndex(function (r) { return r.classList.contains('is-on'); });
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      if (!rows.length) return;
      e.preventDefault();
      var next = rows[Math.min(rows.length - 1, Math.max(0, i + (e.key === 'ArrowDown' ? 1 : -1)))];
      if (next) { next.scrollIntoView({ block: 'nearest' }); openMessage(next.dataset.id); }
    } else if (e.key === 'Delete' && targetIds().length) { e.preventDefault(); act('delete'); }
    else if (e.key === 'r' && state.selected && !e.ctrlKey && !e.metaKey) { e.preventDefault(); respond(e.shiftKey ? 'replyall' : 'reply'); }
    else if (e.key === 'n' && !e.ctrlKey && !e.metaKey) { e.preventDefault(); newMail(); }
  }

  /** Run a search from the search form. */
  function onSearch(e) {
    e.preventDefault();
    var f = e.target;
    state.q = f.q.value.trim(); state.scope = f.scope.value;
    clearReading(); loadList();
  }

  /**
   * Initialise the mailbox page (runs on every swap-in). Checks the open
   * folder on arrival and every minute after while the tab is visible;
   * back/forward reloads the page. Everything is torn down on the next swap.
   */
  function init() {
    app = document.getElementById('mbApp');
    if (!app) return;
    var rows = $('#mbRows');
    state = {
      folder: app.dataset.folder, filter: rows.dataset.filter || 'all', q: rows.dataset.q || '',
      scope: rows.dataset.scope || 'folder',
      selected: app.dataset.selected ? parseInt(app.dataset.selected, 10) : null,
      draft: (new URLSearchParams(location.search)).get('draft')
    };
    fit();
    app.addEventListener('click', onClick);
    app.addEventListener('change', onChange);
    $('#mbSearch').addEventListener('submit', onSearch);
    $('#mbSearch select').addEventListener('change', function () { if (state.q) $('#mbSearch').requestSubmit(); });
    document.addEventListener('keydown', onKey);
    window.addEventListener('resize', fit);
    var frame = $('.mb-frame', app); if (frame) { wireFrame(frame); if (frame.contentDocument && frame.contentDocument.readyState === 'complete') frame.dispatchEvent(new Event('load')); }
    var form = $('.mb-compose', app); if (form) Composer(form);
    if (state.selected || state.draft) app.classList.add('is-reading');
    updateCommands();

    var tick = setInterval(function () { if (!document.hidden) syncNow(true); }, 60000);
    setTimeout(function () { syncNow(true); }, 800);

    var onPop = function () { if (document.getElementById('mbApp')) location.reload(); };
    window.addEventListener('popstate', onPop);

    if (window.Metanoia && Metanoia.onTeardown) Metanoia.onTeardown(function () {
      clearInterval(tick);
      timers.forEach(function (fn) { try { fn(); } catch (e) {} }); timers = [];
      document.removeEventListener('keydown', onKey);
      window.removeEventListener('resize', fit);
      window.removeEventListener('popstate', onPop);
      app = null;
    });
  }

  window.MetanoiaMail = window.MetanoiaMail || {};
  window.MetanoiaMail.init = init;
  window.MetanoiaMail.setBadge = window.MetanoiaMail.setBadge || function (n) {
    document.querySelectorAll('.js-mail-badge').forEach(function (b) { b.textContent = n > 0 ? n : ''; b.hidden = !(n > 0); });
  };
})();
