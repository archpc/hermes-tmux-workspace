"""Unit tests for hermes_tmux.labels — the naming and state vocabulary.

Pure functions only: no tmux, no clock. Everything asserted here is what the
tab, the pane header, the overview and the transcript read from.

The vocabulary is deliberately small and total: ``state_kind`` collapses every
string outside {run, stall, done, fail} to ``other``, so a state this project
has never seen shows as UNKNOWN instead of being guessed into a known badge.
The tests below pin both halves of that — each known kind's glyph/colour/word,
and the degradation of everything else.

Text handling is the other half of the module's job: a goal and a transcript are
untrusted strings. ``title``/``tab_label`` never return something wider than
asked, and ``clip`` is width-exact except at ``width <= 0``, where it returns
``''``; ``clean``/``defuse`` must make a string one inert line, and
``tmux_escape`` must survive being parsed as a tmux format.
"""

from __future__ import annotations

import unittest

from hermes_tmux import labels


class TestTitle(unittest.TestCase):
    """The first clause of a goal — a name that is the parent's words, not ours.

    The only non-verbatim decisions are the clause break (which part is "first")
    and the 12-character floor, because ``"Fix: the parser"`` cut at the colon
    would name the task "Fix".
    """

    def test_first_clause_ends_at_a_sentence_break(self):
        self.assertEqual(labels.title("Implement state.py, then tests. And more."),
                         "Implement state.py, then tests")

    def test_dash_clause_break(self):
        self.assertEqual(labels.title("Audit shell-startup \u2014 report the results"),
                         "Audit shell-startup")

    def test_colon_after_the_minimum_is_a_break(self):
        self.assertEqual(labels.title("Fix the reader: now and forever"), "Fix the reader")

    def test_break_in_the_first_twelve_characters_is_a_prefix(self):
        # "Fix: ..." would leave a one-word title, so the colon is kept. The
        # same rule applies to a dash ("--") for the same reason.
        self.assertEqual(labels.title("Fix: the reader"), "Fix: the reader")
        self.assertEqual(labels.title("Ship it -- really this time"),
                         "Ship it -- really this time")

    def test_clause_break_at_exactly_twelve_is_used(self):
        # "Audit state1" is 12 characters, so the colon that follows is a break.
        # Pins the boundary as >= 12, not > 12.
        self.assertEqual(labels.title("Audit state1: and then run them"), "Audit state1")

    def test_trailing_punctuation_is_stripped(self):
        # The second assertion is the invariant behind the first: a stripped
        # title is the same string as a goal that never had the stop.
        self.assertEqual(labels.title("Review task 0."), "Review task 0")
        self.assertEqual(labels.title("Review task 0."), labels.title("Review task 0"))

    def test_newline_break(self):
        self.assertEqual(labels.title("First line here\nsecond line here"), "First line here")

    def test_whitespace_is_collapsed(self):
        self.assertEqual(labels.title("Fix\tstate\nreader   and tests"), "Fix state reader and tests")

    def test_empty_goal_has_a_placeholder(self):
        # None, "" and whitespace all take the same branch: a missing goal is
        # stated, never rendered as an empty tab.
        for goal in ("", None, "   "):
            self.assertEqual(labels.title(goal), "(no goal recorded)")

    def test_short_goal_is_kept_verbatim(self):
        self.assertEqual(labels.title("short"), "short")


