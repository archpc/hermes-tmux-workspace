"""Unit tests for hermes_tmux.state.

Every fixture is built inside a ``tempfile.TemporaryDirectory``; nothing here
reads the real ``~/.hermes``.

The fixtures reconstruct the on-disk shapes ``state`` reads, and the exactness
matters:

* ``live_root`` is ``<home>/cache/delegation/live/<did>/`` holding
  ``manifest.json`` and ``task-<n>.log``;
* a transcript starts with the writer's six-line header, then
  ``HH:MM:SS role<pad>| text`` lines (``event_line`` reproduces the 9-column
  role field, because the parser does not require the padding but the writer
  emits it);
* ``state.db`` is created with a reduced ``async_delegations`` schema (no
  ``task_json``), so the reader's ``PRAGMA`` whitelist is exercised for the
  columns that exist; ``task_json`` is covered by ``test_state_json.py``.

What the tests are protecting, in one line each: state precedence (DB verdict >
manifest status > "transcript exists without a final line" > unknown), silence
is never read as completion, a half-written manifest is skipped rather than
mistaken for an empty delegation, and the review registry only ever adds
information — it never relabels a reviewed run as the reviewer.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hermes_tmux.state import (
    Worker,
    db_delegations,
    hermes_home,
    live_root,
    load_manifests,
    load_reviews,
    load_workers,
    parse_log,
    read_manifest,
    review_for,
)

HEADER = """=== Hermes subagent live transcript ===
delegation: {did}   task: {index}
goal: {goal}
started: {started}
(append-only; streams while the subagent runs — tail -f me)
========================================
"""


def event_line(ts, role, text):
    """Render an event line exactly like the Hermes writer (role left in 9)."""
    return "%s %-9s| %s" % (ts, role, text)


class FixtureMixin(unittest.TestCase):
    """Builds a fake HERMES_HOME tree on disk.

    Paths go through the module under test (``live_root``) rather than being
    spelled out again here, so a change to the layout breaks the fixtures
    instead of quietly testing the old one. Each helper writes one artifact and
    returns its path; tests compose them.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)

    # -- paths ---------------------------------------------------------
    def delegation_dir(self, delegation_id):
        path = live_root(self.home) / delegation_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    # -- artifacts -----------------------------------------------------
    def write_manifest(self, delegation_id, started, tasks, **extra):
        """Write a manifest; ``extra`` is how a test adds ``completed`` etc.

        Written with a plain ``write_text`` (not atomically) on purpose: the
        reader must tolerate a manifest that was caught mid-write, and one test
        relies on being able to leave truncated JSON on disk.
        """
        directory = self.delegation_dir(delegation_id)
        manifest = {
            "delegation_id": delegation_id,
            "started": started,
            "task_count": len(tasks),
            "model": "deepseek/deepseek-v4.1-flash-1017",
            "provider": "nous",
            "tasks": tasks,
        }
        manifest.update(extra)
        path = directory / "manifest.json"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        return path, manifest

    def write_task(
        self,
        delegation_id,
        index,
        goal="do the thing",
        started="2026-11-12 08:07:32",
        events=(),
    ):
        """Write one transcript: the six-line header plus ``events`` verbatim.

        ``events`` are raw lines, not structured records, so a test can put a
        malformed or truncated line on disk exactly as the writer would.
        """
        directory = self.delegation_dir(delegation_id)
        path = directory / ("task-%d.log" % index)
        body = HEADER.format(
            did=delegation_id, index=index, goal=goal, started=started
        )
        body += "".join(line + "\n" for line in events)
        path.write_text(body, encoding="utf-8")
        return path

    def task_entry(self, index, goal, log_path, status, exit_reason=None, model=None):
        """One ``tasks[]`` entry; optional keys are omitted, not set to None.

        This is the shape Hermes writes: an entry either carries an
        ``exit_reason``/``model`` or has no such key. Keeping the same shape is
        also what makes the round-trip test compare equal.
        """
        entry = {
            "index": index,
            "goal": goal,
            "log": str(log_path),
            "status": status,
        }
        if exit_reason is not None:
            entry["exit_reason"] = exit_reason
        if model is not None:
            entry["model"] = model
        return entry

    def write_db(self, rows):
        """Create ``state.db`` with a reduced ``async_delegations`` schema.

        The table omits ``task_json``, which Hermes writes and
        ``state._task_transcripts`` reads (``test_state_json.py`` covers that
        column), so the reader's ``PRAGMA table_info`` whitelist is exercised
        for the columns that are present rather than bypassed.
        """
        path = self.home / "state.db"
        connection = sqlite3.connect(str(path))
        try:
            connection.execute(
                "CREATE TABLE async_delegations ("
                "delegation_id TEXT PRIMARY KEY, "
                "parent_session_id TEXT, "
                "state TEXT, "
                "dispatched_at REAL, "
                "completed_at REAL, "
                "event_json TEXT, "
                "result_json TEXT)"
            )
            for row in rows:
                connection.execute(
                    "INSERT INTO async_delegations (delegation_id, parent_session_id,"
                    " state, dispatched_at, completed_at, event_json, result_json)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        row["delegation_id"],
                        row.get("parent_session_id"),
                        row.get("state"),
                        row.get("dispatched_at"),
                        row.get("completed_at"),
                        row.get("event_json"),
                        row.get("result_json"),
                    ),
                )
            connection.commit()
        finally:
            connection.close()
        return path

    def write_reviews(self, reviews):
        """The advisory registry; written only by a reviewer run that executed."""
        path = self.home / "cache" / "hermes-tmux" / "reviews.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"reviews": reviews}), encoding="utf-8")
        return path

    # -- shapes --------------------------------------------------------
    def two_task_delegation(self, delegation_id="deleg_6cfc92ad"):
        """The verified two-task shape: task 0 completed with a full log.

        Task 0 carries every event type the parser cares about (kickoff, a
        think/assistant duplicate, two tool calls, a final with duration and
        summary); task 1 is short. Task 1 has no ``model`` of its own, so the
        manifest-level model fallback is on the code path — not asserted
        separately, since no test needs that field.
        """
        log0 = self.write_task(
            delegation_id,
            0,
            goal="write the report",
            events=[
                event_line("08:07:33", "user", "kickoff: write the report"),
                event_line("08:07:33", "start", "write the report"),
                event_line("08:07:35", "think", "I should check the layout first."),
                event_line(
                    "08:07:36",
                    "assistant",
                    "I should check the layout first.",
                ),
                event_line(
                    "08:07:37",
                    "tool",
                    "-> execute_code(import json ...)",
                ),
                event_line(
                    "08:07:39",
                    "result",
                    'execute_code ok 2.0s: {"status": "success"}',
                ),
                event_line("08:07:41", "tool", "-> terminal(python3 -m unittest)"),
                event_line("08:08:02", "result", "terminal ok 21.0s: {}"),
                event_line("08:11:42", "final", "status=completed duration=249.99s "
                           "summary: Report written to /tmp/report.md"),
            ],
        )
        log1 = self.write_task(
            delegation_id,
            1,
            goal="write the tests",
            events=[
                event_line("08:07:33", "user", "kickoff: write the tests"),
                event_line("08:07:34", "tool", "-> read_file(tests/test_state.py)"),
                event_line("08:07:35", "result", "read_file ok 0.1s: {}"),
                event_line(
                    "08:14:39",
                    "final",
                    "status=completed duration=65.5s summary: Tests pass.",
                ),
            ],
        )
        tasks = [
            self.task_entry(
                0,
                "write the report",
                log0,
                "completed",
                exit_reason="completed",
                model="deepseek/deepseek-v4.1-flash-1017",
            ),
            self.task_entry(
                1, "write the tests", log1, "completed", exit_reason="completed"
            ),
        ]
        path, manifest = self.write_manifest(
            delegation_id, "2026-11-12 08:07:32", tasks,
            completed="2026-11-12 08:14:39",
        )
        return path, manifest, log0, log1


