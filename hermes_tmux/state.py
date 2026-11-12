"""Read-only state reader for hermes-tmux.

Turns Hermes delegation artifacts (live transcripts, manifests, the
``async_delegations`` table, and the review registry) into normalized
``Worker`` records.

Ground rules for this module:

* Standard library only, Python 3.9 compatible.
* Read-only: nothing here writes to Hermes state, logs, or config.
* Total functions: every public entry point returns a best-effort value on
  any I/O, parse, or sqlite error. None of them raise.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# --------------------------------------------------------------------------
# constants

#: Roles emitted by the Hermes live-transcript writer.
KNOWN_ROLES = (
    "user",
    "start",
    "think",
    "assistant",
    "tool",
    "result",
    "final",
)

#: ``state`` values in ``async_delegations`` that are *not* terminal.
#: ``async_delegations.state`` values that mean "still live". Hermes'
#: ``_LIVE_STATES`` is ``{"running", "stalling", "finalizing"}``; ``stalling`` and
#: ``finalizing`` are easy to mistake for verdicts, and treating either as
#: terminal would freeze a worker's row at a state it has not reached yet.
NON_TERMINAL_DB_STATES = frozenset(
    {
        "running",
        "pending",
        "queued",
        "dispatched",
        "active",
        "in_progress",
        "starting",
        "stalling",
        "finalizing",
    }
)

_EVENT_RE = re.compile(
    r"^(?P<ts>\d{2}:\d{2}:\d{2})\s+(?P<role>[A-Za-z_][A-Za-z0-9_]*)\s*\|(?P<text>.*)$"
)
_DURATION_RE = re.compile(r"duration=(?P<value>-?\d+(?:\.\d+)?)s")
_SUMMARY_MARK = "summary: "
_TOOL_ARROW = "-> "

_DESIRED_DB_COLUMNS = (
    "delegation_id",
    "parent_session_id",
    "state",
    "dispatched_at",
    "completed_at",
    "event_json",
    "result_json",
    #: dispatch payload; carries the authoritative task index -> transcript map
    "task_json",
)


# --------------------------------------------------------------------------
# paths


def hermes_home(home: Optional[Path] = None) -> Path:
    """Return the Hermes home directory.

    ``home`` wins when given (callers pass an explicit root in tests); otherwise
    ``$HERMES_HOME``, else ``~/.hermes``.
    """
    try:
        if home is not None:
            return Path(home).expanduser()
        env = os.environ.get("HERMES_HOME")
        if env:
            return Path(env).expanduser()
        return Path("~/.hermes").expanduser()
    except Exception:  # pragma: no cover - defensive
        return Path("~/.hermes")


def live_root(home: Optional[Path] = None) -> Path:
    """Return the delegation live root, ``<home>/cache/delegation/live``."""
    base = Path(home).expanduser() if home is not None else hermes_home()
    return base / "cache" / "delegation" / "live"


def _reviews_path(home: Optional[Path]) -> Path:
    base = Path(home).expanduser() if home is not None else hermes_home()
    return base / "cache" / "hermes-tmux" / "reviews.json"


def _db_path(home: Optional[Path]) -> Path:
    base = Path(home).expanduser() if home is not None else hermes_home()
    return base / "state.db"


# --------------------------------------------------------------------------
# log parsing


def _empty_log() -> Dict[str, Any]:
    return {
        "phase": "none",
        "tool_count": 0,
        "last_tool": None,
        "last_error": None,
        "summary": None,
        "first_ts": None,
        "last_ts": None,
        "has_final": False,
        "error_pending": False,
        "duration_s": None,
        "events": [],
    }


def _tool_name(text: str) -> Optional[str]:
    """Extract the tool name from a ``-> name(args)`` transcript line."""
    body = text.strip()
    if body.startswith(_TOOL_ARROW):
        body = body[len(_TOOL_ARROW) :].strip()
    if not body:
        return None
    cut = body.find("(")
    if cut == -1:
        return body.split()[0] if body.split() else None
    name = body[:cut].strip()
    return name or None


def _parse_event_line(line: str) -> Optional[Dict[str, str]]:
    """Parse one ``HH:MM:SS role | text`` line; None when malformed."""
    try:
        match = _EVENT_RE.match(line.rstrip("\n").rstrip("\r"))
        if match is None:
            return None
        ts = match.group("ts")
        hour, minute, second = (int(part) for part in ts.split(":"))
        if hour > 23 or minute > 59 or second > 59:
            return None
        text = match.group("text")
        if text.startswith(" "):
            text = text[1:]
        return {
            "ts": ts,
            "role": match.group("role").strip(),
            "text": text.strip(),
        }
    except Exception:  # pragma: no cover - defensive
        return None


def parse_log(path: Path) -> Dict[str, Any]:
    """Parse a task transcript.

    The first six lines are a fixed header and the rest are event lines. Lines
    that are malformed (no pipe, bad timestamp) are skipped, never fatal. A
    partially written final line is skipped the same way, so reading while the
    writer appends is safe.
    """
    result = _empty_log()
    try:
        raw = Path(path).read_text(encoding="utf-8", errors="replace")
    except Exception:
        return result

    events: List[Dict[str, str]] = result["events"]
    for line in raw.splitlines():
        if not line.strip():
            continue
        event = _parse_event_line(line)
        if event is None:
            continue
        events.append(event)

    if not events:
        return result

    result["phase"] = events[-1]["role"]
    result["first_ts"] = events[0]["ts"]
    result["last_ts"] = events[-1]["ts"]

    tool_count = 0
    last_tool: Optional[str] = None
    last_error: Optional[str] = None
    summary: Optional[str] = None
    duration: Optional[float] = None
    has_final = False
    error_pending = False

    for event in events:
        role = event["role"]
        text = event["text"]
        if role == "tool":
            tool_count += 1
            last_tool = _tool_name(text)
            # A new call supersedes the previous verdict: the marker describes
            # the *latest* call only, so a worker that recovers stops showing
            # an error it no longer has.
            error_pending = False
        elif role == "result":
            if "ERROR" in text:
                last_error = text
                error_pending = True
            else:
                error_pending = False
        elif role == "final":
            has_final = True
            if _SUMMARY_MARK in text:
                summary = text.split(_SUMMARY_MARK, 1)[1].strip() or None
            match = _DURATION_RE.search(text)
            if match is not None:
                try:
                    duration = float(match.group("value"))
                except (TypeError, ValueError):
                    pass

    result["tool_count"] = tool_count
    result["error_pending"] = error_pending
    result["last_tool"] = last_tool
    result["last_error"] = last_error
    result["summary"] = summary
    result["has_final"] = has_final
    result["duration_s"] = duration
    return result


# --------------------------------------------------------------------------
# manifests


def read_manifest(path: Path) -> Dict[str, Any]:
    """Read one manifest. Returns ``{}`` for missing, truncated, or invalid files."""
    try:
        with Path(path).open("r", encoding="utf-8", errors="replace") as handle:
            data = json.load(handle)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def load_manifests(
    home: Optional[Path] = None, limit: int = 40
) -> List[Tuple[Path, Dict[str, Any]]]:
    """Load ``(manifest_path, manifest)`` pairs, newest first."""
    root = live_root(home)
    found: List[Tuple[Path, Dict[str, Any]]] = []
    try:
        if not root.is_dir():
            return []
        entries = list(root.iterdir())
    except Exception:
        return []

    for entry in entries:
        try:
            if not entry.is_dir():
                continue
            manifest_path = entry / "manifest.json"
            if not manifest_path.is_file():
                continue
            manifest = read_manifest(manifest_path)
            if not manifest:
                continue
            found.append((manifest_path, manifest))
        except Exception:
            continue

    def sort_key(item: Tuple[Path, Dict[str, Any]]) -> Any:
        manifest = item[1]
        started = manifest.get("started")
        did = manifest.get("delegation_id")
        if not isinstance(started, str):
            started = ""
        if not isinstance(did, str):
            did = item[0].parent.name
        return (started, did)

    found.sort(key=sort_key, reverse=True)
    try:
        cap = int(limit)
    except (TypeError, ValueError):
        cap = 40
    if cap >= 0:
        found = found[:cap]
    return found


# --------------------------------------------------------------------------
# state.db


def db_delegations(home: Optional[Path] = None) -> Dict[str, Dict[str, Any]]:
    """Read ``async_delegations`` rows, keyed by delegation id.

    Missing file or table yields ``{}``. A locked database yields whatever
    rows could be read before the busy timeout expired.
    """
    path = _db_path(home)
    try:
        if not path.is_file():
            return {}
    except Exception:
        return {}

    uri = "file:" + str(path) + "?mode=ro"
    connection = None
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=0.5)
        connection.row_factory = sqlite3.Row
        cursor = connection.cursor()
        try:
            cursor.execute("PRAGMA table_info(async_delegations)")
            available = {row[1] for row in cursor.fetchall()}
        except sqlite3.Error:
            return {}
        if not available or "delegation_id" not in available:
            return {}
        columns = [name for name in _DESIRED_DB_COLUMNS if name in available]
        select = ", ".join('"%s"' % name for name in columns)
        rows: Dict[str, Dict[str, Any]] = {}
        try:
            cursor.execute('SELECT %s FROM async_delegations' % select)
            for row in cursor.fetchall():
                record = {name: row[name] for name in columns}
                delegation_id = record.get("delegation_id")
                if isinstance(delegation_id, str) and delegation_id:
                    rows[delegation_id] = record
        except sqlite3.Error:
            return rows
        return rows
    except Exception:
        return {}
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass


# --------------------------------------------------------------------------
# review registry


def load_reviews(home: Optional[Path] = None) -> Dict[str, Any]:
    """Load the review registry, always returning a dict with a ``reviews`` list."""
    path = _reviews_path(home)
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            data = json.load(handle)
    except Exception:
        return {"reviews": []}
    if not isinstance(data, dict):
        return {"reviews": []}
    reviews = data.get("reviews")
    if not isinstance(reviews, list):
        data = dict(data)
        data["reviews"] = []
    return data


def _registry_records(reviews: Dict[str, Any]) -> List[Dict[str, Any]]:
    try:
        items = reviews.get("reviews") if isinstance(reviews, dict) else None
    except Exception:
        items = None
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]


def review_for(
    reviews: Dict[str, Any], delegation_id: str, task_index: int
) -> Dict[str, Any]:
    """Return the review record for a task, else the delegation-wide one, else ``{}``.

    An exact ``(delegation_id, task_index)`` match wins; a record with
    ``task_index`` of ``None`` applies to the whole delegation.
    """
    records = _registry_records(reviews)
    fallback: Dict[str, Any] = {}
    for record in records:
        if record.get("delegation_id") != delegation_id:
            continue
        index = record.get("task_index")
        if index is None:
            if not fallback:
                fallback = dict(record)
            continue
        try:
            if int(index) == int(task_index):
                return dict(record)
        except (TypeError, ValueError):
            continue
    return fallback


def is_reviewer_delegation(reviews: Dict[str, Any], delegation_id: str) -> bool:
    """True only when a registry record names this delegation as the reviewer.

    Being *reviewed* is not being a *reviewer*: a review record is written
    against the delegation it audits (``delegation_id``), and names the run that
    performed the audit (``reviewer_delegation``). Role must come from the second
    field only, or every audited executor would be relabelled as a reviewer.
    """
    for record in _registry_records(reviews):
        if record.get("reviewer_delegation") == delegation_id:
            return True
    return False


# --------------------------------------------------------------------------
# workers


@dataclass
class Worker:
    """One normalized unit of delegation work."""

    delegation_id: str
    task_index: int
    key: str
    role: str
    model: Optional[str]
    goal: str
    state: str
    exit_reason: Optional[str]
    phase: str
    tool_count: int
    last_tool: Optional[str]
    last_error: Optional[str]
    summary: Optional[str]
    started: Optional[str]
    duration_s: Optional[float]
    review: Dict[str, Any] = field(default_factory=dict)
    delegation_started: Optional[str] = None
    #: Absolute path of this task's append-only transcript, when one exists on
    #: disk. This is the only thing a worker pane is allowed to tail: a pane must
    #: show the real stream or nothing, never a synthesised summary.
    log_path: Optional[str] = None
    #: Wall-clock time of the worker's most recent transcript event. Drives the
    #: display-only "stall" label; without it a long but healthy run is marked
    #: stalled because only its start time was ever known.
    last_event: Optional[str] = None
    #: True when the most recent completed tool call reported an error.
    error_pending: bool = False


def _resolve_state(
    db_row: Optional[Dict[str, Any]],
    manifest_status: Any,
    log_exists: bool,
    log: Dict[str, Any],
) -> str:
    """Apply the documented precedence: db terminal > manifest > running > unknown."""
    if db_row:
        db_state = db_row.get("state")
        if isinstance(db_state, str) and db_state.strip():
            lowered = db_state.strip().lower()
            if lowered not in NON_TERMINAL_DB_STATES:
                return db_state.strip()
    if isinstance(manifest_status, str) and manifest_status.strip():
        return manifest_status.strip()
    if log_exists and not log.get("has_final"):
        return "running"
    return "unknown"


def _task_transcripts(db_row: Optional[Dict[str, Any]]) -> Dict[str, str]:
    """task index -> transcript path, from ``async_delegations.task_json``.

    Hermes stores the authoritative map at dispatch time
    (``task_json.task_transcripts``). The ``task-<n>.log`` naming convention is
    only a fallback: the map is what the writer actually used.
    """
    if not db_row:
        return {}
    raw = db_row.get("task_json")
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    mapping = payload.get("task_transcripts") if isinstance(payload, dict) else None
    if not isinstance(mapping, dict):
        return {}
    out: Dict[str, str] = {}
    for key, value in mapping.items():
        if isinstance(value, str) and value.strip():
            out[str(key)] = value
    return out


def _task_log_path(
    manifest_path: Path, task: Dict[str, Any], index: int,
    transcripts: Optional[Dict[str, str]] = None,
) -> Optional[Path]:
    declared = task.get("log")
    if isinstance(declared, str) and declared.strip():
        return Path(declared).expanduser()
    if transcripts:
        mapped = transcripts.get(str(index))
        if mapped:
            return Path(mapped).expanduser()
    try:
        return manifest_path.parent / ("task-%d.log" % index)
    except Exception:
        return None


def _combine_started(
    manifest_started: Optional[str], first_ts: Optional[str]
) -> Optional[str]:
    """Join the manifest's date with the transcript's time-of-day.

    The transcript only carries ``HH:MM:SS``; the manifest only carries the
    date the delegation started. Neither alone is a usable timestamp, so when
    both are present prefer the log's time on the manifest's date. Falls back
    to whichever one exists.
    """
    if isinstance(first_ts, str) and re.match(r"^\d{2}:\d{2}:\d{2}$", first_ts):
        if isinstance(manifest_started, str) and len(manifest_started) >= 10:
            date_part = manifest_started[:10]
            if re.match(r"^\d{4}-\d{2}-\d{2}$", date_part):
                combined = "%s %s" % (date_part, first_ts)
                # A delegation that runs past midnight logs times that belong to
                # the NEXT day. Taken on the start date they look ~24 h old, which
                # would label a healthy worker "stalled". If the event time is
                # before the start time of day, roll forward one day.
                start_time = manifest_started[11:19]
                if re.match(r"^\d{2}:\d{2}:\d{2}$", start_time) and first_ts < start_time:
                    try:
                        rolled = datetime.strptime(combined, "%Y-%m-%d %H:%M:%S") \
                            + timedelta(days=1)
                        return rolled.strftime("%Y-%m-%d %H:%M:%S")
                    except ValueError:
                        return combined
                return combined
        return first_ts
    if isinstance(manifest_started, str):
        return manifest_started
    return None


def load_workers(
    home: Optional[Path] = None, limit: int = 40
) -> List[Worker]:
    """Build normalized ``Worker`` records, running first.

    Ordering: running workers first, then by ``delegation_started`` descending,
    then by ``task_index``. Never raises.
    """
    workers: List[Worker] = []
    try:
        manifests = load_manifests(home, limit=limit)
        db_rows = db_delegations(home)
        reviews = load_reviews(home)

        for manifest_path, manifest in manifests:
            try:
                workers.extend(
                    _workers_for_manifest(
                        manifest_path, manifest, db_rows, reviews
                    )
                )
            except Exception:
                continue
    except Exception:
        return []

    try:
        workers.sort(key=lambda worker: worker.task_index)
        workers.sort(
            key=lambda worker: worker.delegation_started or "", reverse=True
        )
        workers.sort(key=lambda worker: 0 if worker.state == "running" else 1)
    except Exception:
        return workers

    try:
        cap = int(limit)
    except (TypeError, ValueError):
        cap = 40
    if cap >= 0:
        workers = workers[:cap]
    return workers


def _workers_for_manifest(
    manifest_path: Path,
    manifest: Dict[str, Any],
    db_rows: Dict[str, Dict[str, Any]],
    reviews: Dict[str, Any],
) -> List[Worker]:
    delegation_id = manifest.get("delegation_id")
    if not isinstance(delegation_id, str) or not delegation_id:
        delegation_id = manifest_path.parent.name
    if not delegation_id:
        return []

    manifest_started = manifest.get("started")
    if not isinstance(manifest_started, str):
        manifest_started = None
    manifest_model = manifest.get("model")
    if not isinstance(manifest_model, str):
        manifest_model = None

    db_row = db_rows.get(delegation_id)
    transcripts = _task_transcripts(db_row)
    is_reviewer = is_reviewer_delegation(reviews, delegation_id)

    tasks = manifest.get("tasks")
    if not isinstance(tasks, list):
        tasks = []

    workers: List[Worker] = []
    for position, task in enumerate(tasks):
        if not isinstance(task, dict):
            continue
        index_value = task.get("index")
        try:
            index = int(index_value)
        except (TypeError, ValueError):
            index = position

        goal = task.get("goal")
        goal = goal if isinstance(goal, str) else ""

        log_path = _task_log_path(manifest_path, task, index, transcripts)
        log_exists = False
        log = _empty_log()
        if log_path is not None:
            try:
                log_exists = log_path.is_file()
            except Exception:
                log_exists = False
            if log_exists:
                log = parse_log(log_path)

        status = task.get("status")
        state = _resolve_state(db_row, status, log_exists, log)

        exit_reason = task.get("exit_reason")
        if not isinstance(exit_reason, str):
            exit_reason = None

        model = task.get("model")
        if not isinstance(model, str) or not model:
            model = manifest_model

        summary = log.get("summary")
        duration = log.get("duration_s")
        started = _combine_started(manifest_started, log.get("first_ts"))
        try:
            tool_count = int(log.get("tool_count") or 0)
        except (TypeError, ValueError):
            tool_count = 0

        workers.append(
            Worker(
                delegation_id=delegation_id,
                task_index=index,
                key="%s#%d" % (delegation_id, index),
                role="reviewer" if is_reviewer else "executor",
                model=model,
                goal=goal,
                state=state,
                exit_reason=exit_reason,
                phase=log.get("phase") or "none",
                tool_count=tool_count,
                last_tool=log.get("last_tool"),
                last_error=log.get("last_error"),
                summary=summary,
                started=started,
                duration_s=duration if isinstance(duration, float) else None,
                review=review_for(reviews, delegation_id, index),
                delegation_started=manifest_started,
                log_path=str(log_path) if (log_path is not None and log_exists) else None,
                last_event=_combine_started(manifest_started, log.get("last_ts")),
                error_pending=bool(log.get("error_pending")),
            )
        )
    return workers
