"""Readable live view of one worker transcript.

``hermes-tmux tail LOG`` prints the transcript that Hermes writes for a
subagent and keeps following it. It replaces a bare ``tail -F`` in worker
panes: the file on disk is still the only source, every event is shown in the
order it was written, and nothing is summarised or invented. What changes is
presentation: colour by event type, tool calls and results reduced to one
line, blank lines around what the worker said, and the two places where the
writer repeats itself (the ``think`` preview of an ``assistant`` message, the
``start`` copy of the goal) collapsed.

Each event is written as ONE terminal line and left to the terminal to wrap.
That is deliberate: tmux re-wraps soft-wrapped lines when a pane is resized,
so a transcript stays readable across full, half and quarter-screen clients
without this process redrawing anything, and the reader's scroll position is
never disturbed. Only the one-line entries are fitted to the pane, at the
width it has when they are printed.

Long entries are clipped with an explicit ``(+N chars)`` marker and the path of
the full transcript is printed first, so a reader always knows there is more
and where it is. Lines that are not events are shown as written.
``HERMES_TMUX_TRANSCRIPT=raw`` restores plain ``tail -F``.
"""

from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

from . import labels

_EVENT_RE = re.compile(
    r"^(?P<ts>\d{2}:\d{2}:\d{2})\s+(?P<role>[A-Za-z_][A-Za-z0-9_]*)\s*\|\s?(?P<text>.*)$"
)
_RESULT_RE = re.compile(r"^(?P<tool>\S+)\s+(?P<status>ok|ERROR)\s+(?P<dur>[\d.]+s)?:?\s*(?P<body>.*)$")
_PAYLOAD_PREFIX = re.compile(r'^\{"[A-Za-z_]+":\s*"?')
_FINAL_KV = re.compile(r"(\w+)=(\S+)")
_HEADER_RE = re.compile(
    r"^(=+( Hermes subagent live transcript =+)?|delegation: .*|goal: .*|started: .*"
    r"|\(append-only; .*)\s*$")
_POLL_S = 1.0
_PANE_CHECK_S = 20.0
#: Columns left free at the end of a fitted line. A pane is often created a
#: column or two wider than the client that later shows it; without the margin
#: every fitted line then wraps a single character onto a line of its own.
_MARGIN = 2

#: Characters shown per event type before clipping. What the worker said is
#: the substance of a transcript and gets room; a tool call or result is
#: evidence that something happened and gets one line (fitted to the pane).
_MAX_CHARS = {"assistant": 4000, "think": 240, "goal": 700, "context": 320,
              "error": 480, "other": 480}


class Style:
    """ANSI styling that degrades to nothing."""

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    def __call__(self, text: str, *codes: str) -> str:
        if not self.enabled or not codes or not text:
            return text
        return "\033[%sm%s\033[0m" % (";".join(codes), text)

    @staticmethod
    def fg(index: int) -> str:
        return "38;5;%d" % index


DIM, BOLD, ITALIC = "2", "1", "3"


def colour_enabled(environ=None, stream=None) -> bool:
    env = os.environ if environ is None else environ
    if env.get("NO_COLOR"):
        return False
    if (env.get("TERM") or "") == "dumb":
        return False
    stream = sys.stdout if stream is None else stream
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError):
        return False


def _limit(text: str, limit: int, ellipsis: str) -> str:
    """Clip to ``limit`` characters with an explicit count of what is hidden."""
    if limit <= 0 or len(text) <= limit:
        return text
    return "%s %s(+%d chars)" % (text[:limit].rstrip(), ellipsis, len(text) - limit)


def _payload_preview(body: str, ascii_mode: bool) -> str:
    """One readable line from a tool result payload.

    Results are written as a JSON object cut off mid-string, so they cannot be
    parsed; the common ``{"output": "`` opener is dropped and escaped newlines
    become a visible break mark instead of a literal backslash-n.
    """
    text = _PAYLOAD_PREFIX.sub("", body.strip(), count=1)
    mark = " / " if ascii_mode else " \u23ce "
    text = text.replace("\\r\\n", mark).replace("\\n", mark).replace("\\r", " ")
    text = text.replace('\\"', '"').replace("\\t", " ")
    return labels.clean(text)


