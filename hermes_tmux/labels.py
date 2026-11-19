"""Human-readable names and state vocabulary shared by every surface.

One place decides how a worker is named and which glyph and colour its state
gets, so the tab, the pane header, the overview and the transcript never
disagree. Nothing here classifies work: a title is the first clause of the goal
the parent wrote, verbatim.

The vocabulary is a four-row table (`_STATES`) plus a fifth cell for "we do not
know". Anything that renders a state goes through `state_kind()` first, which
collapses every string outside {run, stall, done, fail} to "other" — so a new
Hermes exit reason shows as `·` (UNKNOWN) instead of being guessed into a known
badge. Adding a state means adding a row here and nothing else:

===========  ======  =====  =====  =======
kind         glyph   ascii  colour word
===========  ======  =====  =====  =======
run          ●       *      114    RUNNING
stall        ◐       ~      214    SILENT
done         ✓       +      244    DONE
fail         ✗       x      203    FAILED
other        ·       .      244    UNKNOWN
===========  ======  =====  =====  =======

Colours are 256-colour indexes chosen to read on both light and dark themes;
`x`/`+`/`~`/`.` are the ASCII fallbacks used when the locale is not UTF-8 or
`HERMES_TMUX_ASCII` forces it. `ascii_only()` is the single decision point, and
`env.pane_env()` freezes its answer into every pane so no surface can disagree
with the tab it decorates.

Text handling here has one rule: transcript and goal text is untrusted input.
`defuse()` makes it inert, `clean()` makes it one line, and `cell_width()` /
`take_cells()` / `clip()` are the only width arithmetic in the project.
"""

from __future__ import annotations

import os
import re
import unicodedata
from typing import Any, Dict, Iterable, Optional, Tuple

#: Longest window name. The status line shows less of it when space is short.
#: The window is named once and never renamed: `watch._fitted_name` truncates
#: per client at draw time instead, so a resize renames nothing.
TAB_LABEL_WIDTH = 28

#: Dropped from a tab name. Three articles plus one courtesy word: "Please
#: audit X" and "Audit X" must produce the same tab, or the same task dispatched
#: twice in one batch would get two different names.
_ARTICLES = {"the", "a", "an", "please"}
_CLAUSE_BREAKS = (". ", " \u2014 ", " -- ", " - ", ": ", "\n")
_DATE_SUFFIX = re.compile(r"-\d{4,8}$")
_WS = re.compile(r"\s+")
_CTRL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
#: A complete terminal escape sequence: CSI, OSC, DCS/SOS/PM/APC, or a short
#: two-character escape. An unterminated OSC/DCS swallows the rest of the line:
#: there is no way to show the remainder safely.
_ESCAPE_SEQ = re.compile(
    r"\x1b(?:\[[0-?]*[ -/]*[@-~]"
    r"|\][^\x07\x1b]*(?:\x07|\x1b\\)?"
    r"|[PX^_][^\x1b]*(?:\x1b\\)?"
    r"|[@-_])"
)
#: C0 (except tab), DEL and C1 controls, and the bidi overrides that reorder
#: what a reader sees without changing the bytes.
_CTRL_ALL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_BIDI = re.compile("[\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]")

#: state -> (unicode glyph, ascii glyph, 256-colour index, word)
_STATES: Dict[str, Tuple[str, str, int, str]] = {
    "run": ("\u25cf", "*", 114, "RUNNING"),
    "stall": ("\u25d0", "~", 214, "SILENT"),
    "done": ("\u2713", "+", 244, "DONE"),
    "fail": ("\u2717", "x", 203, "FAILED"),
    "other": ("\u00b7", ".", 244, "UNKNOWN"),
}


def ascii_only(environ=None) -> bool:
    """True when glyphs must stay ASCII: forced, or the locale is not UTF-8.

    Precedence: `HERMES_TMUX_ASCII` wins outright (both directions), then the
    first non-empty of `LC_ALL`, `LC_CTYPE`, `LANG` decides. Only the first set
    variable is consulted because that is how libc resolves the locale — a
    UTF-8 `LANG` with a `LC_ALL=C` override must render ASCII.

    With none of them set, the answer is False (UTF-8 assumed). That is the
    optimistic choice: the failure mode of guessing wrong is cosmetic (mojibake
    in place of a glyph), not a broken layout, because `cell_width` measures
    whatever it is given.
    """
    env = os.environ if environ is None else environ
    forced = (env.get("HERMES_TMUX_ASCII") or "").strip().lower()
    if forced in ("1", "true", "yes", "on"):
        return True
    if forced in ("0", "false", "no", "off"):
        return False
    for name in ("LC_ALL", "LC_CTYPE", "LANG"):
        value = env.get(name)
        if value:
            return "utf-8" not in value.lower() and "utf8" not in value.lower()
    return False


