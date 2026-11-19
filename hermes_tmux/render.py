"""Pure, deterministic renderer for the agents overview.

No imports from ``hermes_tmux.state``: this module accepts any object exposing
the documented attributes (plain ``getattr`` with defaults), so it is testable
in isolation with a local stub.

The overview answers three questions in this order and gives each a section:
what is running now, what needs a look, and what finished recently. A worker is
named by the task it was given; model, elapsed time and location are secondary
and sit on a dimmer line. History is one restrained line per worker and never
pushes live work off the pane.

Every line is at most ``width`` columns and has no trailing whitespace. Colour
is optional (``colour=True`` adds SGR sequences); the uncoloured text is the
same characters, so ``status`` in a pipe and the pane agree.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from . import labels

STALL_AFTER_S = 600  # 10 minutes; display-only, never a failure claim
#: A failure nobody looked at stops asking for attention after this long. An
#: alert that can never clear is not an alert: before this rule two failures
#: from the previous day kept the badge lit permanently.
ATTENTION_TTL_S = 2 * 3600
#: Recent-history rows shown by default. Enough to see the last wave of work.
RECENT_ROWS = 6
#: Text is easier to scan in a column than across a 188-column pane.
MAX_CONTENT_WIDTH = 118

_FAIL_STATES = ("failed", "fail", "error", "errored", "crashed", "interrupted",
                "cancelled", "canceled", "timeout", "timed_out", "killed")

_CTRL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]")


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8", "replace")
        except Exception:
            return ""
    try:
        return str(value)
    except Exception:
        return ""


def _clean(value: Any) -> str:
    """Collapse every run of whitespace/control characters to a single space."""
    text = _CTRL_RE.sub(" ", _as_text(value))
    return " ".join(text.split())


def _clip_cell(text: str, width: int) -> str:
    if width <= 0:
        return ""
    return labels.take_cells(text, width)


def _fit_line(text: str, width: int) -> str:
    """Hard guarantee: no trailing whitespace, never wider than ``width`` cells."""
    if width <= 0:
        return ""
    text = _CTRL_RE.sub(" ", text)
    return labels.take_cells(text, width).rstrip()


#: A date-only string carrying a timezone designator, e.g. ``2026-11-19+02:00``
#: or ``2026-11-19+00:00`` (the Z-suffix form after replacement). Offsets may
#: carry seconds (``+02:00:00``); fromisoformat accepts them.
_DATE_ONLY_OFFSET_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2})([+-]\d{2}(?::?\d{2}(?::\d{2}(?:\.\d+)?)?)?)$"
)


def _parse_dt(value: Any) -> Optional[datetime]:
    """Accept 'YYYY-MM-DD HH:MM:SS' and ISO-8601, with or without a 'Z'."""
    text = _as_text(value).strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    text = text.replace("T", " ", 1) if "T" in text and " " not in text else text
    # A date-only offset must not reach fromisoformat as-is: it reinterprets the
    # offset as a time of day and drops the sign ('2026-11-19+02:00' -> 02:00).
    # Give it an explicit midnight so the offset is honoured like any datetime.
    date_offset = _DATE_ONLY_OFFSET_RE.match(text)
    if date_offset:
        text = f"{date_offset.group(1)} 00:00:00{date_offset.group(2)}"
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is not None:
        try:
            dt = dt.astimezone().replace(tzinfo=None)
        except (OverflowError, OSError):
            # Local-time conversion can leave the datetime range at the
            # MINYEAR/MAXYEAR boundary (e.g. '0001-01-01Z' in a western tz).
            # The parser's contract is never to raise on hostile input.
            return None
    return dt


def _humanise(seconds: Optional[float]) -> str:
    if seconds is None:
        return "?"
    try:
        total = int(seconds)
    except (TypeError, ValueError):
        return "?"
    if total < 0:
        total = 0
    if total < 60:
        return "%ds" % total
    if total < 3600:
        return "%dm" % (total // 60)
    hours = total // 3600
    minutes = (total % 3600) // 60
    return "%dh%02dm" % (hours, minutes)


def _activity_at(worker: Any) -> Optional[datetime]:
    """Best available timestamp of the worker's most recent known activity."""
    candidates: List[datetime] = []
    for attr in ("last_event", "last_log_at", "last_activity", "last_seen", "updated_at"):
        dt = _parse_dt(getattr(worker, attr, None))
        if dt is not None:
            candidates.append(dt)
    started = _parse_dt(getattr(worker, "started", None))
    if started is not None:
        duration = getattr(worker, "duration_s", None)
        if isinstance(duration, (int, float)):
            try:
                candidates.append(started + timedelta(seconds=float(duration)))
            except (OverflowError, ValueError):
                candidates.append(started)
        else:
            candidates.append(started)
    if not candidates:
        return None
    return max(candidates)