class Formatter:
    """Turns transcript lines into display lines. Holds one line of lookahead.

    The writer emits a ``think`` line that is the first 80 characters of the
    ``assistant`` line that follows it. Showing both doubles the transcript, so
    a ``think`` is held until the next line shows whether it was a preview.
    """

    def __init__(self, width: int = 100, colour: bool = False,
                 ascii_mode: bool = False) -> None:
        self.width = max(20, int(width))
        self.style = Style(colour)
        self.ascii = ascii_mode
        self.ellipsis = "..." if ascii_mode else "\u2026"
        self._held: Optional[Dict[str, str]] = None
        self._goal_shown = False
        self._final_status = ""
        self._seen_event = False

    # -- public -------------------------------------------------------------

    def feed(self, line: str) -> List[str]:
        line = line.rstrip("\r\n")
        # Transcript text is untrusted (tool output, quoted files, a model's
        # answer). Nothing below is safe on a raw ESC/OSC byte: one OSC 52 in a
        # line would set the reader's clipboard, and CSI can clear the pane.
        line = labels.defuse(line)
        match = _EVENT_RE.match(line)
        if match is None:
            out = self.flush()
            # The fixed file header (title, ids, goal, rule) repeats what the
            # pane header and the kickoff event already say. Any OTHER line
            # that is not an event is shown as written: a malformed or foreign
            # line is evidence, and hiding it would make the view lie by
            # omission.
            if not self._seen_event and _HEADER_RE.match(line):
                return out
            if line.strip():
                out.append(self._line("", "", line.strip(), "other", DIM))
            return out
        self._seen_event = True
        event = {"ts": match.group("ts"), "role": match.group("role").lower(),
                 "text": match.group("text").strip()}
        out: List[str] = []
        if self._held is not None:
            held, self._held = self._held, None
            preview = held["text"].rstrip(".\u2026 ")
            if not (event["role"] == "assistant" and event["text"].startswith(preview)):
                out.extend(self._render(held))
        if event["role"] == "think":
            self._held = event
            return out
        out.extend(self._render(event))
        return out

    def flush(self) -> List[str]:
        """Emit a held ``think`` line (nothing followed it in time)."""
        if self._held is None:
            return []
        held, self._held = self._held, None
        return self._render(held)

    # -- rendering ----------------------------------------------------------

    def _fit(self, text: str, room: int) -> str:
        """Clip to ``room`` columns. Never wider, and never a half-written marker.

        The count of hidden characters is shown when it fits whole; in a pane
        too narrow for that, a bare ellipsis says the entry was clipped.
        """
        room = max(0, room)
        if len(text) <= room:
            return text
        tail = " %s(+%d chars)" % (self.ellipsis, len(text) - room)
        if room >= len(tail) + 4:
            return text[:room - len(tail)].rstrip() + tail
        if room <= len(self.ellipsis):
            return self.ellipsis        # clipping is always marked, even with no room
        return text[:room - len(self.ellipsis)].rstrip() + self.ellipsis

    def _line(self, ts: str, marker: str, text: str, kind: str, *codes: str,
              marker_codes: Optional[List[str]] = None, fit: bool = False) -> str:
        """One event as one terminal line: dim time, marker, text."""
        s = self.style
        lead = (ts + "  ") if ts else ""
        if fit:
            text = self._fit(text, self.width - _MARGIN - len(lead) - len(marker))
        else:
            text = _limit(text, _MAX_CHARS.get(kind, 480), self.ellipsis)
        return (s(lead, DIM) + s(marker, *(marker_codes or list(codes)))
                + s(text, *codes))

    def _render(self, event: Dict[str, str]) -> List[str]:
        s = self.style
        role, ts, text = event["role"], event["ts"], event["text"]
        bullet = "> " if self.ascii else "\u25b8 "

        if role == "user":
            return self._render_kickoff(ts, text)
        if role == "start":
            return [] if self._goal_shown else [self._line(ts, "TASK  ", text, "goal", BOLD), ""]
        if role == "assistant":
            return ["", self._line(ts, "", text, "assistant"), ""]
        if role == "think":
            return [self._line(ts, "", text, "think", DIM, ITALIC)]
        if role == "tool":
            call = text[3:].strip() if text.startswith("-> ") else text
            name, paren, rest = call.partition("(")
            lead = len(ts) + 2 + len(bullet)
            args = self._fit(paren + rest, self.width - _MARGIN - lead - len(name))
            return [s(ts + "  ", DIM) + s(bullet, Style.fg(39))
                    + s(name, BOLD, Style.fg(39)) + args]
        if role == "result":
            return self._render_result(ts, text)
        if role == "final":
            return self._render_final(ts, text)
        return [self._line(ts, role + "  ", text, "other", DIM)]

    def _render_kickoff(self, ts: str, text: str) -> List[str]:
        body = text[len("kickoff:"):].strip() if text.startswith("kickoff:") else text
        goal, _, context = body.partition(" | context: ")
        self._goal_shown = True
        out = [self._line(ts, "TASK  ", goal.strip(), "goal", BOLD)]
        if context.strip():
            out.append(self._line("", "context: ", context.strip(), "context", DIM))
        return out + [""]

    def _render_result(self, ts: str, text: str) -> List[str]:
        match = _RESULT_RE.match(text)
        if match is None:
            return [self._line(ts, "  ", text, "other", DIM, fit=True)]
        failed = match.group("status") == "ERROR"
        dur = match.group("dur") or ""
        preview = _payload_preview(match.group("body"), self.ascii)
        if failed:
            # An error is the one result worth reading in full: it wraps. When
            # the payload names its error, that message is the line; the JSON
            # around it is in the transcript file.
            preview = labels.error_text(match.group("body"))
            mark = "x " if self.ascii else "\u2717 "
            head = "%sERROR %s  " % (mark, dur)
            return [self._line(ts, head, preview, "error", Style.fg(203),
                               marker_codes=[BOLD, Style.fg(203)])]
        head = "  ok %s  " % dur if dur else "  ok  "
        return [self._line(ts, head, preview, "other", DIM, fit=True)]

    def _render_final(self, ts: str, text: str) -> List[str]:
        s = self.style
        fields = dict(_FINAL_KV.findall(text.split("summary:", 1)[0]))
        status = fields.get("status", "")
        if text.startswith("end "):
            reason = fields.get("exit_reason", "")
            # The closing line repeats the status; only an exit reason that
            # differs from it (interrupted, max_iterations) is news.
            if not reason or reason == status:
                return []
            return [s(ts + "  ", DIM) + s("exit: %s" % reason, BOLD, Style.fg(214))]
        self._final_status = status
        ok = status in ("completed", "done", "success")
        kind = "done" if ok else "fail"
        dur = fields.get("duration", "")
        try:
            dur = labels.duration(float(dur.rstrip("s"))) if dur else ""
        except ValueError:
            pass
        head = "%s %s" % (labels.glyph(kind, self.ascii), status or "finished")
        if dur:
            head += " in %s" % dur
        return ["", s(ts + "  ", DIM) + s(head, BOLD, Style.fg(114 if ok else 203))]


