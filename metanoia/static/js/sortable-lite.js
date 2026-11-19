/**
 * @file sortable-lite.js — a tiny, dependency-free drag-to-reorder helper.
 *
 * Pointer-based (works with mouse + touch), reorders direct children of a
 * container by dragging a handle, and calls onEnd(newOrderOfIds) after a drop.
 * Used by the lesson block editor (and reusable anywhere a vendored SortableJS
 * would otherwise be needed).
 *
 * Exports ``window.SortableLite.create(containerEl, opts)``; see :func:`create`.
 */
(function (global) {
  'use strict';

  /**
   * Make the matching children of ``container`` draggable by their handle.
   *
   * @param {Element} container Element whose direct children are reordered.
   * @param {Object} [opts]
   * @param {string} [opts.handle='.le-grip'] Selector for the drag handle within an item.
   * @param {string} [opts.itemSelector='.le-block'] Selector identifying draggable items.
   * @param {string} [opts.idAttr='data-id'] Attribute read to build the order array.
   * @param {string} [opts.draggingClass='is-dragging'] Class on the item being dragged.
   * @param {string} [opts.targetClass='drop-target'] Class on the item under the pointer.
   * @param {function(string[])} [opts.onEnd] Called with the new order of ids after a drop.
   * @returns {{destroy: Function}} Handle that removes the listener.
   */
  function create(container, opts) {
    opts = opts || {};
    var handleSel = opts.handle || '.le-grip';
    var itemSel = opts.itemSelector || '.le-block';
    var idAttr = opts.idAttr || 'data-id';
    var draggingClass = opts.draggingClass || 'is-dragging';
    var targetClass = opts.targetClass || 'drop-target';
    var onEnd = opts.onEnd || function () {};

    var dragEl = null, startY = 0, lastTarget = null;

    /** The draggable item containing ``node``, or null. */
    function itemFrom(node) {
      while (node && node !== container) {
        if (node.matches && node.matches(itemSel)) return node;
        node = node.parentNode;
      }
      return null;
    }

    /** Start a drag when the pointer goes down on a handle. */
    function onPointerDown(e) {
      var handle = e.target.closest ? e.target.closest(handleSel) : null;
      if (!handle || !container.contains(handle)) return;
      var item = itemFrom(handle);
      if (!item) return;
      e.preventDefault();
      dragEl = item;
      startY = e.clientY;
      item.classList.add(draggingClass);
      document.addEventListener('pointermove', onPointerMove);
      document.addEventListener('pointerup', onPointerUp, { once: true });
    }

    /** The container's draggable items, in DOM order. */
    function siblings() {
      return Array.prototype.filter.call(container.children, function (c) {
        return c.matches && c.matches(itemSel);
      });
    }

    /** Move the dragged item before/after the item under the pointer's midpoint. */
    function onPointerMove(e) {
      if (!dragEl) return;
      var y = e.clientY;
      var over = null;
      siblings().forEach(function (el) {
        if (el === dragEl) return;
        var r = el.getBoundingClientRect();
        if (y >= r.top && y <= r.bottom) over = el;
      });
      if (lastTarget && lastTarget !== over) lastTarget.classList.remove(targetClass);
      if (over) {
        over.classList.add(targetClass);
        lastTarget = over;
        var r = over.getBoundingClientRect();
        var before = y < r.top + r.height / 2;
        if (before) container.insertBefore(dragEl, over);
        else container.insertBefore(dragEl, over.nextSibling);
      }
    }

    /** End the drag, clear classes and report the new order to ``onEnd``. */
    function onPointerUp() {
      document.removeEventListener('pointermove', onPointerMove);
      if (lastTarget) lastTarget.classList.remove(targetClass);
      if (dragEl) dragEl.classList.remove(draggingClass);
      dragEl = null; lastTarget = null;
      var ids = siblings().map(function (el) { return el.getAttribute(idAttr); });
      onEnd(ids);
    }

    container.addEventListener('pointerdown', onPointerDown);
    return {
      destroy: function () { container.removeEventListener('pointerdown', onPointerDown); }
    };
  }

  global.SortableLite = { create: create };
})(window);
