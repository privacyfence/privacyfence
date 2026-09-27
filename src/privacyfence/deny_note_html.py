"""The "Deny with a note…" panel: markup and script, shared by every page that offers it (ADR 0082).

A denial may carry an ``intent`` from ``deny_feedback.INTENTS``, picked from chips, and a free-text
``note``. This module renders the panel that collects them (``panel_html``) and the script that
runs it (``PANEL_JS``), once, so the approval card (approval_window_html.py) and the approval list
use one implementation. Each document is self-contained and inlines both.

**Nothing but the person's typing ever fills the note.** ``panel_html`` takes no content argument:
the textarea always renders empty, no chip is checked, and every string in the panel is a constant
of this module or of ``deny_feedback``. ``PANEL_JS`` reads only the textarea and the chips inside
its own panel, and nothing copies card content (the preview, the summary, the agent's stated
reason) into them. That is what makes the note the deciding human's words and nothing else.

The panel is hidden until its host opens it, and it decides nothing by itself: *Deny and send*
calls the host's ``onSubmit`` with ``getFeedback()``, and the host posts the deny. It carries no
``data-pf-action``, so the host's own decision wiring (Escape, ``enableButtons``) never mistakes it
for a decision control. Styling is the design system's: ``.card``, ``.stack`` and ``.cluster``,
``textarea.field``, ``.button``, and ``.chip`` (resources/design/app.css). No inline style and no
colour literal.
"""
from __future__ import annotations

from html import escape as _html_escape

from .deny_feedback import INTENTS, MAX_NOTE_CHARS

KICKER = "Tell the agent why, or what to do instead (optional)"
PLACEHOLDER = "e.g. Send it only to Anna, not the whole team."
HELP = "Only the agent that made this request receives this note. It is not kept in the audit log."
CANCEL_LABEL = "Cancel"


def panel_html(prefix: str, submit_label: str) -> str:
    """The panel, hidden, as ``<section id="{prefix}">``. Every other id in it starts with
    ``prefix``, and the chips' radio group is named ``{prefix}-intent``, so a page could hold more
    than one. ``submit_label`` is the text of the submit button (*Deny and send* on the card).

    There is deliberately no argument for anything the panel shows: see the module docstring."""
    p = _html_escape(prefix)
    chips = "".join(
        f'<label class="chip"><input type="radio" name="{p}-intent" value="{_html_escape(value)}">'
        f"<span>{_html_escape(label)}</span></label>"
        for value, (label, _guidance) in INTENTS.items()
    )
    return (
        f'<section id="{p}" class="card stack pf-deny-note" aria-labelledby="{p}-title" hidden>'
        f'<div class="kicker" id="{p}-title">{_html_escape(KICKER)}</div>'
        f'<div class="cluster" role="radiogroup" aria-labelledby="{p}-title">{chips}</div>'
        '<div>'
        f'<textarea class="field" id="{p}-text" rows="3" maxlength="{MAX_NOTE_CHARS}" '
        f'aria-label="Note to the agent" aria-describedby="{p}-count {p}-help" '
        f'placeholder="{_html_escape(PLACEHOLDER)}"></textarea>'
        f'<div class="field-help" id="{p}-count" aria-live="polite">0 / {MAX_NOTE_CHARS}</div>'
        f'<div class="field-help" id="{p}-help">{_html_escape(HELP)}</div>'
        '</div>'
        '<div class="cluster">'
        f'<button type="button" class="button secondary" data-pf-note-cancel>{_html_escape(CANCEL_LABEL)}</button>'
        f'<button type="button" class="button danger" data-pf-note-submit>{_html_escape(submit_label)}</button>'
        '</div>'
        '</section>'
    )


# window.pfDenyNotePanel(prefix, {onSubmit, onClose}) wires one panel_html() panel and returns
# {open, close, isOpen, getFeedback}, or null when the page has no such panel.
#
# - The chips are native radios, so arrow keys move between them and a screen reader announces a
#   radio group. A radio cannot normally be unchecked, and choosing an intent is optional: a click
#   (or Space) on the chip that is already checked clears it.
# - The counter follows the textarea. Its maxlength and this count both measure UTF-16 code units;
#   the server counts characters after sanitizing, which is never more, so a note the counter
#   accepts is never refused for its length.
# - Enter and Space in the textarea type. Ctrl+Enter or Cmd+Enter submits.
# - Cancel closes the panel and keeps what was typed and chosen.
# - getFeedback() is {note, intent}, each only when non-empty; a note of only whitespace is none.
PANEL_JS = """
(function () {
  window.pfDenyNotePanel = function (prefix, options) {
    var panel = document.getElementById(prefix);
    if (!panel) return null;
    options = options || {};
    var text = document.getElementById(prefix + '-text');
    var count = document.getElementById(prefix + '-count');
    var radios = panel.querySelectorAll('input[type="radio"]');
    var chosen = null;

    function updateCount() {
      count.textContent = text.value.length + ' / ' + text.maxLength;
    }

    function getFeedback() {
      var feedback = {};
      if (text.value.trim()) feedback.note = text.value;
      if (chosen && chosen.checked) feedback.intent = chosen.value;
      return feedback;
    }

    function isOpen() { return !panel.hidden; }

    function open() {
      panel.hidden = false;
      updateCount();
      text.focus();
    }

    function close() {
      panel.hidden = true;
      if (options.onClose) options.onClose();
    }

    function submit() {
      if (options.onSubmit) options.onSubmit(getFeedback());
    }

    for (var i = 0; i < radios.length; i++) {
      // By the time click fires the radio is already checked, so comparing it with the one
      // chosen before tells a new choice from a second click on the same chip.
      radios[i].addEventListener('click', function () {
        if (this === chosen) {
          this.checked = false;
          chosen = null;
        } else {
          chosen = this;
        }
      });
      radios[i].addEventListener('change', function () {
        if (this.checked) chosen = this;
      });
    }

    text.addEventListener('input', updateCount);
    text.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
        e.preventDefault();
        submit();
      }
    });
    panel.querySelector('[data-pf-note-cancel]').addEventListener('click', close);
    panel.querySelector('[data-pf-note-submit]').addEventListener('click', submit);

    return {open: open, close: close, isOpen: isOpen, getFeedback: getFeedback};
  };
})();
"""