def _state_label(worker: Any, now: datetime) -> str:
    raw = _clean(getattr(worker, "state", "")).lower()
    if raw in ("running", "run", "active", "started"):
        activity = _activity_at(worker)
        if activity is not None:
            # A transcript only carries a time of day, joined to the manifest's
            # date. Across midnight that lands in the future, which would make a
            # healthy worker look stalled by hours; clamp instead of guessing.
            if activity > now:
                activity = now
            if (now - activity).total_seconds() > STALL_AFTER_S:
                return "stall"
        return "run"
    if raw in ("done", "completed", "complete", "success", "succeeded", "finished"):
        return "done"
    if raw in _FAIL_STATES:
        return "fail"
    if raw in ("stall", "stalled", "stale"):
        return "stall"
    if not raw:
        return "other"[:5]
    return ((_clean(getattr(worker, "state", "")) or "other")[:5]) or "-"


def _is_reviewer(worker: Any) -> bool:
    return _clean(getattr(worker, "role", "")).lower().startswith("rev")


def _review_status(worker: Any) -> str:
    review = getattr(worker, "review", None)
    if not isinstance(review, dict):
        return "unreviewed"
    status = _clean(review.get("status", "")).lower()
    if status in ("recorded", "failed", "unreviewed"):
        return status
    return "unreviewed"


def _review_note(worker: Any) -> str:
    """'review: recorded (who)' when the registry holds an entry, else ''."""
    if _review_status(worker) != "recorded":
        return ""
    review = getattr(worker, "review", None)
    reviewer = _clean(review.get("reviewer_model")) if isinstance(review, dict) else ""
    # Name the reviewer when one was recorded: an unattributed badge cannot be
    # told apart from a self-assessment. A manual record has no model.
    return "review: recorded (%s)" % (reviewer.rsplit("/", 1)[-1] if reviewer else "manual")


def _key_label(worker: Any) -> str:
    """Short handle for commands: last 8 of the delegation id, '#', task index."""
    delegation_id = _clean(getattr(worker, "delegation_id", "")) or "-"
    index = getattr(worker, "task_index", "")
    return "%s#%s" % (delegation_id[-8:], _as_text("" if index is None else index).strip() or "-")


def _elapsed(worker: Any, now: datetime) -> Optional[float]:
    started = _parse_dt(getattr(worker, "started", None))
    if started is None:
        return None
    return max(0.0, (now - started).total_seconds())


def _ran_for(worker: Any) -> Optional[float]:
    duration = getattr(worker, "duration_s", None)
    if isinstance(duration, (int, float)):
        return float(duration)
    started = _parse_dt(getattr(worker, "started", None))
    ended = _parse_dt(getattr(worker, "last_event", None))
    if started is not None and ended is not None and ended >= started:
        return (ended - started).total_seconds()
    return None


def _ago(worker: Any, now: datetime) -> str:
    activity = _activity_at(worker)
    if activity is None:
        return ""
    return _humanise((now - activity).total_seconds()) + " ago"


def _error_pending(worker: Any) -> bool:
    pending = getattr(worker, "error_pending", None)
    if isinstance(pending, bool):
        return pending
    return bool(_clean(getattr(worker, "last_error", None)))


