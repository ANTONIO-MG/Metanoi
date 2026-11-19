/**
 * @file Upload progress for every file form on the site — no per-page wiring.
 *
 * Any POST form with enctype="multipart/form-data" that has a file chosen is
 * sent with XMLHttpRequest instead, so the bytes going up can be shown: a panel
 * with the file names, a percentage bar, MB sent, speed and time left, and a
 * Cancel button. Once every byte is up it switches to "Processing on the
 * server…" (unzipping a pack, converting a video) until the answer comes back.
 *
 * The request carries X-Upload-Progress: 1, and core.upload_progress turns a
 * redirect answer into {"redirect": url}, so the browser then goes there as a
 * normal page load and the "Uploaded" message shows as usual. Any other answer
 * (a form shown again with its errors) replaces the page, as a normal submit would.
 *
 * Opt out with data-no-upload-progress on the form. Forms whose own JavaScript
 * calls preventDefault() are left alone: the listener runs in the bubble phase
 * on the document, so a form's own submit handler runs first and
 * ``defaultPrevented`` says it has already dealt with the submit. Forms that
 * target another window are left alone too.
 *
 * A file coming back (an export) is handed to the browser as a download. A
 * 413 answer, a dropped connection or another 4xx/5xx shows the reason in the
 * panel. Loaded site-wide; guarded by ``window.__uploadProgress``. Exports
 * ``window.uploadProgress.send(form, submitter)``, a small helper other
 * scripts can use for their own XHR uploads.
 */
