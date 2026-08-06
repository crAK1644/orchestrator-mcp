"""Installs a fake `codex` / `claude` executable on PATH.

The adapters resolve their command through `shutil.which`, so the only honest way
to test them is to put a real executable there and let them build and run their own
argv. The stub records what it was called with and replays recorded output --
neither CLI is installed on the machine this was written on.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

STUB = '''#!/usr/bin/env python3
import json, os, sys, time
from pathlib import Path

SPEC = json.loads({spec!r})
RECORD = Path({record!r})

args = sys.argv[1:]
auth = "auth" in args or ("login" in args and "status" in args)
previous = sorted(RECORD.glob("call-*.json"))
index = len([c for c in previous if not json.loads(c.read_text())["auth"]])

stdin = "" if sys.stdin.isatty() else sys.stdin.read()
(RECORD / ("call-%03d.json" % len(previous))).write_text(
    # The environment as the child actually received it, so a test can assert what
    # did *not* travel -- which is not observable from this process.
    json.dumps({{"argv": args, "stdin": stdin, "auth": auth, "env": dict(os.environ)}})
)

if auth:
    run = SPEC["auth"]
else:
    runs = SPEC["runs"]
    run = runs[index] if index < len(runs) else runs[-1]

# Files the real CLI writes as a side effect of running -- its session log, which is
# where the Codex adapter reads back which model actually answered.
for target, text in run.get("append", {{}}).items():
    Path(target).parent.mkdir(parents=True, exist_ok=True)
    with open(target, "a") as handle:
        handle.write(text)

sys.stdout.write(run.get("stdout", ""))
sys.stdout.flush()
sys.stderr.write(run.get("stderr", ""))
sys.stderr.flush()
if run.get("sleep"):
    time.sleep(run["sleep"])
sys.exit(run.get("returncode", 0))
'''


def install(name: str, tmp_path: Path, monkeypatch, *, auth=None, runs=None) -> Path:
    """Put `name` on PATH and return the directory its calls are recorded in."""
    bindir = tmp_path / "bin"
    record = tmp_path / "calls"
    bindir.mkdir(exist_ok=True)
    record.mkdir(exist_ok=True)

    spec = {"auth": auth or {"stdout": '{"loggedIn": true}'}, "runs": runs or [{}]}
    executable = bindir / name
    executable.write_text(STUB.format(spec=json.dumps(spec), record=str(record)))
    executable.chmod(0o755)

    # Prepended, not replacing: the stub's own `#!/usr/bin/env python3` needs a real
    # interpreter to be findable, and the child gets this same PATH.
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    return record


def calls(record: Path, *, auth: bool = False) -> list[dict]:
    return [
        call
        for call in (json.loads(p.read_text()) for p in sorted(record.glob("call-*.json")))
        if call["auth"] is auth
    ]