def classify(workers: Iterable[Any], now: Optional[datetime] = None,
             seen: Iterable[str] = (), since: Any = None) -> Dict[str, List[Any]]:
    """Split workers into running / attention / recent.

    ``attention`` is a failure nobody has opened yet and that is still fresh.
    ``seen`` names workers whose tab the reader has looked at; it comes from the
    workspace layout and is empty when there is none (``status`` outside tmux).
    ``since`` is when the workspace came up (epoch seconds): a run that ended
    before then is history, the same rule that decides it gets no tab.
    Stalled workers stay under ``running``: they have not finished, and the
    silence is shown on their row.
    """
    now = now or datetime.now()
    seen = set(seen)
    try:
        floor = datetime.fromtimestamp(float(since)) if since else None
    except (TypeError, ValueError, OverflowError, OSError):
        floor = None
    groups: Dict[str, List[Any]] = {"running": [], "attention": [], "recent": []}
    for worker in workers:
        if worker is None:
            continue
        kind = labels.state_kind(_state_label(worker, now))
        if kind in ("run", "stall"):
            groups["running"].append(worker)
            continue
        if kind == "fail" and str(getattr(worker, "key", "")) not in seen:
            activity = _activity_at(worker)
            fresh = activity is None or (now - activity).total_seconds() <= ATTENTION_TTL_S
            if fresh and not (floor is not None and activity is not None and activity < floor):
                groups["attention"].append(worker)
                continue
        groups["recent"].append(worker)

    def _started(worker: Any) -> datetime:
        return _parse_dt(getattr(worker, "started", None)) or datetime.min

    def _latest(worker: Any) -> datetime:
        return _activity_at(worker) or datetime.min

    groups["running"].sort(key=_started)                 # oldest first: stable rows
    groups["attention"].sort(key=_latest, reverse=True)
    groups["recent"].sort(key=_latest, reverse=True)
    return groups


def summary(groups: Dict[str, List[Any]], now: Optional[datetime] = None) -> Dict[str, int]:
    """Counts behind the header and the tab badge (one definition for both)."""
    now = now or datetime.now()
    stalled = sum(1 for w in groups["running"] if _state_label(w, now) == "stall")
    return {"running": len(groups["running"]) - stalled, "stalled": stalled,
            "attention": len(groups["attention"]) + stalled}


# --------------------------------------------------------------------------- #
# drawing
# --------------------------------------------------------------------------- #

BOLD, DIM = "1", "2"
# Colour policy: primary text is the terminal's own foreground (bold for
# emphasis), so it is legible on any theme, light or dark. Secondary text is a
# mid grey from the 256-colour cube that holds up on both. Accents are fixed
# 256-colour indexes, the same ones the status line uses. Nothing here needs
# truecolor.


class _Pen:
    """Builds lines from styled segments while tracking the visible width."""

    def __init__(self, width: int, colour: bool) -> None:
        self.width = max(1, width)
        self.colour = colour
        self.lines: List[str] = []

    def style(self, text: str, *codes: str) -> str:
        if not self.colour or not text or not codes:
            return text
        return "\033[%sm%s\033[0m" % (";".join(codes), text)

    def line(self, *segments: Tuple[str, Tuple[str, ...]]) -> None:
        """Append one line; segments are (text, codes). Clipped to ``width`` cells."""
        room = self.width
        out: List[Tuple[str, Tuple[str, ...]]] = []
        plain = ""
        for text, codes in segments:
            text = _CTRL_RE.sub(" ", text)
            if room <= 0:
                break
            text = labels.take_cells(text, room)
            room -= labels.cell_width(text)
            plain += text
            out.append((text, codes))
        # Trailing whitespace is dropped from the last segments so a pane never
        # repaints blank cells and a pipe never carries padding.
        keep = len(plain.rstrip())
        rendered, used = "", 0
        for text, codes in out:
            if used >= keep:
                break
            text = text[:keep - used]
            used += len(text)
            rendered += self.style(text, *codes)
        self.lines.append(rendered)

    def blank(self) -> None:
        self.lines.append("")

    def split(self, left: Sequence[Tuple[str, Tuple[str, ...]]],
              right: Sequence[Tuple[str, Tuple[str, ...]]]) -> None:
        """Left segments, then right segments flush against the content edge."""
        right_len = sum(labels.cell_width(t) for t, _ in right)
        left_room = max(0, self.width - right_len - (2 if right_len else 0))
        fitted: List[Tuple[str, Tuple[str, ...]]] = []
        used = 0
        for text, codes in left:
            text = labels.take_cells(text, max(0, left_room - used))
            used += labels.cell_width(text)
            fitted.append((text, codes))
        pad = self.width - used - right_len
        self.line(*fitted, (" " * max(0, pad), ()), *right)


def _fg(index: int) -> str:
    return "38;5;%d" % index


