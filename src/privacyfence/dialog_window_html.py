"""Small-dialog HTML template for the confirmation/list-picker dialogs
web_approval_ui.py and settings_controller.py serve (through
web_prompt.py), using the same in-page button row and bridge script as the
approval window. Reuses the card's stylesheets (the shared design files and
approval_window_html.py's ``styles.css``: tokens, the ``.button`` styles, the
decision row and its compact layout, the ``pf-card`` size container) rather
than a second copy of the same visual language --
these are just much smaller, fixed-shape documents than
``build_card_stack_html``'s full card stack, with no header icon/pill, no
preview pane, no PII/disclosure cards.

Two shapes:
  - ``build_confirmation_html()``: a two-button Cancel/<confirm_label> row --
    the shape both ``show_pii_confirmation_popup`` and
    ``show_rule_confirmation_popup`` render, with different title/copy/
    confirm_label but otherwise identical. Cancel is the safe default: the
    accepting button carries ``data-pf-primary`` (the same attribute
    ``approval_window_html.py``'s own Allow once button uses), which
    ``_JS``'s keydown handler deliberately excludes from the Enter/Space-
    activates-a-focused-control path -- hitting Enter can never silently
    accept. Escape resolves Cancel from anywhere in the document.
  - ``build_choice_html()``: a vertical list of clickable option rows plus a
    Cancel button -- the shape both ``show_rule_choice_popup`` and
    ``settings_controller._pick_resource_index``'s Atlassian multi-resource
    picker render. Escape or Cancel resolve to no selection: the caller gets
    ``None`` rather than an index (web_prompt.py).

Bridge protocol (JS -> Python only, same shape as approval_window_html.py's
own): the page posts
``window.webkit.messageHandlers.pf.postMessage({action: 'resolve', result})``
once a button/option resolves the dialog. ``result`` is ``'cancel'``/
``'confirm'`` for the confirmation shape, or a chosen option's index (a
number) / ``'cancel'`` for the choice shape.

Every value interpolated into these documents -- button labels, dialog copy,
and (for the choice shape) each option's own display text -- is run through
``_html_escape()`` before interpolation, the same defensive posture
``build_card_stack_html`` takes with ``details_text``. An option's text can
come from outside (an OAuth ``accessible-resources`` site name or URL), so it
must stay text and never become markup.
"""
from __future__ import annotations

from html import escape as _html_escape

from .approval_window_html import _STYLES_CSS, _new_nonce
from .design_css import DOCUMENT_CSS

# The widest each dialog's card gets; a narrower screen gets all of its
# width (``_document``'s ``min(..., 100%)``).
CONFIRM_WIDTH = 440
PICKER_WIDTH = 480

# Click/keyboard dispatch, plus the "content is actually ready" gate -- the
# same DOMContentLoaded-is-the-right-signal reasoning as approval_window_
# html.py's own _JS (nothing here ever fetches anything either: fonts/colors
# come from the same already-inlined styles.css, and there are no images at
# all in these two shapes). window.__pfEnableButtons is exposed the same way
# too, and likewise nothing in the web UI calls it.
_JS = """
(function () {
  function post(result) {
    if (window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.pf) {
      window.webkit.messageHandlers.pf.postMessage({ action: 'resolve', result: result });
    }
  }

  function resultFor(el) {
    if (el.getAttribute('data-pf-action') === 'choice') {
      return parseInt(el.getAttribute('data-pf-index'), 10);
    }
    return el.getAttribute('data-pf-action');
  }

  function resolveFrom(el) {
    if (!el || el.getAttribute('aria-disabled') === 'true') return;
    if (!el.getAttribute('data-pf-action')) return;
    post(resultFor(el));
  }

  function enableButtons() {
    var controls = document.querySelectorAll('[data-pf-action]');
    for (var i = 0; i < controls.length; i++) {
      controls[i].removeAttribute('aria-disabled');
      controls[i].setAttribute('tabindex', '0');
    }
  }
  window.__pfEnableButtons = enableButtons;

  document.addEventListener('DOMContentLoaded', function () {
    enableButtons();

    document.body.addEventListener('click', function (e) {
      resolveFrom(e.target.closest('[data-pf-action]'));
    });

    document.body.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') {
        // Resolves Cancel from anywhere, not just when focused -- declining
        // via a reflexive keypress is always the safe direction. Present on
        // both shapes: build_confirmation_html's Cancel button and build_
        // choice_html's own Cancel button both carry data-pf-action="cancel".
        resolveFrom(document.querySelector('[data-pf-action="cancel"]'));
        return;
      }
      // See build_confirmation_html's own docstring for why data-pf-primary
      // (the accepting button) is deliberately excluded here -- mirrors
      // approval_window_html.py's own Allow once exclusion.
      if ((e.key === 'Enter' || e.key === ' ') && e.target.closest) {
        var interactive = e.target.closest('[data-pf-action]:not([data-pf-primary])');
        if (interactive) {
          e.preventDefault();
          resolveFrom(interactive);
        }
      }
    });
  });
})();
"""


def _confirm_button_row_html(cancel_label: str, confirm_label: str) -> str:
    """Cancel (left) / <confirm_label> (right, primary) -- same left/right
    grouping as approval_window_html.py's own _button_row_html (Deny left,
    Allow once right)."""
    cancel_html = (
        '<div class="button danger pf-btn-deny" role="button" aria-disabled="true" '
        f'aria-label="{_html_escape(cancel_label)}" data-pf-action="cancel">{_html_escape(cancel_label)}</div>'
    )
    confirm_html = (
        '<div class="button primary pf-btn-primary" role="button" aria-disabled="true" '
        f'data-pf-primary="1" aria-label="{_html_escape(confirm_label)}" data-pf-action="confirm">'
        f'{_html_escape(confirm_label)}</div>'
    )
    return f'<div class="pf-btn-row"><div class="pf-btn-row-left">{cancel_html}</div>{confirm_html}</div>'


