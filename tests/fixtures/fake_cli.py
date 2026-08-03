#!/usr/bin/env python3
"""A stand-in for a consulted CLI, so the transport can be tested with none installed.

Mode is `argv[1]`. Everything here is deliberately dumb: the point is to be a real
OS process with real stdin, a real exit code and a real process group, not to
imitate Codex or Claude -- that is what the recorded fixtures in phase 5 are for.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "echo"

    if mode == "echo":
        sys.stdout.write(sys.stdin.read())
        return 0

    if mode == "argv":
        json.dump(sys.argv[2:], sys.stdout)
        return 0

    if mode == "env":
        json.dump(dict(os.environ), sys.stdout)
        return 0

    if mode == "cwd":
        sys.stdout.write(os.getcwd())
        return 0

    if mode == "fail":
        sys.stderr.write("fake cli refused\n")
        return 3

    if mode == "hang":
        time.sleep(300)
        return 0

    if mode == "stubborn":
        # Ignores SIGTERM and leaves a grandchild behind: only killing the whole
        # process group gets rid of both.
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        grandchild = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
        with open(sys.argv[2], "w") as handle:
            handle.write(f"{os.getpid()} {grandchild.pid}")
        time.sleep(300)
        return 0

    sys.stderr.write(f"unknown mode {mode!r}\n")
    return 2


if __name__ == "__main__":
    sys.exit(main())