def _fit(text: str, width: int, ascii_mode: bool) -> str:
    return labels.clip(text, width, "..." if ascii_mode else "\u2026")


def _tab_segment(worker: Any, tabs: Optional[Dict[str, Any]],
                 spaced: bool = False) -> List[Tuple[str, Tuple[str, ...]]]:
    index = (tabs or {}).get(str(getattr(worker, "key", "")))
    if index in (None, ""):
        return []
    return [(("  " if spaced else "") + "tab %s" % index, (_fg(39),))]


def _meta(parts: Iterable[str], ascii_mode: bool) -> str:
    sep = " | " if ascii_mode else " \u00b7 "
    return sep.join(part for part in parts if part)


_MD = re.compile(r"\*{1,3}|`|^#+\s*|^\s*[-\u2022]\s+")


def _excerpt(text: Any) -> str:
    """A worker's own words as one plain line (markdown markers removed).

    A report usually opens with its conclusion and continues with headings and
    tables; the excerpt stops where those begin.
    """
    raw = _as_text(text)
    for mark in ("\n#", " ## ", " | ", "```"):
        at = raw.find(mark)
        if at > 12:
            raw = raw[:at]
    return _clean(_MD.sub("", raw)).rstrip(" :-")


def _title_line(pen: _Pen, worker: Any, kind: str, tabs: Optional[Dict[str, Any]],
                ascii_mode: bool, right: Sequence[Tuple[str, Tuple[str, ...]]] = (),
                title_codes: Tuple[str, ...] = (BOLD,),
                glyph_colour: Optional[int] = None) -> None:
    """Glyph, task title, and whatever is pinned to the right edge."""
    right = list(right) + _tab_segment(worker, tabs, bool(right))
    room = pen.width - 4 - sum(labels.cell_width(t) for t, _ in right) - (2 if right else 0)
    colour = labels.colour(kind) if glyph_colour is None else glyph_colour
    pen.split([("  ", ()), (labels.glyph(kind, ascii_mode) + " ", (_fg(colour),)),
               (_fit(labels.title(getattr(worker, "goal", "")), room, ascii_mode), title_codes)],
              right)


def _running_rows(pen: _Pen, worker: Any, now: datetime,
                  tabs: Optional[Dict[str, Any]], ascii_mode: bool) -> None:
    kind = labels.state_kind(_state_label(worker, now))
    _title_line(pen, worker, kind, tabs, ascii_mode)
    sep = " | " if ascii_mode else " \u00b7 "
    wide, narrow = pen.width >= 90, pen.width < 60
    tools = getattr(worker, "tool_count", 0) or 0
    # Secondary facts leave in order of how little they are missed: the id,
    # the tool count, then the model. Elapsed time and the current call stay.
    parts = [] if narrow else [labels.model_short(getattr(worker, "model", None))]
    parts.append(labels.duration(_elapsed(worker, now)))
    if wide:
        parts.append("%d tool%s" % (tools, "" if tools == 1 else "s") if tools else "starting")
    left: List[Tuple[str, Tuple[str, ...]]] = [("    " + _meta(parts, ascii_mode), (_fg(244),))]
    key = _key_label(worker) if wide else ""
    room = pen.width - labels.cell_width(left[0][0]) - labels.cell_width(sep) - (labels.cell_width(key) + 2 if key else 0)
    call = _clean(getattr(worker, "last_call", None)) or _clean(getattr(worker, "last_tool", None))
    if kind == "stall":
        activity = _activity_at(worker)
        quiet = _humanise((now - activity).total_seconds()) if activity else "?"
        left.append((sep + "no output for %s" % quiet, (_fg(214),)))
    elif _error_pending(worker):
        left.append((sep + _fit("last call failed: %s" % (call or "?"), room, ascii_mode),
                     (_fg(203),)))
    elif call:
        left.append((sep + _fit(call, room, ascii_mode), ()))
    note = _review_note(worker)
    if note and wide:
        left.append((sep + note, (_fg(244),)))
    pen.split(left, [(key, (_fg(243),))] if key else [])


