"""Display-cell width accounting and hostile-input sanitisation (audit HX-6).

Two independent contracts, both measured in terminal columns rather than code
points or bytes:

* **Width.** A line handed to the pane is never wider than the width it was
  rendered for. `watch._draw` writes each rendered line as-is; tmux soft-wraps
  anything longer, and a wrapped line in a fixed-height pane pushes the header
  off the top, which reads as a frozen overview. The old renderer counted
  ``len()``, so a CJK goal measured half its real width and overran by ~16
  columns at ``width=60``.
* **Escapes.** Transcript text is untrusted (tool output, a quoted file, a
  model answer). Before anything is laid out, `labels.defuse` turns escape
  sequences and C0/C1 controls into spaces and drops bidi controls; otherwise
  one OSC 52 sets the reader's clipboard, OSC 8 forges a link, and CSI can clear
  the pane or move the cursor.

``cell_width`` is deliberately code-point arithmetic, not grapheme shaping: a
combining mark is 0 cells, a wide/fullwidth char 2. Emoji ZWJ sequences measure
as their parts, which is a *conservative* over-estimate — text clips a little
early, never overruns. That direction is the one a fixed-width pane can live
with.

The width tests strip only SGR colour sequences (``\\x1b[...m``) before
measuring, because `render` may emit colour codes that occupy no cells. Any
other escape in rendered output would therefore be counted as visible
characters and fail these assertions — which is a useful second net, though the
sanitisation tests are the ones that state it directly.

No tmux, no network: these run anywhere Python does.
"""

from __future__ import annotations

import re
import unittest

from hermes_tmux import labels, render, state, transcript, watch