(function () {
  'use strict';
  if (!window.XMLHttpRequest || !window.FormData || window.__uploadProgress) return;
  window.__uploadProgress = true;

  var doc = document;

  /** Every file chosen in the form's enabled file inputs. */
  function chosenFiles(form) {
    var files = [];
    Array.prototype.forEach.call(form.querySelectorAll('input[type=file]'), function (input) {
      if (input.disabled || !input.files) return;
      Array.prototype.forEach.call(input.files, function (f) { files.push(f); });
    });
    return files;
  }

  /** Human-readable size (KB / MB / GB). */
  function mb(bytes) {
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(0) + ' KB';
    if (bytes < 1024 * 1024 * 1024) return (bytes / 1024 / 1024).toFixed(1) + ' MB';
    return (bytes / 1024 / 1024 / 1024).toFixed(2) + ' GB';
  }

  /** Human-readable duration ("42 s", "3 min 5 s"); '' when unknown. */
  function duration(seconds) {
    if (!isFinite(seconds) || seconds < 0) return '';
    if (seconds < 60) return Math.max(1, Math.round(seconds)) + ' s';
    var m = Math.floor(seconds / 60), s = Math.round(seconds % 60);
    return m + ' min' + (s ? ' ' + s + ' s' : '');
  }

  /** HTML-escape ``text``. */
  function esc(text) {
    var d = doc.createElement('div'); d.textContent = text; return d.innerHTML;
  }

  var STYLE = '' +
    '.upp-back{position:fixed;inset:0;z-index:2000;background:rgba(15,23,42,.45);display:flex;align-items:center;justify-content:center;padding:16px}' +
    '.upp-card{width:100%;max-width:440px;background:var(--tc-surface,#fff);color:var(--tc-ink,#1f2937);border-radius:14px;box-shadow:0 20px 50px rgba(0,0,0,.25);padding:20px 22px}' +
    '.upp-card h6{margin:0 0 4px;font-size:1rem;font-weight:700;color:inherit}' +
    '.upp-files{font-size:.8rem;color:var(--tc-muted,#6B5E55);margin-bottom:12px;max-height:3.6em;overflow:hidden;word-break:break-all}' +
    '.upp-track{height:10px;border-radius:5px;background:var(--tc-line,rgba(52,71,103,.14));overflow:hidden}' +
    '.upp-fill{height:100%;width:0;border-radius:5px;background:var(--tc-primary,#0B5FC6);transition:width .2s linear}' +
    '.upp-fill.is-busy{width:100%!important;background-image:linear-gradient(45deg,rgba(255,255,255,.25) 25%,transparent 25%,transparent 50%,rgba(255,255,255,.25) 50%,rgba(255,255,255,.25) 75%,transparent 75%);background-size:20px 20px;animation:upp-stripe 1s linear infinite}' +
    '.upp-fill.is-error{background:#dc3545;width:100%!important}' +
    '@keyframes upp-stripe{to{background-position:20px 0}}' +
    '@media (prefers-reduced-motion:reduce){.upp-fill.is-busy{animation:none}.upp-fill{transition:none}}' +
    '.upp-row{display:flex;justify-content:space-between;gap:8px;font-size:.8rem;margin-top:8px;color:var(--tc-muted,#6B5E55);font-variant-numeric:tabular-nums}' +
    '.upp-pct{font-weight:700;color:var(--tc-ink-strong,#3E2415);font-size:.95rem}' +
    '.upp-actions{display:flex;justify-content:flex-end;gap:8px;margin-top:14px}' +
    '.upp-msg{font-size:.82rem;margin-top:10px}';

  /**
   * Show the progress panel (injecting its styles once).
   * @param {File[]} files Files being sent.
   * @param {number} total Their combined size in bytes.
   * @returns {Object} Handles on the panel's parts, plus ``close()``.
   */
  function panel(files, total) {
    if (!doc.getElementById('upp-style')) {
      var s = doc.createElement('style'); s.id = 'upp-style'; s.textContent = STYLE; doc.head.appendChild(s);
    }
    var names = files.map(function (f) { return f.name; });
    var back = doc.createElement('div');
    back.className = 'upp-back';
    back.innerHTML =
      '<div class="upp-card" role="dialog" aria-modal="true" aria-labelledby="upp-title">' +
        '<h6 id="upp-title"><i class="bi bi-cloud-arrow-up me-1"></i><span data-upp="title">Uploading…</span></h6>' +
        '<div class="upp-files">' + esc(names.length > 3 ? names.slice(0, 3).join(', ') + ' and ' + (names.length - 3) + ' more' : names.join(', ')) +
          ' · ' + mb(total) + '</div>' +
        '<div class="upp-track" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0" aria-label="Upload progress"><div class="upp-fill"></div></div>' +
        '<div class="upp-row"><span class="upp-pct" data-upp="pct">0%</span><span data-upp="detail">starting…</span></div>' +
        '<div class="upp-msg" data-upp="msg" aria-live="polite"></div>' +
        '<div class="upp-actions"><button type="button" class="btn btn-sm btn-outline-secondary mb-0" data-upp="cancel">Cancel</button></div>' +
      '</div>';
    doc.body.appendChild(back);
    function q(name) { return back.querySelector('[data-upp="' + name + '"]'); }
    return {
      el: back, fill: back.querySelector('.upp-fill'), track: back.querySelector('.upp-track'),
      title: q('title'), pct: q('pct'), detail: q('detail'), msg: q('msg'), cancel: q('cancel'),
      close: function () { if (back.parentNode) back.parentNode.removeChild(back); }
    };
  }

  /** Undo the submit buttons' loading state and hide the app loader. */
  function restoreButtons(form) {
    Array.prototype.forEach.call(form.querySelectorAll('.is-loading'), function (b) { b.classList.remove('is-loading'); });
    try { window.appLoader && window.appLoader.hide(); } catch (e) {}
  }

  /** Replace the document with ``html`` (a re-shown form), as a normal submit would. */
  function replacePage(html, url) {
    try { if (url && url !== location.href) history.replaceState(null, '', url); } catch (e) {}
    doc.open(); doc.write(html); doc.close();
  }

  /**
   * Submit ``form`` by XHR with the progress panel. Leaving the page while
   * bytes are still going up asks first. Once every byte is on the server the
   * panel switches to "processing" (unpacking / saving / converting).
   *
   * @param {HTMLFormElement} form
   * @param {HTMLElement|null} submitter The button that submitted it, if any.
   */
  function send(form, submitter) {
    var files = chosenFiles(form);
    var total = files.reduce(function (n, f) { return n + f.size; }, 0);
    var data;
    try { data = submitter ? new FormData(form, submitter) : new FormData(form); }
    catch (e) {
      data = new FormData(form);
      if (submitter && submitter.name) data.append(submitter.name, submitter.value || '');
    }
    var action = (submitter && submitter.getAttribute('formaction')) || form.getAttribute('action') || location.href;
    var ui = panel(files, total);
    var xhr = new XMLHttpRequest();
    var started = Date.now(), uploading = true, processingTimer = null;

    function leaveGuard(e) { if (uploading) { e.preventDefault(); e.returnValue = ''; return ''; } }
    window.addEventListener('beforeunload', leaveGuard);
    function done() {
      uploading = false;
      window.removeEventListener('beforeunload', leaveGuard);
      if (processingTimer) clearInterval(processingTimer);
    }
    function fail(text) {
      done();
      ui.title.textContent = 'Upload failed';
      ui.fill.className = 'upp-fill is-error';
      ui.pct.textContent = '';
      ui.detail.textContent = '';
      ui.msg.innerHTML = '<span class="text-danger">' + esc(text) + '</span>';
      ui.cancel.textContent = 'Close';
      ui.cancel.onclick = function () { ui.close(); restoreButtons(form); };
      restoreButtons(form);
    }

    ui.cancel.onclick = function () { xhr.abort(); };

    xhr.upload.onprogress = function (e) {
      if (!e.lengthComputable) return;
      var pct = Math.min(100, Math.floor(e.loaded / e.total * 100));
      var seconds = (Date.now() - started) / 1000;
      var speed = seconds > 0.3 ? e.loaded / seconds : 0;
      ui.fill.style.width = pct + '%';
      ui.track.setAttribute('aria-valuenow', pct);
      ui.pct.textContent = pct + '%';
      ui.detail.textContent = mb(e.loaded) + ' of ' + mb(e.total) +
        (speed ? ' · ' + mb(speed) + '/s · ' + duration((e.total - e.loaded) / speed) + ' left' : '');
      try { window.appLoader && window.appLoader.progress(pct < 100 ? pct : 99); } catch (err) {}
    };
    xhr.upload.onload = function () {
      ui.title.textContent = 'Upload complete — processing on the server…';
      ui.fill.className = 'upp-fill is-busy';
      ui.pct.textContent = '100%';
      ui.track.setAttribute('aria-valuenow', 100);
      var t0 = Date.now();
      ui.detail.textContent = 'working…';
      processingTimer = setInterval(function () {
        ui.detail.textContent = 'working… ' + duration((Date.now() - t0) / 1000);
      }, 1000);
      ui.msg.textContent = 'Large packs and videos can take a minute. Keep this tab open.';
      ui.cancel.disabled = true;
    };
    xhr.onabort = function () {
      done(); ui.close(); restoreButtons(form);
    };
    xhr.onerror = function () {
      fail('The connection dropped before the upload finished. Check your internet and try again.');
    };
    xhr.ontimeout = xhr.onerror;
    xhr.onload = function () {
      done();
      var type = xhr.getResponseHeader('Content-Type') || '';
      var blob = xhr.response;
      if (xhr.getResponseHeader('X-Upload-Redirect')) {
        blob.text().then(function (text) {
          var target = '';
          try { target = JSON.parse(text).redirect; } catch (e) {}
          ui.title.textContent = 'Done — opening…';
          location.assign(target || location.href);
        });
        return;
      }
      if (xhr.status === 413) {
        fail('That file is larger than the server accepts. Try a smaller file, or ask a developer to raise the limit.');
        return;
      }
      if (type.indexOf('text/html') === 0) {
        blob.text().then(function (html) { replacePage(html, xhr.responseURL || action); });
        return;
      }
      if (xhr.status >= 400) {
        fail('The server answered ' + xhr.status + (xhr.statusText ? ' (' + xhr.statusText + ')' : '') + '. Nothing may have been saved — try again.');
        return;
      }
      var disposition = xhr.getResponseHeader('Content-Disposition') || '';
      var match = /filename="?([^";]+)"?/i.exec(disposition);
      var a = doc.createElement('a');
      a.href = URL.createObjectURL(blob); a.download = match ? match[1] : 'download';
      doc.body.appendChild(a); a.click(); a.remove();
      ui.close(); restoreButtons(form);
    };

    xhr.open('POST', action);
    xhr.responseType = 'blob';
    xhr.setRequestHeader('X-Upload-Progress', '1');
    xhr.send(data);
  }

  doc.addEventListener('submit', function (e) {
    var form = e.target;
    if (e.defaultPrevented || !form || form.tagName !== 'FORM') return;
    if ((form.getAttribute('method') || 'get').toLowerCase() !== 'post') return;
    var submitter = e.submitter || null;
    var enctype = (submitter && submitter.getAttribute('formenctype')) || form.getAttribute('enctype') || '';
    if (enctype.toLowerCase() !== 'multipart/form-data') return;
    if (form.hasAttribute('data-no-upload-progress')) return;
    var target = (submitter && submitter.getAttribute('formtarget')) || form.getAttribute('target');
    if (target && target !== '_self') return;
    if (!chosenFiles(form).length) return;
    e.preventDefault();
    send(form, submitter);
  });

  window.uploadProgress = { send: send };
})();