class TestClip(unittest.TestCase):
    """Width-bounded clipping with a word-boundary preference.

    Precedence, as implemented: fits → untouched; width ≤ ellipsis → hard cut;
    otherwise cut at a boundary, but only if that boundary keeps more than half
    the room; then drop a dangling connective. The width bound is absolute and
    is asserted for every width in the last test.
    """

    def test_short_text_is_untouched(self):
        self.assertEqual(labels.clip("short", 10), "short")
        self.assertEqual(labels.clip("exact", 5), "exact")

    def test_zero_width_is_empty(self):
        self.assertEqual(labels.clip("anything", 0), "")
        self.assertEqual(labels.clip("anything", -3), "")

    def test_width_at_or_below_the_ellipsis_is_a_hard_cut(self):
        # No room for "…" itself, so the cut is bare: 1 and 2 columns.
        self.assertEqual(labels.clip("abcdef", 1), "a")
        self.assertEqual(labels.clip("abcdef", 2), "a\u2026")

    def test_cuts_at_a_word_boundary(self):
        self.assertEqual(labels.clip("Audit shell-startup and report the results", 22),
                         "Audit shell-startup\u2026")

    def test_trailing_stop_word_is_dropped(self):
        # "and..." says less than a clean stop at the noun.
        self.assertEqual(labels.clip("Audit shell-startup and report the results", 30),
                         "Audit shell-startup\u2026")

    def test_another_stop_word(self):
        # The boundary lands after "for"; the trailing preposition goes too.
        self.assertEqual(labels.clip("Write the tests for the parser", 20),
                         "Write the tests\u2026")

    def test_mid_word_fallback_when_the_boundary_is_far(self):
        # No space at all: there is no boundary to prefer, so the cut is a
        # plain prefix plus the ellipsis.
        self.assertEqual(labels.clip("abcdefghijklmnopqrstuvwxyz", 10), "abcdefghi\u2026")

    def test_boundary_in_the_far_half_is_used(self):
        self.assertEqual(labels.clip("one two three four five", 12), "one two\u2026")

    def test_ascii_ellipsis(self):
        self.assertEqual(labels.clip("one two three four five six", 12, "..."), "one two...")

    def test_never_wider_than_the_width(self):
        # The load-bearing test: every width from 1 to past the end, both
        # markers. The width was reserved for the marker before the cut, so
        # this is an equality of budget, not an approximation.
        text = "Audit shell-startup and report every result to the parent agent"
        for width in range(1, len(text) + 2):
            for marker in ("\u2026", "..."):
                out = labels.clip(text, width, marker)
                self.assertLessEqual(len(out), width, "width=%d marker=%r" % (width, marker))


class TestTabLabel(unittest.TestCase):
    """The window name: shortened for a tab, stable for the life of the run.

    Three reductions, in order: drop articles ("the", "a", "an", "please"),
    reduce a path to its basename, drop the words every sibling goal shares at
    the front. Each has a floor: at least one word always survives, so a tab is
    never unnamed.
    """

    def test_articles_are_dropped(self):
        self.assertEqual(labels.tab_label("Implement the state reader"), "Implement state reader")

    def test_sibling_prefix_is_dropped(self):
        # All three siblings say "Implement": that word distinguishes nothing
        # on a tab, so it goes and the file name remains.
        self.assertEqual(labels.tab_label("Implement state.py",
                                          ["Implement render.py", "Implement labels.py"]),
                         "state.py")

    def test_no_siblings_keeps_the_title(self):
        self.assertEqual(labels.tab_label("Implement state.py"), "Implement state.py")

    def test_path_is_reduced_to_its_basename(self):
        # A path in a goal is what the reader would have to read to tell two
        # tabs apart; the last segment is the part that differs.
        self.assertEqual(labels.tab_label("Read /Users/sein/Code/x/y.py"), "Read y.py")

    def test_hash_is_stripped(self):
        self.assertEqual(labels.tab_label("Fix issue #123 in state.py"), "Fix issue 123 in state.py")

    def test_one_word_title_survives_sibling_deduplication(self):
        # Everything cannot be dropped: at least one word stays, so two
        # identical one-word goals still get a (possibly identical) name rather
        # than an empty one.
        self.assertEqual(labels.tab_label("Maybe", ["Maybe"]), "Maybe")

    def test_identical_sibling_does_not_erase_the_label(self):
        # An identical sibling is filtered out of the comparison set, so it
        # cannot make every word look "shared".
        self.assertEqual(labels.tab_label("Implement state.py", ["Implement state.py"]),
                         "Implement state.py")

    def test_never_returns_empty(self):
        for goal in ("", "   ", None, "the a an please"):
            out = labels.tab_label(goal)
            self.assertTrue(out.strip(), repr(goal))

    def test_respects_the_width(self):
        out = labels.tab_label("Implement state.py and tests", width=12)
        self.assertLessEqual(len(out), 12)
        self.assertTrue(out.endswith("\u2026"))

    def test_ascii_mode_uses_an_ascii_ellipsis(self):
        out = labels.tab_label("Implement state.py", ascii_mode=True, width=8)
        self.assertEqual(out, "Imple...")
        self.assertTrue(all(ord(ch) < 128 for ch in out))

    def test_labelled_by_the_first_clause(self):
        self.assertEqual(labels.tab_label("Fix reader, then run tests. More follows."),
                         "Fix reader, then run tests")