class TestPaths(FixtureMixin):
    """``hermes_home`` / ``live_root`` resolution, including the env override."""

    def test_live_root_defaults_under_home(self):
        self.assertEqual(
            live_root(self.home),
            self.home / "cache" / "delegation" / "live",
        )

    def test_live_root_uses_hermes_home_env(self):
        # An explicit argument and $HERMES_HOME must agree; the CLI relies on
        # the second when it is run inside a pane.
        with mock.patch.dict(os.environ, {"HERMES_HOME": str(self.home)}):
            self.assertEqual(hermes_home(), self.home)
            self.assertEqual(
                live_root(), self.home / "cache" / "delegation" / "live"
            )

    def test_live_root_falls_back_to_dot_hermes(self):
        # ``clear=True`` so an inherited HERMES_HOME cannot make this pass for
        # the wrong reason.
        env = dict(os.environ)
        env.pop("HERMES_HOME", None)
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(hermes_home(), Path("~/.hermes").expanduser())


class TestParseLog(FixtureMixin):
    """One transcript -> the per-task facts, with no verdicts invented.

    ``parse_log`` reports what the file contains: the last role seen, counts,
    the last tool, the last error text, the final line's duration and summary.
    It never converts the absence of a ``final`` line into a state.
    """

    def test_happy_path_fields(self):
        _path, _manifest, log0, _log1 = self.two_task_delegation()
        parsed = parse_log(log0)

        # first_ts/last_ts are times of day only; the date join happens later.
        self.assertEqual(parsed["first_ts"], "08:07:33")
        self.assertEqual(parsed["last_ts"], "08:11:42")
        self.assertEqual(parsed["phase"], "final")
        self.assertEqual(parsed["tool_count"], 2)
        self.assertEqual(parsed["last_tool"], "terminal")
        self.assertIsNone(parsed["last_error"])
        self.assertTrue(parsed["has_final"])
        self.assertAlmostEqual(parsed["duration_s"], 249.99)
        self.assertEqual(
            parsed["summary"], "Report written to /tmp/report.md"
        )

    def test_header_only_log_has_no_events(self):
        # The header does not parse as an event, so a log that only has the
        # header is indistinguishable from an empty one: all defaults.
        path = self.write_task("deleg_head", 0)
        parsed = parse_log(path)

        self.assertEqual(parsed["phase"], "none")
        self.assertEqual(parsed["tool_count"], 0)
        self.assertIsNone(parsed["last_tool"])
        self.assertIsNone(parsed["last_error"])
        self.assertIsNone(parsed["summary"])
        self.assertIsNone(parsed["first_ts"])
        self.assertIsNone(parsed["last_ts"])
        self.assertFalse(parsed["has_final"])
        self.assertIsNone(parsed["duration_s"])

    def test_malformed_lines_are_skipped(self):
        """Bad lines are skipped, but a *timestamped* partial line is an event.

        This is the surprising case: the writer's own buffered half-line
        (``-> partial line being app``) still starts with a valid timestamp and
        role, so it parses. What is rejected is the truly malformed input — no
        pipe, an out-of-range clock (``99:99:99``), a non-numeric time — because
        those would otherwise drive the stall label.
        """
        path = self.write_task(
            "deleg_bad",
            0,
            events=[
                "this line has no pipe at all",
                "99:99:99 tool     | -> bogus(should be skipped)",
                "08:00:01 tool     | -> read_file(x)",
                "08:00:02 result   | read_file ok 0.1s: {}",
                "08:00:03 not-a-time tool     | -> nope()",
                "",
                "08:00:04 final    | status=completed duration=3.0s summary: done",
                "08:00:05 tool     | -> partial line being app",
            ],
        )
        parsed = parse_log(path)

        self.assertEqual(parsed["first_ts"], "08:00:01")
        self.assertEqual(parsed["phase"], "tool")
        self.assertEqual(parsed["tool_count"], 2)
        self.assertEqual(parsed["last_tool"], "partial")
        self.assertTrue(parsed["has_final"])
        self.assertAlmostEqual(parsed["duration_s"], 3.0)
        self.assertEqual(parsed["summary"], "done")

    def test_result_error_is_captured_and_latest_wins(self):
        # ``last_error`` keeps the latest ERROR text even after a later ok
        # result clears ``error_pending``; the two fields answer different
        # questions ("what went wrong" vs "is something wrong now").
        path = self.write_task(
            "deleg_err",
            0,
            events=[
                event_line("09:00:01", "tool", "-> terminal(false)"),
                event_line(
                    "09:00:02", "result", 'terminal ERROR 0.1s: {"exit_code": 1}'
                ),
                event_line("09:00:03", "tool", "-> read_file(a)"),
                event_line(
                    "09:00:04", "result", 'read_file ERROR 0.1s: {"error": "ENOENT"}'
                ),
                event_line("09:00:05", "result", "read_file ok 0.1s: {}"),
            ],
        )
        parsed = parse_log(path)

        self.assertIn("ERROR", parsed["last_error"])
        self.assertIn("ENOENT", parsed["last_error"])
        self.assertEqual(parsed["tool_count"], 2)

    def test_final_without_summary_or_duration(self):
        # A minimal final line still proves the run ended; the missing fields
        # stay None rather than being defaulted to 0 or "".
        path = self.write_task(
            "deleg_fin",
            0,
            events=[event_line("09:10:00", "final", "status=completed")],
        )
        parsed = parse_log(path)

        self.assertTrue(parsed["has_final"])
        self.assertIsNone(parsed["summary"])
        self.assertIsNone(parsed["duration_s"])

    def test_missing_file_returns_defaults(self):
        # Total function: a path that does not exist is "nothing known", not an
        # exception the caller has to catch.
        parsed = parse_log(self.home / "does-not-exist.log")

        self.assertEqual(parsed["phase"], "none")
        self.assertFalse(parsed["has_final"])
        self.assertEqual(parsed["tool_count"], 0)