def state_kind(label: str) -> str:
    """Collapse a display state to one of run / stall / done / fail / other."""
    return label if label in ("run", "stall", "done", "fail") else "other"


def glyph(kind: str, ascii_mode: bool = False) -> str:
    entry = _STATES.get(kind, _STATES["other"])
    return entry[1] if ascii_mode else entry[0]


def colour(kind: str) -> int:
    return _STATES.get(kind, _STATES["other"])[2]


def word(kind: str) -> str:
    return _STATES.get(kind, _STATES["other"])[3]


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    try:
        return str(value)
    except Exception:
        return ""


def clean(value: Any) -> str:
    """One line of text: control characters and runs of whitespace collapsed."""
    return _WS.sub(" ", _CTRL.sub(" ", _text(value))).strip()


def defuse(value: Any) -> str:
    """Make transcript text inert before it reaches a terminal.

    The file is untrusted input: a tool result, a quoted file, or a model can
    put ESC sequences in it. A raw OSC 52 writes the reader's clipboard, OSC 8
    forges a link, CSI moves the cursor or clears the screen, and bidi controls
    reorder what is displayed without changing what is stored. Escape sequences
    become a space, other C0/C1 controls become a space, bidi controls are
    dropped. Tabs survive; ``clean`` is still the one-line normaliser.
    """
    text = _ESCAPE_SEQ.sub(" ", _text(value))
    text = _CTRL_ALL.sub(" ", text)
    return _BIDI.sub("", text)


def title(goal: Any) -> str:
    """The first clause of a goal: what the parent asked for, in its words.

    Clause breaks are sentence end, dash, double dash, `": "` and newline. A
    break occurring before column 12 is ignored: "Fix: the parser" would
    otherwise become the title "Fix", which names nothing. The first break at or
    after 12 wins; if there is none the whole goal is the title.
    """
    raw = _text(goal)
    best = len(raw)
    for mark in _CLAUSE_BREAKS:
        at = raw.find(mark)
        # A break in the first few characters ("Fix: ...") is a prefix, not the
        # end of the thought; cutting there would leave a one-word title.
        if 12 <= at < best:
            best = at
    text = clean(defuse(raw[:best])).rstrip(".:;,")
    return text or "(no goal recorded)"


_TRAILING = {"and", "or", "of", "the", "a", "an", "to", "for", "in", "on", "with",
             "at", "by", "from", "that", "then"}


def _char_cells(ch: str) -> int:
    """Columns one code point takes: 0 for combining/format, 2 for wide/fullwidth.

    This is code-point arithmetic, not grapheme shaping. An emoji ZWJ sequence
    measures as its parts (a family glyph counts as 8, not 2), and a
    variation-selector cluster as base + 0. The consequence is a *conservative*
    width — text can be clipped a little early, never overrun — which is the
    right direction for a fixed-width pane. Accepted limit, recorded in
    docs/DESIGN.md §5.
    """
    if unicodedata.combining(ch) or unicodedata.category(ch) in ("Mn", "Me", "Cf"):
        return 0
    if unicodedata.east_asian_width(ch) in ("W", "F"):
        return 2
    return 1


def cell_width(text: str) -> int:
    """Display columns ``text`` occupies in a monospace terminal.

    The ASCII shortcut is not an optimisation only: it guarantees that the
    common case can never be off by one, whatever `unicodedata` says about the
    running Python's tables.
    """
    if not text:
        return 0
    if text.isascii():
        return len(text)
    return sum(_char_cells(ch) for ch in text)


def take_cells(text: str, max_cells: int) -> str:
    """Longest prefix of ``text`` occupying at most ``max_cells`` terminal columns."""
    if max_cells <= 0 or not text:
        return ""
    if text.isascii():
        return text[:max_cells]
    used = 0
    out = []
    for ch in text:
        w = _char_cells(ch)
        if used + w > max_cells:
            break
        used += w
        out.append(ch)
    return "".join(out)


def clip(text: str, width: int, ellipsis: str = "\u2026") -> str:
    """Clip to ``width`` display columns, at a word boundary when one is close.

    Rules, in order: fits → untouched; width ≤ the ellipsis itself → a hard cut
    with no marker; otherwise cut at the last space, unless that space is in the
    first half of the room (a boundary that throws away more than half the space
    wastes the label on a stub — cut mid-word instead). A dangling connective
    ("and", "to", "with", …) is then dropped, because "Audit shell-startup and…"
    says less than "Audit shell-startup…".

    The returned string is never wider than ``width`` cells: `take_cells` did
    the cutting and the ellipsis width was reserved first.
    """
    if width <= 0:
        return ""
    if cell_width(text) <= width:
        return text
    ell_w = cell_width(ellipsis)
    if width <= ell_w:
        return take_cells(text, width)
    room = width - ell_w
    prefix = take_cells(text, room)
    # Include one lookahead space if the next char right after `prefix` is a space
    next_is_space = len(text) > len(prefix) and text[len(prefix)] == " "
    cut = len(prefix) if next_is_space else prefix.rfind(" ")
    # A boundary that throws away more than half of the room wastes the label
    # on a short stub; cut mid-word instead.
    if cut < 0 or cell_width(prefix[:cut]) < room // 2:
        return prefix.rstrip(" ,;:-") + ellipsis
    words = prefix[:cut].split(" ")
    # "Audit shell-startup and..." says less than "Audit shell-startup...".
    while len(words) > 1 and words[-1].lower() in _TRAILING:
        words.pop()
    return " ".join(words).rstrip(" ,;:-") + ellipsis