class TestModelShort(unittest.TestCase):
    """What a reader calls a model: no provider prefix, no release date."""

    def test_provider_prefix_is_removed(self):
        # The date suffix goes too, so this is "deepseek-v4.1-flash".
        self.assertEqual(labels.model_short("deepseek/deepseek-v4.1-flash-1017"),
                         "deepseek-v4.1-flash")

    def test_release_date_suffix_is_removed(self):
        self.assertEqual(labels.model_short("claude-sonnet-4-5-20250929"), "claude-sonnet-4-5")
        self.assertEqual(labels.model_short("z-ai/glm-5.3-flash"), "glm-5.3-flash")

    def test_model_without_prefix_or_date_is_kept(self):
        self.assertEqual(labels.model_short("glm-5"), "glm-5")

    def test_missing_model_is_a_question_mark(self):
        # "?" rather than an empty meta segment: a missing model is a missing
        # fact, not zero models.
        for model in (None, "", "   "):
            self.assertEqual(labels.model_short(model), "?")


class TestDuration(unittest.TestCase):
    """The compact duration, including where it changes granularity.

    Four tiers: seconds below a minute; ``MmSSs``-style minutes below ten
    minutes; whole minutes below an hour; then ``HhMMm``. The 600-second edge is
    the surprising one — seconds are dropped there, not at 3600.
    """

    def test_seconds(self):
        self.assertEqual(labels.duration(0), "0s")
        self.assertEqual(labels.duration(38), "38s")
        self.assertEqual(labels.duration(59), "59s")

    def test_minutes_with_seconds_below_ten_minutes(self):
        self.assertEqual(labels.duration(60), "1m00s")
        self.assertEqual(labels.duration(134), "2m14s")
        self.assertEqual(labels.duration(599), "9m59s")

    def test_minutes_only_from_ten_minutes(self):
        # At ten minutes the seconds stop carrying information at this size.
        self.assertEqual(labels.duration(600), "10m")
        self.assertEqual(labels.duration(659), "10m")

    def test_hours(self):
        self.assertEqual(labels.duration(3900), "1h05m")

    def test_negative_is_clamped(self):
        # A clock skew must not print "-5s" as if it were a real duration.
        self.assertEqual(labels.duration(-5), "0s")

    def test_unknown_is_a_question_mark(self):
        self.assertEqual(labels.duration(None), "?")


class TestErrorText(unittest.TestCase):
    """Pulling the message out of a failed tool result's JSON framing.

    A reader needs the sentence; the JSON is already in the transcript file.
    Nothing here guesses: if no field can be found, the raw text is returned.
    """

    def test_named_error_field_wins(self):
        raw = 'tool ERROR 0.4s: {"output": "", "exit_code": 1, "error": "permission denied"}'
        self.assertEqual(labels.error_text(raw), "permission denied")

    def test_named_error_with_escaped_newline_becomes_one_line(self):
        self.assertEqual(labels.error_text('{"error": "line one\\nline two"}'),
                         "line one line two")

    def test_plain_output_payload_drops_the_json_framing(self):
        raw = 'terminal ERROR 5.0s: {"output": "FAILED (errors=1)", "exit_code": 1}'
        self.assertEqual(labels.error_text(raw), "FAILED (errors=1)")

    def test_escaped_newlines_in_output_are_flattened(self):
        raw = 'x ERROR 1.0s: {"output": "a\\nb", "exit_code": 2}'
        self.assertEqual(labels.error_text(raw), "a b")

    def test_raw_text_is_returned_as_is(self):
        self.assertEqual(labels.error_text("plain boom"), "plain boom")

    def test_none_is_empty(self):
        self.assertEqual(labels.error_text(None), "")

    def test_result_is_one_line(self):
        # The one invariant that matters for layout: a multi-line message would
        # break a row that is budgeted as a single line.
        raw = 'tool ERROR 0.4s: {"output": "", "error": "one\\ntwo\\nthree"}'
        self.assertNotIn("\n", labels.error_text(raw))


class TestTmuxEscape(unittest.TestCase):
    """Making text inert inside a tmux format string.

    ``#`` starts a format, ``,`` and ``}`` end a conditional branch. Escaping
    happens with tmux's own quoting, because the string is later parsed by tmux
    at draw time — shell quoting would not help.
    """

    def test_hash_is_doubled(self):
        self.assertEqual(labels.tmux_escape("#{pane_id}"), "##{pane_id#}")

    def test_comma_is_escaped(self):
        self.assertEqual(labels.tmux_escape("a,b"), "a#,b")

    def test_plain_text_is_unchanged(self):
        self.assertEqual(labels.tmux_escape("implement state.py"), "implement state.py")