class TestManifests(FixtureMixin):
    """Discovery, per-manifest reads, and the failure modes of a live directory."""

    def test_read_manifest_round_trips(self):
        # Deep-copied out of the cache: a caller mutating the result must not
        # change what the next read sees.
        path, manifest, _l0, _l1 = self.two_task_delegation()

        self.assertEqual(read_manifest(path), manifest)

    def test_half_written_manifest_is_skipped_other_kept(self):
        # Hermes rewrites manifests in place. Truncated JSON must mean "no
        # answer this pass" — not "this delegation has no tasks" — so the broken
        # and empty ones are absent from the list and the good one survives.
        good_path, _manifest, _l0, _l1 = self.two_task_delegation()

        broken_dir = self.delegation_dir("deleg_broken")
        (broken_dir / "manifest.json").write_text(
            '{"delegation_id": "deleg_broken", "tasks": [{"index": 0,',
            encoding="utf-8",
        )
        empty_dir = self.delegation_dir("deleg_empty")
        (empty_dir / "manifest.json").write_text("", encoding="utf-8")

        loaded = load_manifests(self.home)
        ids = [manifest["delegation_id"] for _path, manifest in loaded]

        self.assertEqual(ids, ["deleg_6cfc92ad"])
        self.assertEqual(loaded[0][0], good_path)

        self.assertEqual(read_manifest(broken_dir / "manifest.json"), {})
        workers = load_workers(self.home)
        self.assertEqual([worker.key for worker in workers], [
            "deleg_6cfc92ad#0",
            "deleg_6cfc92ad#1",
        ])

    def test_missing_live_root_returns_empty(self):
        # Every reader is total: no directory at all yields empty shapes of the
        # right type, so a caller can iterate without a None check.
        self.assertEqual(load_manifests(self.home), [])
        self.assertEqual(load_workers(self.home), [])
        self.assertEqual(db_delegations(self.home), {})
        self.assertEqual(load_reviews(self.home), {"reviews": []})

    def test_live_root_that_is_a_file_does_not_raise(self):
        # A path that is a file where a directory is expected is a plausible
        # half-set-up state; it must read as empty, not as an OSError.
        root = live_root(self.home)
        root.parent.mkdir(parents=True, exist_ok=True)
        root.write_text("not a directory", encoding="utf-8")

        self.assertEqual(load_manifests(self.home), [])
        self.assertEqual(load_workers(self.home), [])

    def test_files_at_top_level_are_ignored(self):
        # Only directories are delegation candidates; a stray file is not one.
        self.two_task_delegation()
        (live_root(self.home) / "stray.json").write_text("{}", encoding="utf-8")

        loaded = load_manifests(self.home)
        self.assertEqual(len(loaded), 1)


