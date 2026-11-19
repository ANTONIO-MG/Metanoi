/**
 * @file Study groups — the member picker on templates/communication/_study_group_form.html.
 *
 * Search filters the list by name as you type. Picking a module narrows the list
 * to that module's programme and unticks anyone outside it (the server would
 * refuse them anyway). The "N selected" count follows the ticks.
 * The list of groups can be searched too ([data-sg-group-filter]), and
 * "Leave group" / "Close group" buttons ([data-confirm]) ask first.
 */
(function () {
  'use strict';

  /**
   * Wire up one member picker form ([data-sg-form]). Someone with no programme
   * listed is already in the group, so the module filter leaves them be. Enter
   * in the search box must not submit the form.
   * @param {HTMLFormElement} form
   */
  function initPicker(form) {
    var filter = form.querySelector('[data-sg-filter]');
    var module = form.querySelector('[data-sg-module]');
    var count = form.querySelector('[data-sg-count]');
    var none = form.querySelector('[data-sg-none]');
    var people = Array.prototype.slice.call(form.querySelectorAll('[data-sg-person]'));
    if (!people.length) return;

    /** Programme id of the selected module, or ''. */
    function programme() {
      var opt = module && module.options[module.selectedIndex];
      return opt ? (opt.getAttribute('data-programme') || '') : '';
    }

    /** Re-apply the search and module filters and update the count. */
    function refresh() {
      var q = (filter && filter.value || '').trim().toLowerCase();
      var prog = programme();
      var shown = 0, ticked = 0;
      people.forEach(function (row) {
        var box = row.querySelector('input[type=checkbox]');
        var progs = (row.getAttribute('data-programmes') || '').split(' ').filter(Boolean);
        var inScope = !prog || !progs.length || progs.indexOf(prog) !== -1;
        if (!inScope && box.checked) box.checked = false;
        var match = !q || (row.getAttribute('data-name') || '').indexOf(q) !== -1;
        var visible = inScope && match;
        row.parentNode.hidden = !visible;
        row.classList.toggle('is-checked', box.checked);
        if (visible) shown++;
        if (box.checked) ticked++;
      });
      if (count) count.textContent = ticked;
      if (none) none.hidden = shown > 0;
    }

    if (filter) filter.addEventListener('input', refresh);
    if (module) module.addEventListener('change', refresh);
    form.addEventListener('change', function (e) {
      if (e.target && e.target.name === 'members') refresh();
    });
    if (filter) filter.addEventListener('keydown', function (e) { if (e.key === 'Enter') e.preventDefault(); });
    refresh();
  }

  /**
   * Filter the group cards ([data-sg-card]) by the text typed in ``input``.
   * @param {HTMLInputElement} input
   */
  function initGroupFilter(input) {
    var cards = Array.prototype.slice.call(document.querySelectorAll('[data-sg-card]'));
    var none = document.querySelector('[data-sg-groups-none]');
    input.addEventListener('input', function () {
      var q = input.value.trim().toLowerCase(), shown = 0;
      cards.forEach(function (card) {
        var hit = !q || (card.getAttribute('data-search') || '').indexOf(q) !== -1;
        card.hidden = !hit;
        if (hit) shown++;
      });
      if (none) none.hidden = shown > 0 || !q;
    });
  }

  /** Initialise every picker, group filter and confirm button on the page. */
  function init() {
    document.querySelectorAll('[data-sg-form]').forEach(initPicker);
    document.querySelectorAll('[data-sg-group-filter]').forEach(initGroupFilter);
    document.querySelectorAll('.sg [data-confirm]').forEach(function (btn) {
      btn.addEventListener('click', function (e) {
        if (!window.confirm(btn.getAttribute('data-confirm'))) e.preventDefault();
      });
    });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