def _attention_rows(pen: _Pen, worker: Any, now: datetime,
                    tabs: Optional[Dict[str, Any]], ascii_mode: bool) -> None:
    _title_line(pen, worker, "fail", tabs, ascii_mode)
    reason = _clean(getattr(worker, "exit_reason", None)) or _clean(getattr(worker, "state", ""))
    # "run ended", not "task failed": this line states how the agent run
    # stopped, which Hermes recorded. Whether the work itself is good is not
    # something this tool knows; the worker's last report is quoted below so
    # the reader can weigh the two.
    parts = ["run ended: %s after %s" % (reason or "error", labels.duration(_ran_for(worker))),
             _ago(worker, now)]
    if pen.width >= 90:
        parts += [labels.model_short(getattr(worker, "model", None)), _key_label(worker),
                  _review_note(worker)]
    pen.line(("    " + _fit(_meta(parts, ascii_mode), pen.width - 4, ascii_mode), (_fg(214),)))
    error = labels.error_text(getattr(worker, "last_error", None))
    if error:
        pen.line(("    " + _fit(error, pen.width - 4, ascii_mode), (_fg(203),)))
    said = _excerpt(getattr(worker, "last_message", None))
    if said:
        lead = "    last report: "
        pen.line((lead, (_fg(244),)),
                 (_fit(said, pen.width - len(lead), ascii_mode), ()))


def _recent_rows(pen: _Pen, worker: Any, now: datetime, tabs: Optional[Dict[str, Any]],
                 ascii_mode: bool, detail: bool) -> None:
    kind = labels.state_kind(_state_label(worker, now))
    reason = _clean(getattr(worker, "exit_reason", None)).lower()
    cut_short = kind == "done" and reason not in ("", "completed", "done", "success")
    _title_line(pen, worker, kind, tabs, ascii_mode,
                right=[(_ago(worker, now), (_fg(244),))], title_codes=(),
                glyph_colour=214 if cut_short else None)
    if not detail:
        return
    sep = " | " if ascii_mode else " \u00b7 "
    parts = [labels.duration(_ran_for(worker))]
    if pen.width >= 72:
        parts.append(labels.model_short(getattr(worker, "model", None)))
    left: List[Tuple[str, Tuple[str, ...]]] = [("    " + _meta(parts, ascii_mode), (_fg(244),))]
    if (cut_short or kind == "fail") and reason:
        left.append((sep + reason, (_fg(214),)))
    note = _review_note(worker)
    if note:
        left.append((sep + note, (_fg(244),)))
    # What came out of it, in the worker's words: the line that makes a row a
    # task instead of a table entry.
    said = _excerpt(getattr(worker, "summary", None) or getattr(worker, "last_message", None))
    key = _key_label(worker) if pen.width >= 90 else ""
    room = pen.width - sum(labels.cell_width(t) for t, _ in left) - labels.cell_width(sep) - (labels.cell_width(key) + 2 if key else 0)
    if said and room >= 16:
        left.append((sep, (_fg(243),)))
        left.append((_fit(said, room, ascii_mode), (_fg(246),)))
    pen.split(left, [(key, (_fg(243),))] if key else [])


def _header(pen: _Pen, counts: Dict[str, int], session_label: str, ascii_mode: bool) -> None:
    sep = " | " if ascii_mode else " \u00b7 "
    left: List[Tuple[str, Tuple[str, ...]]] = [("  HERMES AGENTS", (BOLD,))]
    label = _clean(session_label)
    if label and pen.width >= 70:
        left.append((sep + label, (_fg(244),)))
    right: List[Tuple[str, Tuple[str, ...]]] = []
    if counts["running"]:
        right.append(("%d running" % counts["running"], (BOLD, _fg(114))))
    if counts["attention"]:
        if right:
            right.append((sep, (_fg(244),)))
        words = "need%s attention" % ("s" if counts["attention"] == 1 else "") \
            if pen.width >= 70 else "attention"
        right.append(("%d %s" % (counts["attention"], words), (BOLD, _fg(203))))
    if not right:
        right.append(("idle", (_fg(244),)))
    pen.split(left, right)