class TestWorkers(FixtureMixin):
    """``load_workers``: field mapping, state precedence, ordering and limits.

    Each test isolates one decision. The precedence tests are pairs — a
    terminal DB state that must override the manifest, and a non-terminal one
    that must not — because either rule alone can be satisfied by a shortcut.
    """

    def test_happy_path_two_task_shape(self):
        _path, _manifest, log0, _log1 = self.two_task_delegation()
        workers = load_workers(self.home)

        self.assertEqual(len(workers), 2)
        first, second = workers
        self.assertIsInstance(first, Worker)
        self.assertEqual(first.key, "deleg_6cfc92ad#0")
        self.assertEqual(first.delegation_id, "deleg_6cfc92ad")
        self.assertEqual(first.task_index, 0)
        self.assertEqual(first.role, "executor")
        self.assertEqual(first.state, "completed")
        self.assertEqual(first.exit_reason, "completed")
        self.assertEqual(first.phase, "final")
        self.assertEqual(first.tool_count, 2)
        self.assertEqual(first.last_tool, "terminal")
        self.assertIsNone(first.last_error)
        self.assertEqual(first.summary, "Report written to /tmp/report.md")
        self.assertEqual(first.started, "2026-11-12 08:07:33")
        self.assertAlmostEqual(first.duration_s, 249.99)
        self.assertEqual(first.delegation_started, "2026-11-12 08:07:32")
        self.assertEqual(first.model, "deepseek/deepseek-v4.1-flash-1017")
        self.assertEqual(first.review, {})

        self.assertEqual(second.key, "deleg_6cfc92ad#1")
        self.assertEqual(second.task_index, 1)
        self.assertEqual(second.last_tool, "read_file")
        self.assertAlmostEqual(second.duration_s, 65.5)

        # started comes from the log's first event on the manifest's date, not
        # from the manifest's own start: the difference is one second here,
        # which is exactly enough to tell the two sources apart.
        self.assertEqual(parse_log(log0)["first_ts"], "08:07:33")

    def test_header_only_log_falls_back_to_manifest_started(self):
        # With no event to date the worker, the manifest start is the only
        # honest timestamp, and it is passed through unchanged.
        log = self.write_task("deleg_idle", 0, started="2026-11-12 10:00:00")
        self.write_manifest(
            "deleg_idle",
            "2026-11-12 10:00:00",
            [self.task_entry(0, "waiting", log, "running")],
        )
        workers = load_workers(self.home)

        self.assertEqual(len(workers), 1)
        worker = workers[0]
        self.assertEqual(worker.phase, "none")
        self.assertEqual(worker.state, "running")
        self.assertEqual(worker.started, "2026-11-12 10:00:00")
        self.assertEqual(worker.tool_count, 0)

    def test_log_silence_never_infers_completed(self):
        # No status, no log: unknown. Log without a final line: running.
        # Neither becomes "completed" just because nothing is contradicting it.
        missing = self.delegation_dir("deleg_quiet") / "task-9.log"
        log = self.write_task(
            "deleg_silent",
            0,
            events=[event_line("10:00:01", "think", "hmm")],
        )
        self.write_manifest(
            "deleg_silent",
            "2026-11-12 10:00:00",
            [self.task_entry(0, "silent", log, None)],
        )
        self.write_manifest(
            "deleg_quiet",
            "2026-11-12 09:00:00",
            [self.task_entry(0, "quiet", missing, None)],
        )
        workers = {worker.key: worker for worker in load_workers(self.home)}

        self.assertEqual(workers["deleg_silent#0"].state, "running")
        self.assertEqual(workers["deleg_silent#0"].phase, "think")
        self.assertEqual(workers["deleg_quiet#0"].state, "unknown")

    def test_non_success_manifest_status_passed_through_verbatim(self):
        # Hermes' own vocabulary is kept, not normalised: "interrupted" stays
        # "interrupted" so the display layer can decide how to colour it.
        log = self.write_task(
            "deleg_fail",
            0,
            events=[event_line("11:00:00", "final", "status=failed")],
        )
        self.write_manifest(
            "deleg_fail",
            "2026-11-12 11:00:00",
            [self.task_entry(0, "boom", log, "interrupted", "interrupted")],
        )
        worker = load_workers(self.home)[0]

        self.assertEqual(worker.state, "interrupted")
        self.assertEqual(worker.exit_reason, "interrupted")

    def test_db_terminal_state_wins_over_manifest(self):
        # The DB is Hermes' own record of the batch outcome; where it has a
        # terminal verdict, a stale manifest must not outvote it.
        _path, _manifest, _l0, _l1 = self.two_task_delegation()
        self.write_db([
            {"delegation_id": "deleg_6cfc92ad", "state": "failed"},
        ])
        workers = load_workers(self.home)

        self.assertEqual([worker.state for worker in workers], ["failed", "failed"])

    def test_db_non_terminal_state_does_not_override_manifest(self):
        # The mirror rule: "running" in the DB is not a verdict, so the
        # manifest's completed tasks stay completed.
        _path, _manifest, _l0, _l1 = self.two_task_delegation()
        self.write_db([
            {"delegation_id": "deleg_6cfc92ad", "state": "running"},
        ])
        workers = load_workers(self.home)

        self.assertEqual(
            [worker.state for worker in workers], ["completed", "completed"]
        )

    def test_db_absent_and_db_without_table(self):
        # Three absences in one test: no file, an empty database, and (via the
        # worker list) the fallback to manifest state. None of them is an error.
        _path, _manifest, _l0, _l1 = self.two_task_delegation()
        self.assertEqual(db_delegations(self.home), {})

        empty_db = self.home / "state.db"
        sqlite3.connect(str(empty_db)).close()
        self.assertEqual(db_delegations(self.home), {})

        self.assertEqual(
            [worker.state for worker in load_workers(self.home)],
            ["completed", "completed"],
        )

    def test_db_rows_are_keyed_by_delegation(self):
        # The reader hands back the whole row dict (whitelisted columns), keyed
        # by delegation id; unused columns are still available to callers.
        self.write_db([
            {"delegation_id": "deleg_a", "state": "completed", "dispatched_at": 1.0},
            {"delegation_id": "deleg_b", "state": "failed"},
        ])
        rows = db_delegations(self.home)

        self.assertEqual(set(rows), {"deleg_a", "deleg_b"})
        self.assertEqual(rows["deleg_a"]["dispatched_at"], 1.0)
        self.assertEqual(rows["deleg_b"]["state"], "failed")

    def test_sorting_running_first_then_newest(self):
        # Live work first (it is what the reader can act on), then newest
        # first inside each half. Four cases so both halves are exercised in
        # both orders.
        def add(did, started, status, events):
            log = self.write_task(did, 0, started=started, events=events)
            self.write_manifest(
                did, started, [self.task_entry(0, did, log, status)]
            )

        add("deleg_old_done", "2026-11-12 08:00:00", "completed", [
            event_line("08:00:01", "final", "status=completed duration=1.0s"),
        ])
        add("deleg_new_done", "2026-11-12 09:00:00", "completed", [
            event_line("09:00:01", "final", "status=completed duration=1.0s"),
        ])
        add("deleg_old_run", "2026-11-12 07:00:00", "running", [
            event_line("07:00:01", "tool", "-> terminal(ls)"),
        ])
        add("deleg_new_run", "2026-11-12 10:00:00", "running", [
            event_line("10:00:01", "tool", "-> terminal(ls)"),
        ])
        keys = [worker.key for worker in load_workers(self.home)]

        self.assertEqual(keys, [
            "deleg_new_run#0",
            "deleg_old_run#0",
            "deleg_new_done#0",
            "deleg_old_done#0",
        ])

    def test_task_index_breaks_ties_within_delegation(self):
        # Siblings share a delegation_started, so the index is the only stable
        # tie-break; the reader must not reorder them by dict or log order.
        logs = [
            self.write_task("deleg_multi", index, events=[
                event_line("09:00:01", "tool", "-> terminal(ls)"),
            ])
            for index in (0, 1, 2)
        ]
        self.write_manifest(
            "deleg_multi",
            "2026-11-12 09:00:00",
            [self.task_entry(i, "t%d" % i, logs[i], "running") for i in range(3)],
        )
        self.assertEqual(
            [worker.task_index for worker in load_workers(self.home)], [0, 1, 2]
        )

    def test_limit_truncates(self):
        # ``limit=0`` means "no workers": load_manifests/load_workers document
        # 0 as a cap of zero, distinct from a negative limit meaning all. No
        # current caller passes 0.
        _path, _manifest, _l0, _l1 = self.two_task_delegation()
        self.assertEqual(len(load_workers(self.home, limit=1)), 1)
        self.assertEqual(len(load_workers(self.home, limit=0)), 0)

    def test_missing_log_path_falls_back_to_sibling(self):
        # A manifest entry with no ``log`` key must not lose the transcript: the
        # ``task-<n>.log`` sibling convention is the last resort, and this pins
        # that it is actually consulted.
        directory = self.delegation_dir("deleg_nolog")
        self.write_task("deleg_nolog", 0, events=[
            event_line("12:00:01", "tool", "-> terminal(ls)"),
        ])
        entry = {"index": 0, "goal": "no log key", "status": "running"}
        (directory / "manifest.json").write_text(
            json.dumps({
                "delegation_id": "deleg_nolog",
                "started": "2026-11-12 12:00:00",
                "tasks": [entry],
            }),
            encoding="utf-8",
        )
        worker = load_workers(self.home)[0]

        self.assertEqual(worker.tool_count, 1)
        self.assertEqual(worker.last_tool, "terminal")


