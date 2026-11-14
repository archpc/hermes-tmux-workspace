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

import copy
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
        "last_call": None,
        "last_message": None,
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


_STAT_CACHE_MAX = 512
_LOG_CACHE: Dict[str, Tuple[Tuple[int, int, int, int], Dict[str, Any]]] = {}
_MANIFEST_CACHE: Dict[str, Tuple[Tuple[int, int, int, int], Dict[str, Any]]] = {}


def _stat_sig(path: Path) -> Optional[Tuple[int, int, int, int]]:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)


def _copy_log(parsed: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(parsed)
    out["events"] = []
    return out


def parse_log(path: Path) -> Dict[str, Any]:
    """Parse a task transcript, cached by ``(st_dev, st_ino, st_size, st_mtime_ns)``.

    The first six lines are a fixed header and the rest are event lines. Lines
    that are malformed (no pipe, bad timestamp) are skipped, never fatal. A
    partially written final line is skipped the same way, so reading while the
    writer appends is safe. Finished transcripts on disk never change, so
    keying by stat metadata avoids re-reading megabytes of historical logs on
    unrelated SQLite WAL wakeups while immediately invalidating on any append
    or atomic file replacement.
    """
    p = Path(path)
    key = str(p)
    sig = _stat_sig(p)
    if sig is None:
        _LOG_CACHE.pop(key, None)
        return _empty_log()
    cached = _LOG_CACHE.get(key)
    if cached is not None and cached[0] == sig:
        return _copy_log(cached[1])

    result = _empty_log()
    try:
        raw = p.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return result

    events: List[Dict[str, str]] = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        event = _parse_event_line(line)
        if event is None:
            continue
        events.append(event)

    if not events:
        if len(_LOG_CACHE) >= _STAT_CACHE_MAX and key not in _LOG_CACHE:
            _LOG_CACHE.pop(next(iter(_LOG_CACHE)), None)
        _LOG_CACHE[key] = (sig, result)
        return _copy_log(result)

    result["phase"] = events[-1]["role"]
    result["first_ts"] = events[0]["ts"]
    result["last_ts"] = events[-1]["ts"]

    tool_count = 0
    last_tool: Optional[str] = None
    last_call: Optional[str] = None
    last_message: Optional[str] = None
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
            call = text.strip()
            last_call = (call[len(_TOOL_ARROW):] if call.startswith(_TOOL_ARROW) else call).strip() or None
            # A new call supersedes the previous verdict: the marker describes
            # the *latest* call only, so a worker that recovers stops showing
            # an error it no longer has.
            error_pending = False
        elif role == "assistant":
            last_message = text or last_message
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
    result["last_call"] = last_call
    result["last_message"] = last_message
    result["last_error"] = last_error
    result["summary"] = summary
    result["has_final"] = has_final
    result["duration_s"] = duration
    if len(_LOG_CACHE) >= _STAT_CACHE_MAX and key not in _LOG_CACHE:
        _LOG_CACHE.pop(next(iter(_LOG_CACHE)), None)
    _LOG_CACHE[key] = (sig, result)
    return _copy_log(result)


# --------------------------------------------------------------------------
# manifests


def read_manifest(path: Path) -> Dict[str, Any]:
    """Read one manifest (stat-cached). Returns ``{}`` for missing or invalid files."""
    p = Path(path)
    key = str(p)
    sig = _stat_sig(p)
    if sig is None:
        _MANIFEST_CACHE.pop(key, None)
        return {}
    cached = _MANIFEST_CACHE.get(key)
    if cached is not None and cached[0] == sig:
        return copy.deepcopy(cached[1])
    try:
        with p.open("r", encoding="utf-8", errors="replace") as handle:
            data = json.load(handle)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    if len(_MANIFEST_CACHE) >= _STAT_CACHE_MAX and key not in _MANIFEST_CACHE:
        _MANIFEST_CACHE.pop(next(iter(_MANIFEST_CACHE)), None)
    _MANIFEST_CACHE[key] = (sig, data)
    return copy.deepcopy(data)


def _manifest_has_live_task(manifest: Dict[str, Any]) -> bool:
    """True when any task in ``manifest`` may still be active.

    Keeps long-running delegations from being evicted out of ``load_manifests``
    when more than ``limit`` short-lived delegations start after them.
    """
    tasks = manifest.get("tasks")
    if isinstance(tasks, list) and tasks:
        for task in tasks:
            if not isinstance(task, dict):
                continue
            status = task.get("status")
            if status is None or (isinstance(status, str) and not status.strip()):
                if not manifest.get("completed"):
                    return True
                continue
            if isinstance(status, str) and status.strip().lower() in NON_TERMINAL_DB_STATES:
                return True
        return False
    return not bool(manifest.get("completed"))


def load_manifests(
    home: Optional[Path] = None, limit: int = 40
) -> List[Tuple[Path, Dict[str, Any]]]:
    """Load ``(manifest_path, manifest)`` pairs, active first then newest first."""
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
    found.sort(key=lambda item: 0 if _manifest_has_live_task(item[1]) else 1)
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
    #: The most recent tool call as the transcript wrote it, name and argument
    #: preview ("terminal(pytest -q)"): what the worker is doing right now.
    last_call: Optional[str] = None
    #: The last thing the worker itself said. Shown beside a failed run: how
    #: the run ended (a provider error, an interruption) and what the worker
    #: had reported by then are separate facts, and only the first is ours to
    #: state.
    last_message: Optional[str] = None


def _db_row_for_task(
    db_rows: Dict[str, Dict[str, Any]], delegation_id: str, task_index: int
) -> Optional[Dict[str, Any]]:
    """Find the ``async_delegations`` row governing ``(delegation_id, task_index)``.

    Under ``delegation.independent_completions``, Hermes dispatches one DB unit
    per task under ``<delegation_id>-<k>`` with ``task_json.task_indexes`` naming
    the covered task index(es), while ``manifest.json`` stays keyed by the base
    ``delegation_id``. Check suffixed unit rows first, then the batch row.
    """
    prefix = delegation_id + "-"
    for key, row in db_rows.items():
        if not key.startswith(prefix):
            continue
        raw_task = row.get("task_json")
        if isinstance(raw_task, str) and raw_task.strip():
            try:
                payload = json.loads(raw_task)
            except (TypeError, ValueError):
                payload = None
            if isinstance(payload, dict):
                indexes = payload.get("task_indexes")
                if isinstance(indexes, list):
                    for item in indexes:
                        try:
                            if int(item) == task_index:
                                return row
                        except (TypeError, ValueError):
                            continue
                    continue
        if delegation_id not in db_rows:
            suffix = key[len(prefix):]
            try:
                if int(suffix) - 1 == task_index:
                    return row
            except ValueError:
                continue
    return db_rows.get(delegation_id)


def _db_task_verdict(
    db_row: Optional[Dict[str, Any]], task_index: int, unit_task_count: int
) -> Tuple[Optional[str], Optional[str]]:
    """Extract per-task ``(status, exit_reason)`` from ``db_row['result_json']``."""
    if not db_row:
        return (None, None)
    raw = db_row.get("result_json")
    if not isinstance(raw, str) or not raw.strip():
        return (None, None)
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return (None, None)
    if not isinstance(payload, dict):
        return (None, None)
    results = payload.get("results")
    if isinstance(results, list):
        chosen: Optional[Dict[str, Any]] = None
        for entry in results:
            if not isinstance(entry, dict):
                continue
            raw_idx = entry.get("task_index")
            if raw_idx is None:
                continue
            try:
                if int(raw_idx) == task_index:
                    chosen = entry
                    break
            except (TypeError, ValueError):
                continue
        if chosen is None and 0 <= task_index < len(results):
            cand = results[task_index]
            if isinstance(cand, dict) and "task_index" not in cand:
                chosen = cand
        if chosen is not None:
            st = chosen.get("status")
            er = chosen.get("exit_reason")
            return (
                st.strip() if isinstance(st, str) and st.strip() else None,
                er.strip() if isinstance(er, str) and er.strip() else None,
            )
    if unit_task_count <= 1:
        st = payload.get("status")
        er = payload.get("exit_reason")
        return (
            st.strip() if isinstance(st, str) and st.strip() else None,
            er.strip() if isinstance(er, str) and er.strip() else None,
        )
    return (None, None)


def _resolve_state(
    db_row: Optional[Dict[str, Any]],
    manifest_status: Any,
    log_exists: bool,
    log: Dict[str, Any],
    *,
    task_db_status: Optional[str] = None,
    unit_task_count: int = 1,
) -> str:
    """Resolve worker state: per-task DB verdict > batch DB verdict > manifest > running > unknown.

    In multi-task batches, Hermes sets ``async_delegations.state = 'completed'``
    when the batch finishes delivering mixed per-task outcomes, recording each
    subagent's real verdict in ``result_json.results`` and ``manifest.json``.
    A batch-level ``completed`` must therefore never mask a per-task terminal
    failure/interruption in ``manifest_status``, whereas non-success batch DB
    verdicts (``failed``, ``unknown`` from crash recovery, ``interrupted``)
    still override stale ``running`` manifests.
    """
    if isinstance(task_db_status, str) and task_db_status.strip():
        if task_db_status.strip().lower() not in NON_TERMINAL_DB_STATES:
            return task_db_status.strip()
    if db_row:
        db_state = db_row.get("state")
        if isinstance(db_state, str) and db_state.strip():
            lowered = db_state.strip().lower()
            if lowered not in NON_TERMINAL_DB_STATES:
                if (
                    unit_task_count > 1
                    and lowered == "completed"
                    and isinstance(manifest_status, str)
                    and manifest_status.strip()
                    and manifest_status.strip().lower() not in NON_TERMINAL_DB_STATES
                ):
                    return manifest_status.strip()
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

    base_db_row = db_rows.get(delegation_id)
    base_transcripts = _task_transcripts(base_db_row)
    is_reviewer = is_reviewer_delegation(reviews, delegation_id)

    tasks = manifest.get("tasks")
    if not isinstance(tasks, list):
        tasks = []
    manifest_task_count = max(len(tasks), 1)

    workers: List[Worker] = []
    for position, task in enumerate(tasks):
        if not isinstance(task, dict):
            continue
        index_value = task.get("index")
        try:
            index = int(index_value)
        except (TypeError, ValueError):
            index = position

        task_db_row = _db_row_for_task(db_rows, delegation_id, index)
        transcripts = dict(base_transcripts)
        if task_db_row is not base_db_row:
            transcripts.update(_task_transcripts(task_db_row))
        unit_count = 1 if (task_db_row is not None and task_db_row is not base_db_row) else manifest_task_count
        task_db_status, task_db_exit = _db_task_verdict(task_db_row, index, unit_count)

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
        state = _resolve_state(
            task_db_row, status, log_exists, log,
            task_db_status=task_db_status, unit_task_count=unit_count,
        )

        exit_reason = task.get("exit_reason")
        if not isinstance(exit_reason, str) or not exit_reason:
            exit_reason = task_db_exit if isinstance(task_db_exit, str) else None

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
                last_call=log.get("last_call"),
                last_message=log.get("last_message"),
            )
        )
    return workers
