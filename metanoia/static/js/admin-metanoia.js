/**
 * @file The Django admin header's behaviour (templates/admin/_metanoia_header.html).
 *
 * Loaded on admin pages alongside admin-metanoia.css. Self-invoking; reads the
 * DOM only and defines no globals.
 *
 * - Centres the current table's pill in the app's pill strip, since on a long
 *   strip it may sit past the edge.
 * - Drives the "Find a table" box ([data-ta-find]): it searches the links in the
 *   All-apps drawer ([data-ta-menu] a[data-ta-model]) — which are exactly the
 *   tables the signed-in user may open — by table or app name. Arrow keys move,
 *   Enter opens, Escape closes; "/" anywhere on the page (outside a field) jumps
 *   into the box.
 */
(function () {
  var pills = document.querySelector('.ta-pills'), on = pills && pills.querySelector('a.is-on');
  if (on) pills.scrollLeft = on.offsetLeft - (pills.clientWidth - on.offsetWidth) / 2;

  var box = document.querySelector('[data-ta-find]');
  if (!box) return;
  var input = box.querySelector('[data-ta-find-input]');
  var out = box.querySelector('[data-ta-find-results]');
  var links = Array.prototype.slice.call(document.querySelectorAll('[data-ta-menu] a[data-ta-model]'));
  var items = links.map(function (a) {
    return { url: a.getAttribute('href'), model: a.dataset.taModel, app: a.dataset.taApp, icon: a.dataset.taIcon,
             hay: (a.dataset.taModel + ' ' + a.dataset.taApp).toLowerCase() };
  });
  var shown = [], cursor = 0;

  /**
   * Escape text for safe insertion as HTML.
   * @param {string} s
   * @returns {string}
   */
  function esc(s) { var d = document.createElement('div'); d.textContent = s; return d.innerHTML; }

  /**
   * Filter the tables by every typed word, put names that start with the query
   * first, and show up to 12 matches (or a "no match" note). Hides the list when
   * the box is empty.
   */
  function render() {
    var q = input.value.trim().toLowerCase();
    if (!q) { out.hidden = true; return; }
    var words = q.split(/\s+/);
    shown = items.filter(function (it) { return words.every(function (w) { return it.hay.indexOf(w) !== -1; }); })
      .sort(function (a, b) { return a.model.toLowerCase().indexOf(q) - b.model.toLowerCase().indexOf(q); })
      .slice(0, 12);
    cursor = 0;
    out.innerHTML = shown.length
      ? shown.map(function (it, i) {
          return '<a href="' + esc(it.url) + '"' + (i === 0 ? ' class="is-on"' : '') + '><i class="bi ' + esc(it.icon) + '"></i>'
            + esc(it.model) + '<small>' + esc(it.app) + '</small></a>';
        }).join('')
      : '<p>No table matches “' + esc(input.value.trim()) + '”.</p>';
    out.hidden = false;
  }

  /**
   * Move the keyboard highlight through the shown results, wrapping round.
   * @param {number} step +1 for down, -1 for up.
   */
  function move(step) {
    var as = out.querySelectorAll('a');
    if (!as.length) return;
    as[cursor].classList.remove('is-on');
    cursor = (cursor + step + as.length) % as.length;
    as[cursor].classList.add('is-on');
    as[cursor].scrollIntoView({ block: 'nearest' });
  }

  input.addEventListener('input', render);
  input.addEventListener('focus', render);
  input.addEventListener('keydown', function (e) {
    if (e.key === 'ArrowDown') { e.preventDefault(); move(1); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); move(-1); }
    else if (e.key === 'Enter') { e.preventDefault(); if (shown[cursor]) window.location.href = shown[cursor].url; }
    else if (e.key === 'Escape') { out.hidden = true; input.blur(); }
  });
  document.addEventListener('click', function (e) { if (!box.contains(e.target)) out.hidden = true; });
  document.addEventListener('keydown', function (e) {
    var t = e.target, typing = t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName));
    if (e.key === '/' && !typing) { e.preventDefault(); input.focus(); }
  });
})();