def _centre(lines: List[str], width: int, content: int) -> List[str]:
    """Sit the block in the middle of a pane wider than the content column."""
    margin = max(0, (width - content) // 2)
    if not margin:
        return lines
    pad = " " * margin
    return [(pad + line) if line else line for line in lines]


def _section(pen: _Pen, name: str) -> None:
    pen.blank()
    pen.line(("  " + name, (_fg(244),)))


# --------------------------------------------------------------------------- #
# public API
# --------------------------------------------------------------------------- #


def render(
    workers: Iterable[Any],
    *,
    width: int = 110,
    height: Optional[int] = None,
    now: Optional[datetime] = None,
    session_label: str = "",
    live_root: Optional[str] = None,
    note: str = "",
    colour: bool = False,
    tabs: Optional[Dict[str, Any]] = None,
    seen: Iterable[str] = (),
    since: Any = None,
    ascii_mode: bool = False,
    recent_rows: int = RECENT_ROWS,
) -> str:
    """Render the agents overview.

    ``tabs`` maps a worker key to the tmux window index showing its transcript.
    ``note`` carries an explicit failure line (a reader exception); it is
    printed instead of letting an empty list imply "no workers". ``height``
    bounds the block: history only ever takes rows the live sections left
    free. A pane too short for the live sections alone shows their first rows.
    Widths are counted in characters; double-width text (CJK) in a goal can
    overrun a line by a few columns.
    """
    width = int(width) if isinstance(width, int) else 110
    width = max(1, width)
    if now is None:
        now = datetime.now()
    worker_list: List[Any] = [w for w in workers if w is not None]
    if not worker_list or _clean(note):
        return render_empty(width=width, live_root=live_root, note=note,
                            colour=colour, session_label=session_label,
                            ascii_mode=ascii_mode)

    pen = _Pen(min(width, MAX_CONTENT_WIDTH), colour)
    groups = classify(worker_list, now, seen, since)
    _header(pen, summary(groups, now), session_label, ascii_mode)

    if groups["running"]:
        _section(pen, "RUNNING")
        for index, worker in enumerate(groups["running"]):
            if index:
                pen.blank()
            _running_rows(pen, worker, now, tabs, ascii_mode)
    else:
        pen.blank()
        pen.line(("  Nothing running.", ()))

    if groups["attention"]:
        _section(pen, "NEEDS ATTENTION")
        for index, worker in enumerate(groups["attention"]):
            if index:
                pen.blank()
            _attention_rows(pen, worker, now, tabs, ascii_mode)

    recent = groups["recent"]
    if recent:
        limit = max(0, int(recent_rows))
        detail = True
        if height is not None:
            # History takes what is left: the section header (2 rows) and the
            # "older" line (1) come out first, live sections are never trimmed,
            # and a short pane drops the second line of each row before it
            # drops rows.
            left = int(height) - len(pen.lines) - 3
            detail = left >= 2 * min(limit, len(recent), 3)
            limit = max(0, min(limit, left // (2 if detail else 1)))
            spaced = detail and left >= 3 * min(limit, len(recent))
        else:
            spaced = False
        if limit:
            _section(pen, "RECENT")
            for index, worker in enumerate(recent[:limit]):
                if spaced and index:
                    pen.blank()
                _recent_rows(pen, worker, now, tabs, ascii_mode, detail)
            hidden = len(recent) - limit
            if hidden > 0:
                pen.line(("    + %d older" % hidden, (_fg(243),)),
                         ("  (hermes-tmux status --all)" if pen.width >= 60 else "", (_fg(243),)))

    lines = pen.lines
    if height is not None and len(lines) > int(height):
        lines = lines[:max(0, int(height))]
    return "\n".join(_centre(lines, width, pen.width))


def render_empty(*, width: int = 110, live_root: Optional[str] = None,
                 note: str = "", colour: bool = False, session_label: str = "",
                 ascii_mode: bool = False) -> str:
    """Render the block shown when no delegations are visible."""
    width = int(width) if isinstance(width, int) else 110
    pen = _Pen(min(max(1, width), MAX_CONTENT_WIDTH), colour)
    _header(pen, {"running": 0, "stalled": 0, "attention": 0}, session_label, ascii_mode)
    pen.blank()
    headline = _clean(note)
    root = _clean(live_root) or "(unknown)"
    if headline:
        pen.line(("  " + headline, (BOLD, _fg(203))))
        pen.line(("  this is a reader failure, not an empty workspace", ()))
    else:
        pen.line(("  No delegations yet.", ()))
        pen.line(("  When Hermes delegates work, each worker gets its own tab here "
                  "and a row on this page.", (_fg(244),)))
    pen.blank()
    pen.line(("  live root: " + root, (_fg(243),)))
    return "\n".join(_centre(pen.lines, max(1, width), pen.width))
