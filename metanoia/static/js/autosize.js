/**
 * @file Auto-expanding text boxes — one helper for every composer.
 *
 * Any <textarea data-autosize> grows with what is typed or pasted, up to
 * data-autosize-max lines (10 by default), then scrolls. It shrinks back when
 * its form is reset (e.g. after a message is sent) or when the value is set
 * from code and MetanoiaAutosize.resize(el) is called.
 *
 * Delegated from the document, so it works for boxes added later (HTMX swaps,
 * modals, JS-rendered threads) without any per-page set-up.
 *
 * <textarea data-autosize data-enter-submits> also sends its form on Enter
 * (Shift+Enter for a new line) — for one-line comment boxes that used to be
 * <input>s. While the @-mention menu (.mention-menu) is open, Enter is left to
 * the menu to pick a person.
 *
 * Exports window.MetanoiaAutosize = { resize, scan }; loading the file twice is a
 * no-op.
 */
(function () {
  if (window.MetanoiaAutosize) return;

  /**
   * Parse a computed CSS length.
   * @param {string} v e.g. "20px".
   * @returns {number} The number, or 0 when it is not numeric (e.g. "normal").
   */
  function px(v) { var n = parseFloat(v); return isNaN(n) ? 0 : n; }

  /**
   * Fit a textarea's height to its content, capped at data-autosize-max lines
   * (default 10) plus padding and, for border-box sizing, borders; beyond the
   * cap it scrolls.
   * @param {HTMLElement} el Ignored unless it is a <textarea>.
   */
  function resize(el) {
    if (!el || el.tagName !== 'TEXTAREA') return;
    var cs = window.getComputedStyle(el);
    var line = px(cs.lineHeight) || px(cs.fontSize) * 1.5 || 20;
    var lines = parseInt(el.getAttribute('data-autosize-max') || '10', 10) || 10;
    var chrome = px(cs.paddingTop) + px(cs.paddingBottom);
    if (cs.boxSizing === 'border-box') chrome += px(cs.borderTopWidth) + px(cs.borderBottomWidth);
    var max = Math.round(line * lines + chrome);
    el.style.height = 'auto';
    var want = el.scrollHeight + (cs.boxSizing === 'border-box' ? px(cs.borderTopWidth) + px(cs.borderBottomWidth) : 0);
    var h = Math.min(want, max);
    el.style.height = h + 'px';
    el.style.overflowY = want > max ? 'auto' : 'hidden';
  }

  /**
   * @param {Element} el
   * @returns {boolean} Whether el is an auto-sizing textarea.
   */
  function isBox(el) { return el && el.matches && el.matches('textarea[data-autosize]'); }

  document.addEventListener('input', function (e) { if (isBox(e.target)) resize(e.target); });
  document.addEventListener('focusin', function (e) { if (isBox(e.target)) resize(e.target); });
  document.addEventListener('reset', function (e) {
    var form = e.target;
    if (!form || !form.querySelectorAll) return;
    setTimeout(function () { form.querySelectorAll('textarea[data-autosize]').forEach(resize); }, 0);
  }, true);
  document.addEventListener('keydown', function (e) {
    var el = e.target;
    if (e.key !== 'Enter' || e.shiftKey || e.isComposing || e.defaultPrevented || !isBox(el) || !el.hasAttribute('data-enter-submits')) return;
    var menu = document.querySelector('.mention-menu:not([hidden])');
    if (menu) return;
    if (!el.value.trim()) { e.preventDefault(); return; }
    var form = el.form;
    if (!form) return;
    e.preventDefault();
    if (form.requestSubmit) form.requestSubmit(); else form.submit();
  });

  /**
   * Resize every auto-sizing textarea under root (the document by default).
   * Also run on DOMContentLoaded and on each htmx:load. Resizing after a form
   * reset is deferred a tick, because "reset" fires before the values change.
   * @param {ParentNode} [root]
   */
  function scan(root) { (root || document).querySelectorAll('textarea[data-autosize]').forEach(resize); }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', function () { scan(); });
  else scan();
  document.addEventListener('htmx:load', function (e) { scan(e.target); });

  window.MetanoiaAutosize = { resize: resize, scan: scan };
})();