# --------------------------------------------------------------------------- follow

def _pane_width(default: int = 100) -> int:
    try:
        return max(20, os.get_terminal_size(sys.stdout.fileno()).columns)
    except (OSError, ValueError, AttributeError):
        return default


def quiet_tty():
    """Stop the terminal echoing typed keys; returns a function that undoes it.

    A worker pane is a view, not a prompt. The same command can be run by hand
    in a reader's own shell, so the caller must restore the terminal on the way
    out: a shell left without echo looks broken.
    """
    try:
        import termios
        fd = sys.stdin.fileno()
        saved = termios.tcgetattr(fd)
        attrs = termios.tcgetattr(fd)
        attrs[3] &= ~termios.ECHO
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
    except Exception:
        return lambda: None

    def restore() -> None:
        try:
            termios.tcsetattr(fd, termios.TCSANOW, saved)
        except Exception:
            pass

    return restore


def _emit(lines: List[str]) -> bool:
    if not lines:
        return True
    try:
        sys.stdout.write("\n".join(lines) + "\n")
        sys.stdout.flush()
        return True
    except (OSError, ValueError):
        return False              # the pane is gone


def run_tail(path: str, follow: bool = True) -> int:
    """Print the transcript at ``path`` readably and keep following it."""
    colour = colour_enabled()
    ascii_mode = labels.ascii_only()
    fmt = Formatter(_pane_width(), colour, ascii_mode)
    style = Style(colour)
    # No cursor and no echo while following: the pane is a view, and a
    # blinking block invites typing. Both are put back on the way out, because
    # this command also runs in a reader's own shell.
    restore = lambda: None
    if colour and follow:
        sys.stdout.write("\033[?25l")
        restore = quiet_tty()
    if not _emit([style("transcript: %s" % labels.defuse(path), DIM), ""]):
        return 0

    pane_id = os.environ.get("TMUX_PANE", "")
    # The kernel reports appends, so a new line shows at once; the timeout only
    # bounds the pane-liveness check and the lookahead flush.
    from .watch import WatchSet
    watcher = WatchSet()
    offset = 0
    pending = b""
    ident = None          # (st_dev, st_ino) of the file being followed
    last_pane_check = time.time()
    try:
        while True:
            try:
                stat = os.stat(path)
                size, current = stat.st_size, (stat.st_dev, stat.st_ino)
            except OSError:
                size, current = -1, None
            # Rotation is not only truncation: a replacement file that is
            # larger than the old offset used to be read from the middle, so
            # the pane showed half a line and silently skipped the beginning.
            if ident is not None and current is not None and current != ident:
                # A held ``think`` belongs to the old file: show it rather than
                # dropping it, then start the new file with fresh state.
                _emit(fmt.flush())
                offset, pending = 0, b""
                fmt = Formatter(fmt.width, colour, ascii_mode)
            if current is not None:
                ident = current
            if 0 <= size < offset:
                offset, pending = 0, b""        # truncated: start over
            if size > offset:
                try:
                    with open(path, "rb") as handle:
                        handle.seek(offset)
                        chunk = handle.read()
                except OSError:
                    # The file can be stat'd but not read (permissions, a
                    # directory path, a transient replacement). Do not spin:
                    # wait for the next change and try again.
                    if not follow:
                        _emit(fmt.flush())
                        return 0
                    watcher.sync([], [Path(path)])
                    watcher.wait(_POLL_S)
                    continue
                offset += len(chunk)
                pending += chunk
                *complete, pending = pending.split(b"\n")
                # One-line entries are fitted to the pane as it is now; text
                # already printed is the terminal's to re-wrap.
                fmt.width = _pane_width(fmt.width)
                out: List[str] = []
                for raw in complete:
                    out.extend(fmt.feed(raw.decode("utf-8", "replace")))
                if not _emit(out):
                    return 0
                continue
            if not follow:
                # A one-shot read shows everything on disk, including a last
                # line the writer has not terminated yet.
                tail = fmt.feed(pending.decode("utf-8", "replace")) if pending.strip() else []
                _emit(tail + fmt.flush())
                return 0
            watcher.sync([], [path])
            # Nothing new within one wait: a held line was not a preview.
            if not watcher.wait(_POLL_S) and not _emit(fmt.flush()):
                return 0
            now = time.time()
            if pane_id and now - last_pane_check >= _PANE_CHECK_S:
                last_pane_check = now
                from . import tmuxio
                # A server killed outright does not always hang up its panes'
                # processes; without this the follower would outlive its pane.
                if not tmuxio.pane_exists(pane_id):
                    return 0
    except KeyboardInterrupt:
        return 0
    finally:
        watcher.close()
        restore()
        if colour and follow:
            try:
                sys.stdout.write("\033[?25h")
                sys.stdout.flush()
            except (OSError, ValueError):
                pass
