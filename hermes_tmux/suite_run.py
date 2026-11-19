"""Run the workspace regression through TestBench in the hx:tests pane.

One job: start `testbench run <repo> --profile regression --suite
full-tmux-regression --stream`, copy its output to the pane *and* to
`tests.log`, and report the exit status through a receipt file.

Contract with the caller (`cli._run_tests_in_aux`), all three via paths in the
receipt's directory — the caller never reads this process's stdout:

* `started` is touched once the runner actually begins. Absent after a short
  grace period, the caller stops waiting: a Python that never imported (bad
  interpreter, cwd, import error) must not hang the command for seven minutes.
* `exit` is written atomically (`<name>.tmp` then replace) with the decimal
  status, so a reader never sees a half-written number.
* `cancel` appearing at any time is treated as an interrupt, not a failure.

Signals: SIGINT, SIGHUP and SIGTERM are all converted into a SIGINT sent to
TestBench, because its Python unwinds its isolation cleanup on SIGINT and does
not on SIGHUP — the sandbox would leak a server. The reported status is
`128 + signum` so a caller can tell "the user cancelled" (130) from "the suite
failed" (1).

Neither `tests.log` nor the pane is the source of truth for the exit status;
the receipt is. `tests.log` is only what a human reads afterwards.
"""
from __future__ import annotations

import codecs
import os
import selectors
import shutil
import signal
import subprocess
import sys
from pathlib import Path


def testbench_binary() -> str:
    preferred = Path.home() / ".local/bin/testbench"
    binary = shutil.which(str(preferred)) or shutil.which("testbench")
    return str(Path(binary).absolute()) if binary else ""


def run(repo: Path, log_path: Path, binary: str, cancel: Path | None = None) -> int:
    """Stream one TestBench run; return its status (128+signal when interrupted).

    Output is written twice on purpose: the pane is the live view, the log is
    the record that survives the pane being closed. A write to the pane that
    fails (pane gone) is swallowed; the run continues and still waits for
    TestBench to clean up, because abandoning it would leave its sandbox
    behind. The log write is not guarded the same way — if `tests.log` cannot
    be written the whole thing is broken and the exception should surface.
    """
    proc = None
    interrupted = None

    def interrupt(signum, _frame):
        nonlocal interrupted
        if interrupted is None:
            interrupted = signum
            if proc is not None:
                # TestBench's Python unwinds its isolation cleanup on SIGINT.
                # Never send an unhandled HUP to a live TestBench coordinator.
                proc.send_signal(signal.SIGINT)

    signals = (signal.SIGINT, signal.SIGHUP, signal.SIGTERM)
    previous = {sig: signal.signal(sig, interrupt) for sig in signals}
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("w", encoding="utf-8") as log:
            def check_cancel() -> None:
                if cancel is not None and (cancel.exists() or not cancel.parent.is_dir()):
                    interrupt(signal.SIGINT, None)

            def show(text: str) -> None:
                log.write(text)
                log.flush()
                try:
                    print(text, end="", flush=True)
                except OSError:
                    pass  # the pane closed; still wait for TestBench to clean up

            try:
                if binary:
                    check_cancel()
                if not binary:
                    show("hermes-tmux: TestBench unavailable; no tests ran.\n")
                    rc = 127
                elif interrupted is None:
                    show("Running workspace regression in TestBench…\n")
                    proc = subprocess.Popen(
                        [binary, "run", str(repo), "--profile", "regression", "--suite", "full-tmux-regression", "--stream"],
                        cwd=repo, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        start_new_session=True,
                    )
                    if interrupted is not None:
                        proc.send_signal(signal.SIGINT)
                    assert proc.stdout is not None
                    decode = codecs.getincrementaldecoder("utf-8")("replace")
                    with proc.stdout, selectors.DefaultSelector() as ready:
                        ready.register(proc.stdout, selectors.EVENT_READ)
                        while ready.get_map():
                            check_cancel()
                            for key, _ in ready.select(0.1):
                                chunk = os.read(key.fd, 8192)
                                if chunk:
                                    show(decode.decode(chunk))
                                else:
                                    show(decode.decode(b"", final=True))
                                    ready.unregister(key.fd)
                    while proc.poll() is None:
                        check_cancel()
                        try:
                            proc.wait(timeout=0.1)
                        except subprocess.TimeoutExpired:
                            pass
                    rc = proc.wait()
                    if rc < 0:
                        rc = 128 - rc
                else:
                    rc = 1
            except OSError as exc:
                show(f"hermes-tmux: could not run TestBench: {exc}\n")
                rc = 1
            if interrupted is not None:
                rc = 128 + interrupted
            show(f"TestBench exit status: {rc}\n")
            return rc
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def main() -> int:
    """`python -m hermes_tmux.suite_run REPO LOG BINARY [RECEIPT]`.

    The receipt path is optional so the command can be run by hand; the pane
    launcher always passes one (see `cli._aux_command`, role "tests"), and the
    shell fallback there writes the status itself if this module never starts.
    """
    repo, log_path, binary, *receipt_arg = sys.argv[1:]
    rc = 1
    receipt = Path(receipt_arg[0]) if receipt_arg else None
    try:
        if receipt is not None and receipt.parent.is_dir():
            (receipt.parent / "started").touch()
        rc = run(Path(repo), Path(log_path), binary, receipt.parent / "cancel" if receipt else None)
    finally:
        if receipt is not None:
            pending = receipt.with_suffix(".tmp")
            try:
                pending.write_text(str(rc), encoding="ascii")
                pending.replace(receipt)
            except FileNotFoundError:
                if receipt.parent.is_dir():
                    raise  # caller is still here: do not hide a real receipt failure
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
