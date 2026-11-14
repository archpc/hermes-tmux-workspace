"""Regression tests for display-column width accounting (CJK / fullwidth /
combining characters) and hostile escape / control sanitization across
labels, render, transcript, and pane headers.
"""

from __future__ import annotations

import re
import unittest

from hermes_tmux import labels, render, state, transcript, watch


class UnicodeWidthAndEscapeRegressions(unittest.TestCase):
    def _plain_cols(self, line: str) -> int:
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
        fmt = transcript.Formatter(width=60, colour=False, ascii_mode=False)
        lines = fmt.feed("22:00:01 tool      | -> terminal(echo 终端双宽字符长命令行裁剪宽度测试验证数据流完整性检查报告)")
        self.assertEqual(len(lines), 1)
        self.assertLessEqual(labels.cell_width(lines[0]), 60)

    def test_combining_marks_count_zero_width_and_stay_attached(self) -> None:
        # 'e' + U+0301 COMBINING ACUTE ACCENT = 1 terminal cell, 2 code points
        composed = "cafe\u0301"
        self.assertEqual(labels.cell_width(composed), 4)
        self.assertEqual(labels.clip(composed, 4), composed)

    def test_hostile_osc52_dcs_csi_and_bidi_are_completely_defused(self) -> None:
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
        # With 10 CJK chars = 20 display cells (not 10), the narrowest threshold
        # in the conditional must reflect >= 20 title cells rather than 10.
        thresholds = [int(m) for m in re.findall(r"#\{pane_width\},(\d+)", hdr)]
        self.assertTrue(thresholds and min(thresholds) >= 48, thresholds)


if __name__ == "__main__":
    unittest.main()
