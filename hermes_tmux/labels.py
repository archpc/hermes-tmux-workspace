"""Human-readable names and state vocabulary shared by every surface.

One place decides how a worker is named and which glyph and colour its state
gets, so the tab, the pane header, the overview and the transcript never
disagree. Nothing here classifies work: a title is the first clause of the goal
the parent wrote, verbatim.
"""

from __future__ import annotations

import os
import re
import unicodedata
from typing import Any, Dict, Iterable, Optional, Tuple

#: Longest window name. The status line shows less of it when space is short.
TAB_LABEL_WIDTH = 28

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
    """True when glyphs must stay ASCII: forced, or the locale is not UTF-8."""
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
    """The first clause of a goal: what the parent asked for, in its words."""
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
    if unicodedata.combining(ch) or unicodedata.category(ch) in ("Mn", "Me", "Cf"):
        return 0
    if unicodedata.east_asian_width(ch) in ("W", "F"):
        return 2
    return 1


def cell_width(text: str) -> int:
    """Display columns ``text`` occupies in a monospace terminal."""
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
    """Clip to ``width`` display columns, at a word boundary when one is close."""
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
    while others and len(words) > shared + 1 and all(
            len(ws) > shared and ws[shared].lower() == words[shared].lower() for ws in others):
        shared += 1
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
    text sits inside a conditional.
    """
    return text.replace("#", "##").replace(",", "#,").replace("}", "#}")
