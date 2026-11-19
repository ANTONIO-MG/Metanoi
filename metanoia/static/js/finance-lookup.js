/**
 * @file Finance pickers: search-as-you-type for customers and products & services.
 *
 * One floating panel per picker type, attached to whichever input is focused.
 * The panel carries a "search in" field menu and filter menus (institution,
 * programme, role / type, category, source); filters persist across the rows
 * of one invoice, so narrowing to "UNISA PGDA" once serves every line.
 *
 *   var p = new TCLookup({url, fields, filters, render, empty, footer});
 *   p.attach(input, {onPick: fn(row, input), onClose: fn(input)});
 *
 * Keyboard: ↓/↑ move, Enter picks (and never submits the invoice), Esc
 * closes. Results come from the JSON endpoints in
 * apps/finance/views_catalogue.py. Exports ``window.TCLookup`` (with the
 * ``TCLookup.esc`` and ``TCLookup.money`` helpers).
 */
(function (w, d) {
  'use strict';

  /** HTML-escape ``s`` (null/undefined → ''). */
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c];
    });
  }
  /**
   * Escape ``text`` and wrap each search word (2+ characters) in ``<mark>``,
   * without touching anything inside tags.
   * @param {string} text
   * @param {string[]} words
   * @returns {string} HTML.
   */
  function mark(text, words) {
    var out = esc(text);
    words.forEach(function (word) {
      if (word.length < 2) { return; }
      var safe = esc(word).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
      out = out.replace(new RegExp('(' + safe + ')(?![^<]*>)', 'gi'), '<mark>$1</mark>');
    });
    return out;
  }
  /** Format ``n`` as rands with two decimals, e.g. ``R1,250.00``. */
  function money(n) {
    return 'R' + (parseFloat(n) || 0).toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2});
  }

  var uid = 0;

  /**
   * A search-as-you-type picker panel shared by every input it is attached to.
   *
   * @class
   * @param {Object} opts
   * @param {string} opts.url JSON search endpoint.
   * @param {Array<{id: string, label: string}>} opts.fields "Search in" choices.
   * @param {Array<Object>} opts.filters Filter menus (``key``, ``label``, ``any``, ``options``).
   * @param {Function} opts.render ``(row, words, helpers)`` → HTML for one result.
   * @param {Function} opts.empty ``(q, state)`` → HTML when nothing matches.
   * @param {Function} [opts.footer] ``(q)`` → HTML for the actions area.
   * @param {Function} [opts.onFooter] ``(lookup, actionsEl, q)`` after the footer is drawn.
   * @param {boolean} [opts.autoHighlight] Highlight the first result as you type.
   */
  function TCLookup(opts) {
    this.o = opts;
    this.state = {field: 'all'};
    this.id = 'tcLookup' + (++uid);
    this.rows = [];
    this.active = -1;
    this.input = null;
    this.timer = null;
    this.ctrl = null;
    this.build();
  }

  /**
   * Create the floating panel and its listeners. A mousedown inside the panel
   * keeps focus in the input (so clicking a result works) unless it lands on
   * a form control, which may take focus.
   */
  TCLookup.prototype.build = function () {
    var self = this, o = this.o;
    var panel = d.createElement('div');
    panel.className = 'tc tc-lookup';
    panel.hidden = true;
    var bar = '<div class="tc-lookup__bar">';
    bar += '<label class="tc-lookup__f"><span>Search in</span><select class="form-select form-select-sm" data-f="field">' +
      o.fields.map(function (f) { return '<option value="' + esc(f.id) + '">' + esc(f.label) + '</option>'; }).join('') +
      '</select></label>';
    o.filters.forEach(function (f) {
      bar += '<label class="tc-lookup__f"><span>' + esc(f.label) + '</span><select class="form-select form-select-sm" data-f="' + esc(f.key) + '">' +
        '<option value="">' + esc(f.any || 'Any') + '</option>' +
        f.options.map(function (x) {
          return '<option value="' + esc(x.id) + '"' + (x.institution ? ' data-institution="' + esc(x.institution) + '"' : '') + '>' + esc(x.label) + '</option>';
        }).join('') + '</select></label>';
    });
    bar += '<button type="button" class="btn btn-sm btn-link tc-lookup__reset" data-reset hidden>Clear filters</button></div>';
    panel.innerHTML = bar +
      '<ul class="tc-lookup__list" role="listbox" id="' + this.id + '-list" aria-label="' + esc(o.label || 'Results') + '"></ul>' +
      '<div class="tc-lookup__foot"><span data-count aria-live="polite"></span><span data-actions></span></div>' +
      '<div class="tc-lookup__extra" data-extra hidden></div>';
    d.body.appendChild(panel);
    this.panel = panel;
    this.list = panel.querySelector('.tc-lookup__list');

    panel.addEventListener('mousedown', function (e) {
      if (!e.target.closest('select, input, textarea, button, a')) { e.preventDefault(); }
    });
    panel.querySelectorAll('[data-f]').forEach(function (sel) {
      sel.addEventListener('change', function () {
        self.state[sel.dataset.f] = sel.value;
        if (sel.dataset.f === 'institution') { self.scopeProgrammes(); }
        self.syncReset();
        self.search();
        if (self.input) { self.input.focus(); }
      });
    });
    panel.querySelector('[data-reset]').addEventListener('click', function () {
      panel.querySelectorAll('[data-f]').forEach(function (sel) {
        if (sel.dataset.f !== 'field') { sel.value = ''; self.state[sel.dataset.f] = ''; }
      });
      self.scopeProgrammes(); self.syncReset(); self.search();
      if (self.input) { self.input.focus(); }
    });
    this.list.addEventListener('click', function (e) {
      var li = e.target.closest('[data-i]');
      if (li) { self.pick(parseInt(li.dataset.i, 10)); }
    });
    d.addEventListener('mousedown', function (e) {
      if (panel.hidden || panel.contains(e.target) || e.target === self.input) { return; }
      self.close();
    });
    w.addEventListener('resize', function () { self.place(); });
    w.addEventListener('scroll', function () { self.place(); }, true);
  };

  /** Hide programmes outside the chosen institution, clearing a now-hidden choice. */
  TCLookup.prototype.scopeProgrammes = function () {
    var inst = this.state.institution || '';
    var sel = this.panel.querySelector('[data-f="programme"]');
    if (!sel) { return; }
    Array.prototype.forEach.call(sel.options, function (opt) {
      opt.hidden = !!(inst && opt.dataset.institution && opt.dataset.institution !== inst);
    });
    if (sel.selectedOptions[0] && sel.selectedOptions[0].hidden) { sel.value = ''; this.state.programme = ''; }
  };

  /** Show the reset button only while a filter is set. */
  TCLookup.prototype.syncReset = function () {
    var on = this.o.filters.some(function (f) { return this.state[f.key]; }, this);
    this.panel.querySelector('[data-reset]').hidden = !on;
  };

  /**
   * Attach the picker to an input (combobox ARIA, focus/typing/keyboard).
   *
   * @param {HTMLInputElement} input
   * @param {Object} handlers
   * @param {function(Object, HTMLInputElement)} handlers.onPick Called with the chosen row.
   * @param {function(HTMLInputElement)} [handlers.onClose] Called when the panel closes.
   * @param {function(HTMLInputElement, string): string} [handlers.queryFor] Rewrites the query.
   * @param {boolean} [handlers.selectOnFocus] Select the input's text on focus.
   */
  TCLookup.prototype.attach = function (input, handlers) {
    var self = this;
    input.setAttribute('role', 'combobox');
    input.setAttribute('aria-autocomplete', 'list');
    input.setAttribute('aria-expanded', 'false');
    input.setAttribute('aria-controls', this.id + '-list');
    input.setAttribute('autocomplete', 'off');
    input._tcLookup = handlers;
    input.addEventListener('focus', function () {
      if (handlers.selectOnFocus) { input.select(); }
      self.open(input);
    });
    input.addEventListener('click', function () { if (self.panel.hidden) { self.open(input); } });
    input.addEventListener('input', function () {
      if (self.panel.hidden || self.input !== input) { self.open(input); return; }
      clearTimeout(self.timer);
      self.timer = setTimeout(function () { self.search(); }, 180);
    });
    input.addEventListener('keydown', function (e) {
      if (self.panel.hidden || self.input !== input) {
        if (e.key === 'ArrowDown') { self.open(input); e.preventDefault(); }
        return;
      }
      if (e.key === 'ArrowDown') { self.move(1); e.preventDefault(); }
      else if (e.key === 'ArrowUp') { self.move(-1); e.preventDefault(); }
      else if (e.key === 'Enter') {
        e.preventDefault();
        if (self.active >= 0) { self.pick(self.active); } else { self.close(); }
      } else if (e.key === 'Escape') { self.close(); e.preventDefault(); }
      else if (e.key === 'Tab') { self.close(); }
    });
  };

  /** Open the panel under ``input`` (closing it elsewhere first) and search. */
  TCLookup.prototype.open = function (input) {
    if (this.input && this.input !== input) { this.close(); }
    this.input = input;
    this.panel.hidden = false;
    input.setAttribute('aria-expanded', 'true');
    this.place();
    this.search();
  };

  /** Hide the panel and notify the input's ``onClose`` handler. */
  TCLookup.prototype.close = function () {
    if (this.panel.hidden) { return; }
    var input = this.input;
    this.panel.hidden = true;
    this.panel.querySelector('[data-extra]').hidden = true;
    this.active = -1;
    if (input) {
      input.setAttribute('aria-expanded', 'false');
      input.removeAttribute('aria-activedescendant');
      if (input._tcLookup.onClose) { input._tcLookup.onClose(input); }
    }
    this.input = null;
  };

  /** Position the panel below the input, at least 460px wide, inside the viewport. */
  TCLookup.prototype.place = function () {
    if (this.panel.hidden || !this.input) { return; }
    var r = this.input.getBoundingClientRect();
    var vw = d.documentElement.clientWidth;
    var width = Math.min(Math.max(r.width, 460), vw - 16);
    var left = Math.min(Math.max(8, r.left), vw - width - 8);
    this.panel.style.width = width + 'px';
    this.panel.style.left = (left + w.scrollX) + 'px';
    this.panel.style.top = (r.bottom + w.scrollY + 4) + 'px';
  };

  /** The search text, passed through the input's ``queryFor`` when given. */
  TCLookup.prototype.query = function () {
    var input = this.input, q = input ? input.value.trim() : '';
    if (input && input._tcLookup.queryFor) { q = input._tcLookup.queryFor(input, q); }
    return q;
  };

  /** Fetch results for the current query and filters, aborting any earlier request. */
  TCLookup.prototype.search = function () {
    var self = this;
    if (!this.input) { return; }
    var params = new URLSearchParams();
    var q = this.query();
    params.set('q', q);
    Object.keys(this.state).forEach(function (k) { if (self.state[k]) { params.set(k, self.state[k]); } });
    if (this.ctrl) { this.ctrl.abort(); }
    this.ctrl = w.AbortController ? new AbortController() : null;
    this.list.setAttribute('aria-busy', 'true');
    fetch(this.o.url + '?' + params.toString(), {
      credentials: 'same-origin', headers: {'Accept': 'application/json'},
      signal: this.ctrl ? this.ctrl.signal : undefined
    }).then(function (r) { return r.json(); }).then(function (data) {
      self.show(data, q);
    }).catch(function (err) {
      if (err && err.name === 'AbortError') { return; }
      self.list.innerHTML = '<li class="tc-lookup__empty">Search is not available right now.</li>';
    });
  };

  /**
   * Render the results, the match count and the footer actions.
   * @param {{results: Array, total: number}} data Endpoint response.
   * @param {string} q The query the results are for.
   */
  TCLookup.prototype.show = function (data, q) {
    var self = this, words = q.split(/\s+/).filter(Boolean);
    this.rows = data.results || [];
    this.active = -1;
    this.list.removeAttribute('aria-busy');
    if (!this.rows.length) {
      this.list.innerHTML = '<li class="tc-lookup__empty">' + this.o.empty(q, this.state) + '</li>';
    } else {
      this.list.innerHTML = this.rows.map(function (row, i) {
        return '<li role="option" class="tc-lookup__opt" id="' + self.id + '-o' + i + '" data-i="' + i + '" aria-selected="false">' +
          self.o.render(row, words, {esc: esc, mark: mark, money: money}) + '</li>';
      }).join('');
    }
    var shown = this.rows.length, total = data.total || 0;
    this.panel.querySelector('[data-count]').textContent =
      total ? (total > shown ? 'Showing ' + shown + ' of ' + total + ' — keep typing or filter' : total + (total === 1 ? ' match' : ' matches')) : '';
    var actions = this.panel.querySelector('[data-actions]');
    actions.innerHTML = this.o.footer ? this.o.footer(q) : '';
    if (this.o.onFooter) { this.o.onFooter(this, actions, q); }
    if (this.o.autoHighlight && this.rows.length && q) { this.move(1); }
  };

  /** Move the highlight by ``step`` results, wrapping round. */
  TCLookup.prototype.move = function (step) {
    var opts = this.list.querySelectorAll('[data-i]');
    if (!opts.length) { return; }
    this.active = (this.active + step + opts.length) % opts.length;
    opts.forEach(function (li, i) { li.setAttribute('aria-selected', i === this.active ? 'true' : 'false'); }, this);
    var li = opts[this.active];
    this.input.setAttribute('aria-activedescendant', li.id);
    li.scrollIntoView({block: 'nearest'});
  };

  /** Pick result ``i`` for the current input and close. */
  TCLookup.prototype.pick = function (i) {
    var row = this.rows[i], input = this.input;
    if (!row || !input) { return; }
    input._tcLookup.onPick(row, input);
    this.close();
  };

  /** Pick a row that did not come from a search (the quick-create result). */
  TCLookup.prototype.pickRow = function (row) {
    var input = this.input;
    if (!input) { return; }
    input._tcLookup.onPick(row, input);
    this.close();
    input.focus();
  };

  TCLookup.esc = esc;
  TCLookup.money = money;
  w.TCLookup = TCLookup;
})(window, document);
