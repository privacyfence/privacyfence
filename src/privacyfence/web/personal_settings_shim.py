"""Browser half of "Move your settings" (Settings > Auto-accept).

``personal_transfer_shim`` returns the ``<script nonce>`` that both settings pages carry right after
``routes_settings._settings_bridge_shim``. Export is a plain navigation to the download route. Import
chooses a file, POSTs it to the preview route, and shows what the import would do in a dialog; the
confirm button re-posts the *same* ``File`` with the previewed hash (never read into a string), and
completes a ``428`` step-up answer with ``pfWebauthnGet`` the way ``attemptOrgUpload`` does.

The dialog is appended to ``document.body``, not ``#app``, because ``render()`` rebuilds ``#app`` on
every state push. Every string that came from the preview reaches the DOM through ``textContent``
only -- a rule sentence or a skip reason is file content. Only ``fetch`` is used (the local CSP has
``form-action 'none'``).
"""
from __future__ import annotations

from html import escape

from ..html_embed import json_for_script

EXPORT_URL = "/api/settings/personal/export"
PREVIEW_URL = "/api/settings/personal/preview"
IMPORT_URL = "/api/settings/personal/import"

_SCRIPT = r"""
(function () {
  var CSRF = __CSRF__;
  var EXPORT_URL = __EXPORT_URL__, PREVIEW_URL = __PREVIEW_URL__, IMPORT_URL = __IMPORT_URL__;
  var GROUPS = [
    ['add', 'Will be added'], ['extend', 'Will be extended'],
    ['present', 'Already there'], ['skip', 'Skipped'],
  ];
  var fileInput = document.createElement('input');
  fileInput.type = 'file';
  fileInput.accept = '.json,application/json';
  fileInput.style.display = 'none';
  fileInput.setAttribute('aria-hidden', 'true');
  fileInput.setAttribute('data-pf-personal-file', '1');
  document.body.appendChild(fileInput);
  var dialog = null;

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) { node.className = className; }
    if (text !== undefined) { node.textContent = text; }
    return node;
  }

  function button(label, primary) {
    var b = el('div', primary ? 'button primary' : 'button', label);
    b.setAttribute('role', 'button');
    b.setAttribute('tabindex', '0');
    return b;
  }

  function closeDialog() {
    if (!dialog) { return; }
    var d = dialog;
    dialog = null;
    document.removeEventListener('keydown', d.onKey, true);
    d.overlay.remove();
    if (d.opener && d.opener.focus) { d.opener.focus(); }
  }

  function post(url, file, fields) {
    var fd = new FormData();
    fd.append('file', file);
    fd.append('csrf', CSRF);
    Object.keys(fields || {}).forEach(function (k) { fd.append(k, fields[k]); });
    return fetch(url, {method: 'POST', credentials: 'same-origin', body: fd});
  }

  function errorText(r) {
    return r.json().then(function (data) {
      return (data && data.error) || ('Request failed (' + r.status + ').');
    }, function () { return 'Request failed (' + r.status + ').'; });
  }

  function completeImport(file, sha256, assertion) {
    var fields = {sha256: sha256, confirmed: 'true'};
    if (assertion) { fields.webauthn_assertion = JSON.stringify(assertion); }
    return post(IMPORT_URL, file, fields).then(function (r) {
      if (r.status === 428) {
        return r.json().then(function (data) {
          if (data.webauthn_options && window.PublicKeyCredential) {
            return pfWebauthnGet(JSON.stringify(data.webauthn_options)).then(function (a) {
              return completeImport(file, sha256, a);
            }).catch(function (err) {
              window.alert('This change needs your passkey, and the prompt failed: ' + err.message);
              return false;
            });
          }
          window.alert('This change needs a passkey, and none is available in this browser.');
          return false;
        });
      }
      if (r.status === 403) {
        return r.json().then(function (data) {
          if (data.enroll_url) {
            window.alert('This change requires a passkey. Set one up at ' + data.enroll_url + '.');
          } else {
            window.alert(data.error || 'Not allowed.');
          }
          return false;
        });
      }
      if (!r.ok) {
        return errorText(r).then(function (msg) { window.alert(msg); return false; });
      }
      return true;
    });
  }

  function stepUpLine(step) {
    if (step.status === 'apply') {
      return 'Your step-up level will be set to "' + step.scope + '".';
    }
    return step.reason || '';
  }

  function openDialog(preview, file, opener) {
    closeDialog();
    var counts = preview.counts || {};
    var step = preview.step_up || {};
    var willImport = (counts.add || 0) + (counts.extend || 0);

    var overlay = el('div', 'pf-modal-overlay');
    overlay.setAttribute('role', 'presentation');
    var box = el('div', 'pf-modal');
    box.style.width = 'min(640px, 100%)';
    box.style.maxHeight = '90vh';
    box.style.overflowY = 'auto';
    box.setAttribute('role', 'dialog');
    box.setAttribute('aria-modal', 'true');
    box.setAttribute('aria-label', 'Import your settings');
    box.appendChild(el('div', 'pf-modal-title', 'Import your settings'));

    var origin = 'Settings file' + (preview.source_mode ? ' from a ' + preview.source_mode + ' install' : '') +
      (preview.exported_at ? ', exported ' + preview.exported_at : '') + '.';
    box.appendChild(el('div', 'pf-modal-desc', origin));

    GROUPS.forEach(function (g) {
      var items = (preview.items || []).filter(function (it) { return it.status === g[0]; });
      if (!items.length) { return; }
      var section = el('div', 'pf-pt-group');
      section.setAttribute('data-pf-personal-group', g[0]);
      section.appendChild(el('div', 'pf-modal-title', g[1] + ' (' + items.length + ')'));
      var list = el('ul', 'pf-pt-list');
      items.forEach(function (it) {
        var li = el('li', 'pf-pt-item');
        li.appendChild(el('span', 'pf-pt-sentence', it.sentence));
        if (it.reason) {
          li.appendChild(el('span', 'pf-modal-desc', ' -- ' + it.reason));
        }
        list.appendChild(li);
      });
      section.appendChild(list);
      box.appendChild(section);
    });

    var stepText = stepUpLine(step);
    if (stepText) {
      var stepNode = el('div', 'pf-modal-desc', stepText);
      stepNode.setAttribute('data-pf-personal-stepup', step.status || '');
      box.appendChild(stepNode);
    }
    (preview.warnings || []).forEach(function (w) {
      var warn = el('div', 'pf-modal-error', w);
      warn.setAttribute('role', 'status');
      box.appendChild(warn);
    });

    var buttons = el('div', 'pf-modal-buttons');
    var cancel = button('Cancel', false);
    cancel.setAttribute('aria-label', 'Cancel import');
    var confirm = button('Import ' + willImport + ' rule(s)', true);
    confirm.setAttribute('data-pf-personal-confirm', '1');
    var disabled = willImport === 0 && step.status !== 'apply';
    if (disabled) {
      confirm.setAttribute('aria-disabled', 'true');
      confirm.style.opacity = '0.5';
    }
    var busy = false;
    cancel.addEventListener('click', closeDialog);
    confirm.addEventListener('click', function () {
      if (disabled || busy) { return; }
      busy = true;
      completeImport(file, preview.sha256, null).then(function (ok) {
        busy = false;
        if (ok) { closeDialog(); window.location.reload(); }
      }, function (err) {
        busy = false;
        window.alert('The import failed: ' + err.message);
      });
    });
    buttons.appendChild(cancel);
    buttons.appendChild(confirm);
    box.appendChild(buttons);
    overlay.appendChild(box);

    function onKey(ev) {
      if (ev.key === 'Escape') {
        ev.preventDefault();
        ev.stopPropagation();
        closeDialog();
      } else if (ev.key === 'Tab') {
        var first = cancel, last = confirm;
        if (ev.shiftKey && document.activeElement === first) { ev.preventDefault(); last.focus(); }
        else if (!ev.shiftKey && document.activeElement === last) { ev.preventDefault(); first.focus(); }
      }
    }
    document.addEventListener('keydown', onKey, true);
    document.body.appendChild(overlay);
    dialog = {overlay: overlay, onKey: onKey, opener: opener};
    cancel.focus();
  }

  var opener = null;
  document.addEventListener('click', function (e) {
    var target = e.target && e.target.closest ? e.target : null;
    if (!target) { return; }
    if (target.closest('[data-pf-personal-export]')) {
      window.location = EXPORT_URL;
      return;
    }
    var importEl = target.closest('[data-pf-personal-import]');
    if (importEl) {
      opener = importEl;
      fileInput.click();
    }
  });

  fileInput.addEventListener('change', function () {
    if (!fileInput.files || !fileInput.files[0]) { return; }
    var file = fileInput.files[0];
    post(PREVIEW_URL, file).then(function (r) {
      if (!r.ok) {
        return errorText(r).then(function (msg) { window.alert(msg); });
      }
      return r.json().then(function (preview) { openDialog(preview, file, opener); });
    }).catch(function (err) {
      window.alert('Could not read that file: ' + err.message);
    });
  });
  // Choosing the same file twice in a row must still fire "change".
  fileInput.addEventListener('click', function () { fileInput.value = ''; });
})();
"""


def personal_transfer_shim(*, csrf: str, nonce: str) -> str:
    """The ``<script nonce>`` for "Move your settings". ``csrf`` goes in through
    ``json_for_script`` (a Python ``repr`` would leave ``</script>`` as it is)."""
    script = (
        _SCRIPT
        .replace("__CSRF__", json_for_script(csrf))
        .replace("__EXPORT_URL__", json_for_script(EXPORT_URL))
        .replace("__PREVIEW_URL__", json_for_script(PREVIEW_URL))
        .replace("__IMPORT_URL__", json_for_script(IMPORT_URL))
    )
    return f'<script nonce="{escape(nonce, quote=True)}">{script}</script>'
