"""Exit-code wrapper for adoptable runs (``adopt_on_restart``).

``python -m tqdaemon.run_wrapper --log LOG --exit EXIT -- command args...``

Runs the task with stdout/stderr appended to LOG, waits for it and writes its exit code to EXIT
(atomically). A daemon restarted while the task ran cannot read the exit code of a process it did not
spawn; it reads EXIT instead (see tqdaemon.adopt). The wrapper ignores SIGTERM/SIGINT itself: tq
signals the whole process group, so the task gets the signal and the wrapper only outlives it long
enough to record how it ended.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path


def write_exit_code(path: Path, code: int) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(str(int(code)), encoding="ascii")
    os.replace(tmp, path)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        sep = args.index("--")
        opts, cmd = args[:sep], args[sep + 1 :]
        log = Path(opts[opts.index("--log") + 1])
        exit_file = Path(opts[opts.index("--exit") + 1])
    except (ValueError, IndexError):
        print("usage: python -m tqdaemon.run_wrapper --log LOG --exit EXIT -- command [args...]", file=sys.stderr)
        return 2
    if not cmd:
        return 2

    # A no-op handler, not SIG_IGN: ignored dispositions survive exec, so the task would ignore SIGTERM too.
    for name in ("SIGTERM", "SIGINT", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, lambda *_: None)
            except (OSError, ValueError):
                pass

    popen_kwargs: dict = {}
    if os.name == "nt":
        from tqdaemon.win_console import apply_hide_console_popen_kwargs

        popen_kwargs = apply_hide_console_popen_kwargs({})
    with open(log, "a", encoding="utf-8", errors="replace", buffering=1) as out:
        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT, **popen_kwargs)
        except OSError as exc:
            out.write(f"[tq-wrapper] could not start {cmd[0]!r}: {exc}\n")
            code = 127
        else:
            code = proc.wait()
    write_exit_code(exit_file, code)
    return code if 0 <= code < 256 else 1


if __name__ == "__main__":
    sys.exit(main())