class TestAsciiOnly(unittest.TestCase):
    """The single decision point for ASCII output.

    Precedence follows libc: ``HERMES_TMUX_ASCII`` wins outright in both
    directions, then the first set of ``LC_ALL``, ``LC_CTYPE``, ``LANG``. With
    nothing set the answer is UTF-8 — the optimistic default whose failure mode
    is cosmetic (a glyph instead of mojibake).
    """

    def test_default_locale_is_not_ascii_only(self):
        self.assertFalse(labels.ascii_only({}))

    def test_forced_on(self):
        for value in ("1", "true", "yes", "on", " TRUE "):
            self.assertTrue(labels.ascii_only({"HERMES_TMUX_ASCII": value}), value)

    def test_forced_off_beats_a_non_utf8_locale(self):
        # Both directions are honoured: an explicit "0" is not overridden by
        # LANG=C, which is how a user on a C locale asks for glyphs anyway.
        self.assertFalse(labels.ascii_only({"HERMES_TMUX_ASCII": "0", "LANG": "C"}))

    def test_non_utf8_locale_forces_ascii(self):
        self.assertTrue(labels.ascii_only({"LANG": "C"}))
        self.assertTrue(labels.ascii_only({"LC_ALL": "en_US.ISO-8859-1"}))

    def test_utf8_locale_is_allowed(self):
        # Both spellings of the charset are recognised.
        self.assertFalse(labels.ascii_only({"LC_ALL": "en_US.UTF-8"}))
        self.assertFalse(labels.ascii_only({"LANG": "en_US.utf8"}))

    def test_lc_all_wins_over_lang(self):
        # Only the first set variable is consulted, matching libc: a UTF-8
        # LANG under LC_ALL=C must still render ASCII.
        self.assertTrue(labels.ascii_only({"LC_ALL": "C", "LANG": "en_US.UTF-8"}))


class TestStateTable(unittest.TestCase):
    """The five-row table every surface reads a state's appearance from.

    Pinning the exact values (glyph, ASCII glyph, 256-colour index, word) is
    what keeps the tab, the pane header and the overview from disagreeing; the
    unknown-kind test is the guard that a sixth state cannot appear by accident.
    """

    def test_glyph_colour_and_word_for_every_kind(self):
        expected = {
            "run": ("\u25cf", "*", 114, "RUNNING"),
            "stall": ("\u25d0", "~", 214, "SILENT"),
            "done": ("\u2713", "+", 244, "DONE"),
            "fail": ("\u2717", "x", 203, "FAILED"),
            "other": ("\u00b7", ".", 244, "UNKNOWN"),
        }
        for kind, (glyph, ascii_glyph, colour, word) in expected.items():
            self.assertEqual(labels.glyph(kind), glyph, kind)
            self.assertEqual(labels.glyph(kind, True), ascii_glyph, kind)
            self.assertEqual(labels.colour(kind), colour, kind)
            self.assertEqual(labels.word(kind), word, kind)

    def test_unknown_kind_degrades_to_other(self):
        # Every accessor, not just ``glyph``: a new kind must not paint with a
        # colour or label from a neighbouring row.
        self.assertEqual(labels.glyph("bogus"), labels.glyph("other"))
        self.assertEqual(labels.colour("bogus"), labels.colour("other"))
        self.assertEqual(labels.word("bogus"), labels.word("other"))

    def test_ascii_glyphs_are_ascii(self):
        for kind in ("run", "stall", "done", "fail", "other"):
            self.assertTrue(all(ord(ch) < 128 for ch in labels.glyph(kind, True)))

    def test_state_kind_collapses_unknown_labels(self):
        # ``state_kind`` is the funnel: anything not one of the four real kinds
        # is "other", so no caller has to remember the table's keys.
        for label, kind in (("run", "run"), ("stall", "stall"), ("done", "done"),
                            ("fail", "fail"), ("weird", "other"), ("", "other")):
            self.assertEqual(labels.state_kind(label), kind, label)


class TestClean(unittest.TestCase):
    """One line of text: control characters and space runs collapsed."""

    def test_control_characters_and_runs_of_space_collapse(self):
        self.assertEqual(labels.clean("a\tb\nc   d"), "a b c d")

    def test_none_is_empty(self):
        self.assertEqual(labels.clean(None), "")

    def test_edges_are_stripped(self):
        self.assertEqual(labels.clean("  padded  "), "padded")


class TestBytesInput(unittest.TestCase):
    """Bytes are decoded, not ``repr``-ed.

    A goal read from a log or a manifest can arrive as bytes. Passing it through
    ``str()`` would print ``b'...'`` into the tab name, which is what this
    guards.
    """

    def test_bytes_goal_is_decoded_not_repr(self):
        from hermes_tmux import labels
        self.assertEqual(labels.title(b"bytes goal for a worker"), "bytes goal for a worker")
        self.assertEqual(labels.clean(b"a  b"), "a b")


if __name__ == "__main__":
    unittest.main()
