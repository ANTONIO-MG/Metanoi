/**
 * @file Metanoia Community — @-tagging, Share and Report (templates/pages/feed.html).
 *
 * Tagging: any input/textarea with [data-mention-input] gets a people picker
 * when "@" is typed. The community it searches comes from the element named in
 * data-community-from (a <select>), or from data-community (a slug), or the
 * default community. Picking a person writes "@First Last " into the text and
 * adds a hidden <input name="mentions" value="<id>"> to the form — the server
 * checks both (apps.communication.community.resolve_mentions). Suggestions come
 * from /communication/community/mentions/?c=<community>&q=<query>, debounced by
 * 150ms; arrow keys move, Enter / Tab pick, Escape closes.
 *
 * Share and Report: clicking .feed-share-open / .feed-report-open opens the
 * #feedShareModal / #feedReportModal Bootstrap modal with the post's URL
 * (data-share-url / data-report-url) as the form action. Share hides the
 * community the post already lives in (data-from, else the default community)
 * and preselects the first other one.
 *
 * Sets window.__communityInit so loading the file twice is a no-op; needs
 * window.bootstrap for the modals.
 */
(function () {
  if (window.__communityInit) return;
  window.__communityInit = 1;

  var URL = '/communication/community/mentions/';
  var menu = null, target = null, items = [], active = 0, timer = null, token = null;

  /**
   * Escape text for safe insertion as HTML.
   * @param {string} s
   * @returns {string}
   */
  function esc(s) { return (s || '').replace(/[&<>"]/g, function (c) {
    return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]; }); }

  /**
   * The community slug a mention box searches: the value of the <select> named
   * by data-community-from, else data-community, else "" (the default).
   * @param {HTMLElement} el
   * @returns {string}
   */
  function communityOf(el) {
    var from = el.getAttribute('data-community-from');
    if (from) { var sel = document.querySelector(from); if (sel && sel.value) return sel.value; }
    return el.getAttribute('data-community') || '';
  }

  /**
   * The "@word" (up to two words) right before the caret.
   * @param {HTMLInputElement|HTMLTextAreaElement} el
   * @returns {?{start: number, end: number, query: string}} Its span and the
   *   text after "@", or null when the caret is not in a mention.
   */
  function currentToken(el) {
    var pos = el.selectionStart, text = el.value.slice(0, pos);
    var m = /(^|\s)@([^\s@]{0,30}(?: [^\s@]{0,30})?)$/.exec(text);
    if (!m) return null;
    return {start: pos - m[2].length - 1, end: pos, query: m[2]};
  }

  /** Hide the menu and forget the current suggestions and token. */
  function close() { if (menu) menu.hidden = true; items = []; token = null; }

  /**
   * Position the menu just below the given field.
   * @param {HTMLElement} el
   */
  function place(el) {
    var r = el.getBoundingClientRect();
    menu.style.left = (window.scrollX + r.left) + 'px';
    menu.style.top = (window.scrollY + r.bottom + 4) + 'px';
  }

  /**
   * Draw the suggestion menu (creating it on first use) for the current items,
   * or close it when there are none.
   */
  function render() {
    if (!menu) {
      menu = document.createElement('div');
      menu.className = 'mention-menu'; menu.hidden = true;
      document.body.appendChild(menu);
      menu.addEventListener('mousedown', function (e) {
        var b = e.target.closest('button[data-i]'); if (!b) return;
        e.preventDefault(); pick(parseInt(b.getAttribute('data-i'), 10));
      });
    }
    if (!items.length) { close(); return; }
    menu.innerHTML = items.map(function (p, i) {
      return '<button type="button" data-i="' + i + '" class="' + (i === active ? 'is-active' : '') + '">' +
        (p.avatar ? '<img src="' + esc(p.avatar) + '" alt="">' : '') +
        '<span><strong>' + esc(p.label) + '</strong>' +
        (p.role ? '<br><small class="text-muted">' + esc(p.role) + '</small>' : '') + '</span></button>';
    }).join('');
    place(target); menu.hidden = false;
  }

  /**
   * Insert "@Label " for suggestion i in place of the token, and add a hidden
   * mentions input for that person to the form (once).
   * @param {number} i Index into the current suggestions.
   */
  function pick(i) {
    var p = items[i]; if (!p || !target || !token) return;
    var el = target, v = el.value, insert = '@' + p.label + ' ';
    el.value = v.slice(0, token.start) + insert + v.slice(token.end);
    var caret = token.start + insert.length;
    el.setSelectionRange(caret, caret); el.focus();
    var form = el.closest('form');
    if (form && !form.querySelector('input[name=mentions][value="' + p.id + '"]')) {
      var h = document.createElement('input');
      h.type = 'hidden'; h.name = 'mentions'; h.value = p.id; form.appendChild(h);
    }
    close();
  }

  /**
   * Fetch suggestions for the token at the caret, after a 150ms pause; stale
   * replies (another field, or no token any more) are dropped.
   * @param {HTMLElement} el The mention field being typed in.
   */
  function lookup(el) {
    token = currentToken(el);
    if (!token) { close(); return; }
    target = el;
    clearTimeout(timer);
    timer = setTimeout(function () {
      var q = token ? token.query : '';
      fetch(URL + '?c=' + encodeURIComponent(communityOf(el)) + '&q=' + encodeURIComponent(q),
            {credentials: 'same-origin', headers: {'X-Requested-With': 'XMLHttpRequest'}})
        .then(function (r) { return r.ok ? r.json() : {results: []}; })
        .then(function (d) { if (target !== el || !token) return; items = d.results || []; active = 0; render(); })
        .catch(close);
    }, 150);
  }

  document.addEventListener('input', function (e) {
    var el = e.target; if (!el.matches || !el.matches('[data-mention-input]')) return;
    lookup(el);
  });
  document.addEventListener('keydown', function (e) {
    if (!menu || menu.hidden || e.target !== target) return;
    if (e.key === 'ArrowDown') { active = (active + 1) % items.length; render(); e.preventDefault(); }
    else if (e.key === 'ArrowUp') { active = (active - 1 + items.length) % items.length; render(); e.preventDefault(); }
    else if (e.key === 'Enter' || e.key === 'Tab') { pick(active); e.preventDefault(); }
    else if (e.key === 'Escape') { close(); }
  });
  document.addEventListener('focusout', function (e) {
    if (e.target === target) setTimeout(close, 150);
  });

  /**
   * Open a Share / Report modal with its form posting to the post's URL.
   * Clears hidden mentions left from a previous use.
   * @param {string} id The modal element's id.
   * @param {string} action The form action URL.
   * @param {function(HTMLFormElement)} [prepare] Resets the form for this post.
   */
  function openModal(id, action, prepare) {
    var el = document.getElementById(id); if (!el || !window.bootstrap) return;
    var form = el.querySelector('form'); form.action = action;
    form.querySelectorAll('input[name=mentions]').forEach(function (m) { m.remove(); });
    if (prepare) prepare(form);
    window.bootstrap.Modal.getOrCreateInstance(el).show();
  }
  document.addEventListener('click', function (e) {
    var share = e.target.closest && e.target.closest('.feed-share-open');
    if (share) {
      e.preventDefault();
      openModal('feedShareModal', share.getAttribute('data-share-url'), function (form) {
        var from = share.getAttribute('data-from'), sel = form.querySelector('select[name=community]'), first = null;
        Array.prototype.forEach.call(sel.options, function (o) {
          var same = from ? o.value === from : o.getAttribute('data-default') === '1';
          o.disabled = same; o.hidden = same;
          if (!same && first === null) first = o.value;
        });
        if (first !== null) sel.value = first;
        form.querySelector('textarea').value = '';
      });
      return;
    }
    var rep = e.target.closest && e.target.closest('.feed-report-open');
    if (rep) {
      e.preventDefault();
      openModal('feedReportModal', rep.getAttribute('data-report-url'), function (form) {
        form.querySelector('textarea').value = '';
      });
    }
  });
})();