class TestReviews(FixtureMixin):
    """The review registry: an optional, advisory artifact.

    Two independent rules are tested here. A malformed registry is "no reviews".
    And role must come only from ``reviewer_delegation``: a review record is
    written *against* the delegation it audits, so reading ``delegation_id``
    as the reviewer would relabel every audited run.
    """

    def test_review_registry_missing_or_invalid(self):
        # Missing, unparsable, and wrong-typed all collapse to the same empty
        # registry, because the absence of evidence must not read as a verdict.
        self.assertEqual(load_reviews(self.home), {"reviews": []})

        path = self.home / "cache" / "hermes-tmux" / "reviews.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        self.assertEqual(load_reviews(self.home), {"reviews": []})

        path.write_text(json.dumps({"reviews": "nope"}), encoding="utf-8")
        self.assertEqual(load_reviews(self.home), {"reviews": []})

    def test_review_for_exact_and_delegation_wide(self):
        # Exact (delegation, task) wins; a record with ``task_index: None``
        # covers the whole delegation and is the fallback for other tasks.
        reviews = {"reviews": [
            {
                "id": "rev_ab12cd34",
                "delegation_id": "deleg_6cfc92ad",
                "task_index": 0,
                "reviewer_model": "z-ai/glm-5.3-flash",
                "status": "recorded",
                "report": "/abs/path.md",
                "at": "2026-11-12 09:00:00",
            },
            {
                "id": "rev_whole",
                "delegation_id": "deleg_6cfc92ad",
                "task_index": None,
                "status": "failed",
            },
        ]}

        exact = review_for(reviews, "deleg_6cfc92ad", 0)
        self.assertEqual(exact["id"], "rev_ab12cd34")

        fallback = review_for(reviews, "deleg_6cfc92ad", 1)
        self.assertEqual(fallback["id"], "rev_whole")

        self.assertEqual(review_for(reviews, "deleg_other", 0), {})
        self.assertEqual(review_for({"reviews": []}, "deleg_6cfc92ad", 0), {})

    def test_reviewed_delegation_stays_an_executor(self):
        """Being reviewed must not relabel the reviewed run as the reviewer.

        A review record is written against the delegation it audits, so the
        audited delegation is the executor. Only a delegation named in
        ``reviewer_delegation`` is a reviewer.
        """
        _path, _manifest, _l0, _l1 = self.two_task_delegation()
        self.write_reviews([
            {
                "id": "rev_ab12cd34",
                "delegation_id": "deleg_6cfc92ad",
                "task_index": 0,
                "reviewer_model": "z-ai/glm-5.3-flash",
                "status": "recorded",
                "report": "/abs/path.md",
                "at": "2026-11-12 09:00:00",
            },
        ])
        workers = {worker.key: worker for worker in load_workers(self.home)}

        self.assertEqual(workers["deleg_6cfc92ad#0"].role, "executor")
        self.assertEqual(workers["deleg_6cfc92ad#1"].role, "executor")
        # The verdict attaches to the reviewed task only, not to its sibling.
        self.assertEqual(
            workers["deleg_6cfc92ad#0"].review["id"], "rev_ab12cd34"
        )
        self.assertEqual(workers["deleg_6cfc92ad#1"].review, {})

    def test_delegation_named_as_reviewer_is_a_reviewer(self):
        _path, _manifest, _l0, _l1 = self.two_task_delegation()
        self.write_reviews([
            {
                "id": "rev_ee00ff11",
                "delegation_id": "deleg_someone_else",
                "task_index": None,
                "reviewer_delegation": "deleg_6cfc92ad",
                "reviewer_model": "z-ai/glm-5.3-flash",
                "status": "recorded",
            },
        ])
        workers = {worker.key: worker for worker in load_workers(self.home)}
        self.assertEqual(workers["deleg_6cfc92ad#0"].role, "reviewer")
        self.assertEqual(workers["deleg_6cfc92ad#1"].role, "reviewer")
        # The reviewer run carries no review verdict about itself.
        self.assertEqual(workers["deleg_6cfc92ad#0"].review, {})

    def test_goal_text_mentioning_review_does_not_make_reviewer(self):
        # Role comes from the registry, never from the goal's wording: a task
        # whose goal says "review the design doc" is still an executor.
        log = self.write_task(
            "deleg_plain", 0, goal="review the design doc for the reviewers"
        )
        self.write_manifest(
            "deleg_plain",
            "2026-11-12 13:00:00",
            [self.task_entry(0, "review the design doc", log, "running")],
        )
        worker = load_workers(self.home)[0]

        self.assertEqual(worker.role, "executor")
        self.assertEqual(worker.review, {})


if __name__ == "__main__":
    unittest.main()