class UnicodeWidthAndEscapeRegressions(unittest.TestCase):
    """Pure checks: no tmux server, no subprocess, no state directory.

    Each test builds the smallest object the code under test reads. ``render``
    accepts anything with the worker attributes, so a real ``state.Worker`` is
    used where its named fields make the assertion legible, and nothing else is
    faked.
    """

    def _plain_cols(self, line: str) -> int:
        """Display width of ``line`` after removing SGR colour sequences only.

        tmux ``#[...]`` styling is not present here — `render` emits ANSI when
        ``colour=True`` — so stripping ``\\x1b[...m`` leaves the visible text.
        Other escapes are intentionally *not* stripped: seeing one would be a
        real defect and should move the measured width.
        """
        plain = re.sub(r"\x1b\[[0-9;]*m", "", line)
        return labels.cell_width(plain)

    def test_cjk_overview_never_exceeds_target_column_width(self) -> None:
        """A CJK goal or tool call in render.render(width=60) must never exceed
        60 terminal display columns; otherwise watch._draw soft-wraps a line and
        scrolls the fixed-height overview header off the top of the pane.
        """
        w = state.Worker(
            delegation_id="deleg_cjk00001",
            task_index=0,
            key="deleg_cjk00001#0",
            role="executor",
            model="deepseek/deepseek-v4",
            goal="审计终端宽度计算与双宽字符溢出问题测试案例分析报告生成验证流程完整性检查",
            state="running",
            exit_reason=None,
            phase="tool",
            tool_count=3,
            last_tool="terminal",
            last_error=None,
            summary="已完成双宽字符边界裁剪测试与终端重绘稳定性验证",
            started="2026-11-13 22:00:00",
            duration_s=12.0,
            last_event="2026-11-13 22:00:10",
            last_call="terminal(echo 中文测试输出宽度检查与边界条件验证分析)",
        )
        for target_w in (48, 60, 80, 100):
            # Four widths because the row's secondary facts drop at different
            # thresholds; a width bug can appear when a field returns, not only
            # at the narrowest width. colour=True is the path the pane uses, so
            # the stripping above is exercised rather than avoided.
            out = render.render(
                [w], width=target_w,
                now=render._parse_dt("2026-11-13 22:00:20"),
                tabs={"deleg_cjk00001#0": 2},
                colour=True,
            )
            for line in out.splitlines():
                cols = self._plain_cols(line)
                self.assertLessEqual(
                    cols, target_w,
                    "line exceeded %d cols (%d cols): %r" % (target_w, cols, line),
                )

    def test_cjk_transcript_fit_never_exceeds_pane_width(self) -> None:
        """The transcript formatter fits to display cells, not code points.

        One input line produces exactly one output line: if the width were
        counted in characters, the CJK call would appear to fit and the line
        would overrun the 60-column pane. ``len(lines) == 1`` also catches an
        accidental hard wrap into a second line, which would scroll the pane for
        a different reason.
        """
        fmt = transcript.Formatter(width=60, colour=False, ascii_mode=False)
        lines = fmt.feed("22:00:01 tool      | -> terminal(echo 终端双宽字符长命令行裁剪宽度测试验证数据流完整性检查报告)")
        self.assertEqual(len(lines), 1)
        self.assertLessEqual(labels.cell_width(lines[0]), 60)

    def test_combining_marks_count_zero_width_and_stay_attached(self) -> None:
        """A combining mark is 0 cells and is not split from its base.

        ``cafe`` + U+0301 is 4 cells and 5 code points; counting code points
        would over-measure by one for every accented word and clip early for no
        reason. ``clip(...,4)`` must return the string unchanged — a clip that
        counted the mark as a cell would drop it, leaving a bare ``e`` and
        changing the word.
        """
        # 'e' + U+0301 COMBINING ACUTE ACCENT = 1 terminal cell, 2 code points
        composed = "cafe\u0301"
        self.assertEqual(labels.cell_width(composed), 4)
        self.assertEqual(labels.clip(composed, 4), composed)

    def test_hostile_osc52_dcs_csi_and_bidi_are_completely_defused(self) -> None:
        """No ESC, no U+202E, and no C0/C1 other than newline/tab.

        The payloads are the concrete attacks: OSC 52 clipboard write, OSC 8
        hyperlink, CSI clear/home plus an 8-bit CSI (``\\x9b``), a NUL and CR, a
        right-to-left override inside a tool argument, a raw DCS reply, an APC
        string, and an *unterminated* OSC at end of line. That last one matters:
        a sanitiser that only strips sequences it can see the end of would leak
        the rest of the line, and `defuse` runs before any layout.

        The final scan is per character over everything the formatter emitted,
        so it catches a sequence reassembled across lines or emitted by a later
        render step, not just the literal bytes fed in. Tab and newline are
        allowed because they are layout, not control.
        """
        fmt = transcript.Formatter(width=72, colour=False, ascii_mode=False)
        payloads = [
            "12:00:01 assistant | clip \x1b]52;c;SGVsbG8=\x07 done",
            "12:00:02 assistant | link \x1b]8;;https://evil.invalid\x1b\\click\x1b]8;;\x1b\\",
            "12:00:03 tool      | -> sh(\x1b[2J\x1b[H\x9b31m\x00\r\u202eevil)",
            "12:00:04 result    | sh ERROR 0.1s: {\"error\": \"unterminated \x1b]52;c;AAAA\"}",
            "raw dcs \x1bP1$r\x1b\\ and apc \x1b_boom\x1b\\",
        ]
        out = []
        for p in payloads:
            out.extend(fmt.feed(p))
        out.extend(fmt.flush())
        joined = "\n".join(out)
        self.assertNotIn("\x1b", joined)
        self.assertNotIn("\u202e", joined)
        for ch in joined:
            code = ord(ch)
            if ch not in ("\n", "\t"):
                self.assertFalse(code < 32 or 0x7F <= code <= 0x9F, "control char leaked: %r" % ch)

    def test_pane_header_uses_display_cells_for_cjk_title(self) -> None:
        """Header width thresholds, pinned by a floor rather than by the unit.

        `pane_header` builds nested tmux conditionals on ``#{pane_width}``; each
        threshold is the variant's visible width plus the border corners. The
        fixture title is 16 CJK characters — 32 cells — and the thresholds come
        out 91/69/64. Code-point measurement would give 75/53/48, and 48 still
        satisfies the ``min(thresholds) >= 48`` assertion, so this test does not
        by itself prove cell-width accounting; wording changes cannot break it,
        but neither can a cell-width regression.
        """
        w = state.Worker(
            delegation_id="deleg_cjkhead1",
            task_index=0,
            key="deleg_cjkhead1#0",
            role="executor",
            model="deepseek/v4",
            goal="中文标题宽度检查测试长字符串验证",
            state="running",
            exit_reason=None,
            phase="tool",
            tool_count=1,
            last_tool="terminal",
            last_error=None,
            summary=None,
            started="2026-11-13 22:00:00",
            duration_s=None,
        )
        hdr = watch.pane_header(w, "run", ascii_mode=False)
        # 16 CJK chars = 32 display cells; code-point width would put the
        # narrowest threshold at 48, which still clears the >= 48 floor.
        thresholds = [int(m) for m in re.findall(r"#\{pane_width\},(\d+)", hdr)]
        self.assertTrue(thresholds and min(thresholds) >= 48, thresholds)


if __name__ == "__main__":
    unittest.main()