def _cancel_only_button_row_html(cancel_label: str) -> str:
    cancel_html = (
        '<div class="button danger pf-btn-deny" role="button" aria-disabled="true" '
        f'aria-label="{_html_escape(cancel_label)}" data-pf-action="cancel">{_html_escape(cancel_label)}</div>'
    )
    return f'<div class="pf-btn-row"><div class="pf-btn-row-left">{cancel_html}</div></div>'


def _message_html(lines: list[str]) -> str:
    """Each non-empty line becomes its own paragraph. Empty lines (the old
    AppleScript ``lines`` lists used them purely as inter-paragraph spacing,
    see approval_popup.py's former ``_build_message``) are dropped rather
    than rendered as an empty ``<p>`` -- normal CSS paragraph margin already
    provides that spacing here."""
    return "".join(f"<p>{_html_escape(line)}</p>" for line in lines if line)


def _document(*, width: int, body_html: str) -> str:
    """Shared page shell for both shapes -- same overall structure as
    approval_window_html.build_card_stack_html's own returned document
    (vendored styles.css, a couple of small overrides, the bridge script),
    just without that function's per-layout width/rail-color logic, since
    both shapes here are one fixed narrow width apiece.

    Generates its own fresh CSP nonce (see approval_window_html.py's
    module-level Content-Security-Policy note on why this document -- rendered once, served
    unchanged thereafter -- needs one baked in at build time rather than
    per response) and reuses ``approval_window_html.extract_csp_nonce``'s
    own ``<script nonce="...">`` tag shape so the same extraction code
    recovers it later, from either kind of document."""
    nonce = _new_nonce()
    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<!-- See approval_window_html.build_card_stack_html's own head: without
     this a phone renders the document in a ~980px viewport at ~40% scale. -->
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<style nonce="{nonce}">
{DOCUMENT_CSS}
{_STYLES_CSS}
html {{ height: 100%; }}
html, body {{ overflow-y: auto; }}
/* The card root (styles.css's .pf-card-root, the pf-card size container):
   at most {width}px, centred in a wider tab, all of a narrower one. Every
   dialog is under styles.css's 600px, so it is always a compact card: a page
   that grows with its content, with Cancel and the accepting button as two
   equal full-height touch targets. */
.pf-card-root {{ width: min({width}px, 100%); }}
.pf-dialog {{ padding: 24px 20px; }}
h2 {{ font-size: 19px; margin-bottom: 12px; overflow-wrap: anywhere; }}
.pf-choice-list {{ display: flex; flex-direction: column; gap: 6px; margin: 4px 0 15px; }}
/* An option is visibly a control at rest -- a bordered row the height of a
   tap target -- and the highlight a mouse gets on hover, a keyboard gets on
   focus and a finger on press, so no state is reachable by hover alone. */
.pf-choice-row {{
  display: flex; align-items: center; min-height: var(--tap);
  padding: 10px 14px; border-radius: var(--radius-s);
  background: var(--surface); color: var(--ink); font-size: 14px;
  border: 1px solid var(--control-line); overflow-wrap: anywhere;
  cursor: pointer; user-select: none;
}}
.pf-choice-row:hover, .pf-choice-row:focus-visible, .pf-choice-row:active {{
  background: var(--accent-soft); border-color: var(--accent);
}}
.pf-choice-row[aria-disabled="true"] {{ opacity: .45; }}
</style>
</head>
<body><div class="pf-card-root"><div class="pf-card pf-dialog">{body_html}</div></div><script nonce="{nonce}">{_JS}</script></body>
</html>
"""


def build_confirmation_html(
    *, title: str, message_lines: list[str], cancel_label: str, confirm_label: str,
) -> str:
    """Two-button Cancel/<confirm_label> dialog. See module docstring for
    why Cancel is the default."""
    body_html = (
        '<div class="pf-kicker"><span>PrivacyFence</span></div>'
        f'<h2>{_html_escape(title)}</h2>'
        f'<div class="pf-dialog-message">{_message_html(message_lines)}</div>'
        f'{_confirm_button_row_html(cancel_label, confirm_label)}'
    )
    return _document(width=CONFIRM_WIDTH, body_html=body_html)


def build_choice_html(
    *, title: str, prompt: str, options: list[str], cancel_label: str = "Cancel",
) -> str:
    """A vertical list of clickable option rows plus Cancel. See module
    docstring for why Escape and Cancel return no selection."""
    rows = "".join(
        '<div class="pf-choice-row" role="button" aria-disabled="true" '
        f'aria-label="{_html_escape(opt)}" data-pf-action="choice" data-pf-index="{i}">'
        f'{_html_escape(opt)}</div>'
        for i, opt in enumerate(options)
    )
    body_html = (
        '<div class="pf-kicker"><span>PrivacyFence</span></div>'
        f'<h2>{_html_escape(title)}</h2>'
        f'<p>{_html_escape(prompt)}</p>'
        f'<div class="pf-choice-list">{rows}</div>'
        f'{_cancel_only_button_row_html(cancel_label)}'
    )
    return _document(width=PICKER_WIDTH, body_html=body_html)