def _label_words(goal: Any) -> list:
    """Title words for a tab: no articles, a path reduced to its last part."""
    words = []
    for word in title(goal).split(" "):
        word = word.strip("`'\"").replace("#", "")
        if "/" in word.strip("/"):
            word = word.rstrip("/").rsplit("/", 1)[-1]
        if word and word.lower() not in _ARTICLES:
            words.append(word)
    return words or [title(goal).replace("#", "")]


def tab_label(goal: Any, siblings: Iterable[Any] = (), width: int = TAB_LABEL_WIDTH,
              ascii_mode: bool = False) -> str:
    """Window name for a worker, decided once and never changed.

    ``siblings`` are the goals of the other tasks dispatched with this one.
    Sibling tasks often open with the same words ("Implement state.py",
    "Implement render.py"), and a narrow tab shows only the opening, so the
    words every sibling shares at the front are dropped: what is left starts
    with what tells the tabs apart. The sibling set is fixed at dispatch, so
    the name is stable. The status line truncates this name to fit; the window
    is not renamed when the terminal is resized.
    """
    words = _label_words(goal)
    others = [_label_words(other) for other in siblings]
    others = [ws for ws in others if ws != words]
    shared = 0
    # Never strip the last word: `shared + 1` keeps at least one, so two tasks
    # whose goals differ only later still get different (non-empty) tabs. A
    # sibling set of one has nothing to compare against, always shared == 0.
    while others and len(words) > shared + 1 and all(
            len(ws) > shared and ws[shared].lower() == words[shared].lower() for ws in others):
        shared += 1
    # Lower-case comparison above: "Implement X" and "implement X" must strip
    # the shared "implement", but the surviving words keep their original case.
    return clip(" ".join(words[shared:]), width, "..." if ascii_mode else "\u2026")


def model_short(model: Optional[str]) -> str:
    """Provider prefix and release-date suffix removed: what a reader calls it."""
    text = clean(model)
    if not text:
        return "?"
    text = text.rsplit("/", 1)[-1] or text
    return _DATE_SUFFIX.sub("", text) or text


def duration(seconds: Optional[float]) -> str:
    """Compact duration: 38s, 2m14s, 1h05m."""
    if seconds is None:
        return "?"
    try:
        total = max(0, int(seconds))
    except (TypeError, ValueError):
        return "?"
    if total < 60:
        return "%ds" % total
    if total < 3600:
        minutes, secs = divmod(total, 60)
        return "%dm%02ds" % (minutes, secs) if total < 600 else "%dm" % minutes
    hours, rest = divmod(total, 3600)
    return "%dh%02dm" % (hours, rest // 60)


_ERROR_FIELD = re.compile(r'"error":\s*"(?P<text>.+?)"\s*(?:[,}]|$)')
_RESULT_HEAD = re.compile(r"^\S+\s+ERROR\s+[\d.]+s:\s*")


def error_text(raw: Any) -> str:
    """The message inside a failed tool result, when the payload names one.

    A failed result is written as ``tool ERROR 0.4s: {"output": "", ...,
    "error": "<message>"}``. The message is what a reader needs; the JSON
    around it stays in the transcript file. Falls back to the raw text.
    """
    text = "" if raw is None else str(raw)
    named = _ERROR_FIELD.search(text)
    if named:
        return clean(named.group("text").replace('\\"', '"').replace("\\n", " "))
    text = _RESULT_HEAD.sub("", text)
    text = re.sub(r'^\{"[A-Za-z_]+":\s*"', "", text)
    text = re.sub(r'",\s*"exit_code".*$', "", text)
    return clean(text.replace("\\n", " ").replace('\\"', '"'))


def tmux_escape(text: str) -> str:
    """Make arbitrary text inert inside a tmux format string.

    ``#`` introduces formats and styles; ``,`` and ``}`` end a branch when the
    text sits inside a conditional. All three are escaped with tmux's own
    ``#,`` / ``#}`` / ``##`` forms, which is why this is done here and not with
    shell quoting: the string is later handed to `set-option`, parsed by tmux,
    and expanded at draw time. Only `watch.pane_header` needs it (a goal can
    contain any of the three); everything else that reaches a format is ours.
    """
    return text.replace("#", "##").replace(",", "#,").replace("}", "#}")
