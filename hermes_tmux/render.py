"""Pure, deterministic, plain-ASCII renderer for Hermes subagent worker state.

No imports from ``hermes_tmux.state``: this module accepts any object exposing the
documented attributes (plain ``getattr`` with defaults), so it is testable in
isolation with a local stub.

Output is a fixed-width block that is safe to paint into a tmux pane: ASCII only,
no ANSI escapes, no unicode box drawing, no trailing whitespace, and every line is
at most ``width`` characters.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

#: Kept short on purpose: this is painted on one line and clipped at the pane
#: width, so a sentence-length legend would silently lose its ending.
LEGEND = (
    "legend: STATE from Hermes state; review only when an entry was recorded."
)

STALL_AFTER_S = 600  # 10 minutes; display-only, never a failure claim

# (name, desired width, minimum width). 'goal' takes whatever is left.
_COLUMNS = (
    ("key", 11, 8),
    ("role", 5, 4),
    ("state", 5, 5),
    ("model", 16, 8),
    ("tool", 11, 6),
    ("age", 5, 3),
)

# Shrink order: the goal text is the reason the pane exists, so trim the
# peripheral columns before giving up entirely on a column.
_SHRINK_ORDER = ("model", "tool", "key", "age", "role", "state")

_DROP_ORDER = ("tool", "model", "role", "state")

_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")


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
    if len(text) > width:
        return text[:width]
    return text


def _fit_line(text: str, width: int) -> str:
    """Hard guarantee: no trailing whitespace, never longer than ``width``."""
    if width <= 0:
        return ""
    text = _CTRL_RE.sub(" ", text)
    if len(text) > width:
        text = text[:width]
    return text.rstrip()


def _parse_dt(value: Any) -> Optional[datetime]:
    """Accept 'YYYY-MM-DD HH:MM:SS' and ISO-8601, with or without a 'Z'."""
    text = _as_text(value).strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    text = text.replace("T", " ", 1) if "T" in text and " " not in text else text
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
        dt = dt.astimezone().replace(tzinfo=None)
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
    if raw in ("failed", "fail", "error", "errored", "crashed"):
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


def _model_label(worker: Any) -> str:
    model = _clean(getattr(worker, "model", None))
    if not model:
        return "-"
    if "/" in model:
        model = model.rsplit("/", 1)[-1].strip() or model
    return model


def _tool_label(worker: Any, width: int) -> str:
    tool = _clean(getattr(worker, "last_tool", None))
    # Prefer the explicit "latest call failed" flag. Fall back to the presence
    # of any recorded error for objects that predate the flag.
    pending = getattr(worker, "error_pending", None)
    if isinstance(pending, bool):
        has_error = pending
    else:
        error = getattr(worker, "last_error", None)
        has_error = error is not None and _clean(error) != ""
    base = tool or "-"
    if not has_error:
        return _clip_cell(base, width)
    if width <= 1:
        return "!"
    # '!' marks a recorded error on the last tool call. One column is reserved
    # for it, otherwise _fit_line would truncate the marker away at the pane edge.
    base = _clip_cell(base, width - 1)
    return base + "!"


def _age_label(worker: Any, now: datetime) -> str:
    started = _parse_dt(getattr(worker, "started", None))
    if started is None:
        return "?"
    return _humanise((now - started).total_seconds())


def _key_label(worker: Any, width: int = 11) -> str:
    delegation_id = _clean(getattr(worker, "delegation_id", "")) or "-"
    index = getattr(worker, "task_index", "")
    if index is None:
        index = ""
    tail = "#" + (_as_text(index).strip() or "-")
    if width <= 0:
        return ""
    full = delegation_id[-8:] + tail
    if len(full) <= width:
        return full
    if len(tail) >= width:
        return tail[:width]
    # Narrow column: keep the task index, trim the delegation suffix.
    return delegation_id[-(width - len(tail)):] + tail


# --------------------------------------------------------------------------- #
# layout
# --------------------------------------------------------------------------- #


def _separators(n_cols: int) -> int:
    """n-1 single gaps between columns plus a 2-space gap before GOAL."""
    if n_cols <= 0:
        return 0
    return (n_cols - 1) + 2


def _total_width(cols: Sequence[Tuple[str, int, int]]) -> int:
    return sum(width for _, width, _ in cols) + _separators(len(cols))


def _layout(width: int) -> Tuple[List[Tuple[str, int, int]], int]:
    """Return (columns, goal_width) for the given total width.

    Shrink peripheral columns toward their minimums first, and only then drop
    columns outright, so a narrow pane keeps the widest useful GOAL.
    """
    cols: List[Tuple[str, int, int]] = list(_COLUMNS)
    goal_min = max(3, min(10, width // 6))

    def shrink_once() -> bool:
        for name in _SHRINK_ORDER:
            for i, (col_name, desired, minimum) in enumerate(cols):
                if col_name == name and desired > minimum:
                    cols[i] = (col_name, desired - 1, minimum)
                    return True
        return False

    while _total_width(cols) + goal_min > width:
        if not shrink_once():
            break

    while _total_width(cols) + goal_min > width and len(cols) > 2:
        dropped = False
        for name in _DROP_ORDER:
            if len(cols) <= 2:
                break
            if any(col[0] == name for col in cols):
                cols = [col for col in cols if col[0] != name]
                dropped = True
                break
        if not dropped:
            break
        while _total_width(cols) + goal_min > width:
            if not shrink_once():
                break

    goal_width = max(0, width - _total_width(cols))
    return cols, goal_width


def _rule(width: int) -> str:
    return "-" * max(0, width)


def _header_line(cols: Sequence[Tuple[str, int, int]], goal_width: int) -> str:
    """Header aligned to the same column widths the rows use."""
    chunks = [name.upper().ljust(width) for name, width, _minimum in cols]
    line = " ".join(chunks)
    if goal_width > 0:
        line += "  " + "GOAL".ljust(goal_width)
    return line.rstrip()


def _counts(workers: Sequence[Any], now: datetime) -> Tuple[Dict[str, int], int]:
    counts: Dict[str, int] = {}
    running = 0
    for worker in workers:
        label = _state_label(worker, now)
        counts[label] = counts.get(label, 0) + 1
        if label in ("run", "stall"):
            running += 1
    return counts, running


def _summary_line(counts: Dict[str, int], total: int) -> str:
    order = ["run", "stall", "done", "fail"]
    parts: List[str] = []
    for label in order:
        if counts.get(label):
            parts.append("%s %d" % (label, counts[label]))
    for label in sorted(counts):
        if label not in order and counts[label]:
            parts.append("%s %d" % (label, counts[label]))
    if not parts:
        return "total 0"
    return "total %d: %s" % (total, ", ".join(parts))


def _title_line(
    running: int,
    total: int,
    width: int,
    now: datetime,
    session_label: str,
    newest: Optional[datetime],
) -> str:
    left = "hermes subagents"
    label = _clean(session_label)
    if label:
        left += " - " + label
    left += "   %d running / %d total" % (running, total)

    right = ""
    if newest is not None:
        right = _humanise((now - newest).total_seconds())

    if right:
        pad = width - len(left) - len(right)
        if pad >= 1:
            line = left + (" " * pad) + right
        else:
            keep = max(0, width - len(right) - 1)
            line = left[:keep] + " " + right
    else:
        line = left
    return line


def _worker_row(worker: Any, cols: Sequence[Tuple[str, int, int]], goal_width: int, now: datetime) -> str:
    reviewer = _is_reviewer(worker)
    cells: Dict[str, str] = {
        "key": _clip_cell(_key_label(worker, _col_width(cols, "key")), _col_width(cols, "key")),
        "role": _clip_cell("*revw" if reviewer else "exec", _col_width(cols, "role")),
        "state": _clip_cell(_state_label(worker, now), _col_width(cols, "state")),
        "model": _clip_cell(_model_label(worker), _col_width(cols, "model")),
        "tool": _tool_label(worker, _col_width(cols, "tool")),
        "age": _clip_cell(_age_label(worker, now), _col_width(cols, "age")),
    }

    chunks: List[str] = []
    for name, width, _minimum in cols:
        cell = cells.get(name, "")
        if len(cell) < width:
            cell = cell.ljust(width)
        chunks.append(cell)
    row = " ".join(chunks)

    goal = _clean(getattr(worker, "goal", "")) or "-"
    if _review_status(worker) == "recorded":
        # Name the reviewer when the registry recorded one: an unattributed
        # "reviewed" badge cannot be distinguished from a self-assessment.
        review = getattr(worker, "review", None)
        reviewer = _clean(review.get("reviewer_model")) if isinstance(review, dict) else ""
        goal += " | review: recorded"
        if reviewer:
            goal += " (%s)" % reviewer.rsplit("/", 1)[-1]
        else:
            # A manual record has no reviewer model; say so rather than implying
            # an automated reviewer ran.
            goal += " (manual)"
    if goal_width > 0:
        row += "  " + _clip_cell(goal, goal_width).ljust(goal_width)
        return row.rstrip()

    # No room for a GOAL column: mark reviewers distinctly instead.
    if reviewer and (len(row) + len(" [review]")) <= _row_capacity(cols):
        row += " [review]"
    return row.rstrip()


def _col_width(cols: Sequence[Tuple[str, int, int]], name: str) -> int:
    for column_name, width, _minimum in cols:
        if column_name == name:
            return width
    return 0


def _row_capacity(cols: Sequence[Tuple[str, int, int]]) -> int:
    return _total_width(cols)


# --------------------------------------------------------------------------- #
# public API
# --------------------------------------------------------------------------- #


def render(
    workers: Iterable[Any],
    *,
    width: int = 110,
    max_rows: int = 12,
    now: Optional[datetime] = None,
    session_label: str = "",
    live_root: Optional[str] = None,
    note: str = "",
) -> str:
    """Render worker records as a fixed-width ASCII block.

    ``note`` carries an explicit failure line (for example a reader exception).
    It is printed verbatim instead of letting an empty list imply "no workers".
    """
    width = int(width) if isinstance(width, int) else 110
    if width < 1:
        width = 1
    max_rows = int(max_rows) if isinstance(max_rows, int) else 12
    if max_rows < 0:
        max_rows = 0
    if now is None:
        now = datetime.now()

    worker_list: List[Any] = [w for w in workers if w is not None]

    if not worker_list:
        return render_empty(width=width, live_root=live_root, note=note)

    counts, running = _counts(worker_list, now)
    total = len(worker_list)

    timestamps = [ts for ts in (_activity_at(w) for w in worker_list) if ts is not None]
    newest = max(timestamps) if timestamps else None

    cols, goal_width = _layout(width)

    active = [w for w in worker_list if _state_label(w, now) in ("run", "stall")]
    settled = [w for w in worker_list if _state_label(w, now) not in ("run", "stall")]
    ordered = active + settled

    lines: List[str] = []
    lines.append(_title_line(running, total, width, now, session_label, newest))
    lines.append(_rule(width))
    lines.append(_header_line(cols, goal_width))

    for worker in ordered[:max_rows]:
        lines.append(_worker_row(worker, cols, goal_width, now))

    lines.append(_rule(width))
    lines.append(_summary_line(counts, total))

    recorded = sum(1 for w in worker_list if _review_status(w) == "recorded")
    lines.append("review: %d recorded, %d unreviewed" % (recorded, total - recorded))
    lines.append(LEGEND)

    return "\n".join(_fit_line(line, width) for line in lines)


def render_empty(*, width: int = 110, live_root: Optional[str] = None,
                 note: str = "") -> str:
    """Render the block shown when no delegations are visible."""
    width = int(width) if isinstance(width, int) else 110
    if width < 1:
        width = 1

    root = _clean(live_root) or "(unknown)"
    headline = _clean(note)
    lines: List[str] = [
        "hermes subagents",
        _rule(width),
        "",
        ("  " + headline) if headline else "  no delegations found",
        "  live root: " + root,
        "  this is a reader failure, not an empty workspace" if headline
        else "  waiting for subagent activity (this pane updates on file events)",
        "",
        LEGEND,
    ]
    return "\n".join(_fit_line(line, width) for line in lines)
