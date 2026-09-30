"""Installs a fake `copilot` executable on PATH.

It refuses the way the real one does about who may run what, because the adapter's
readiness check is built on exactly that: signed out is exit 1 with "No authentication
information found" on stderr, and an unknown model is exit 1 with `Model "X" from
--model flag is not available` -- the sign-in check winning, as it does there. Anything
past those two is scripted, one entry of `runs` for each call that got that far and the
last one repeating.

It records argv, stdin, the working directory, the environment as the child actually
received it, what sat in that directory, the mode of `COPILOT_HOME` and what its
`settings.json` said when the call started. The working directory is one the adapter
deletes on the way out, so this is the only place a test can see it. Like the real CLI it
creates `<COPILOT_HOME>/session-state/<id>`, and a lock beside it, for any signed-in run
that names a session -- one it goes on to refuse over the model included -- which is what
a later `resume` looks for.

A script cannot know which session id the adapter will pass, and the real CLI echoes it
back in its last event, so `__SESSION__` in a scripted stdout becomes `--session-id`.
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
stdin = "" if sys.stdin.isatty() else sys.stdin.read()
home = Path(os.environ["COPILOT_HOME"]) if os.environ.get("COPILOT_HOME") else None


def flag(name):
    return args[args.index(name) + 1] if name in args else None


model, session = flag("--model"), flag("--session-id")
previous = sorted(RECORD.glob("call-*.json"))

run = None
if not SPEC["signed_in"]:
    run = {{"returncode": 1, "stderr": "Error: No authentication information found.\\n"}}
elif model not in SPEC["models"]:
    run = {{
        "returncode": 1,
        "stderr": 'Error: Model "%s" from --model flag is not available.\\n' % model,
    }}
refused = run is not None
if not refused:
    served = len([c for c in previous if json.loads(c.read_text())["served"]])
    run = SPEC["runs"][min(served, len(SPEC["runs"]) - 1)]

(RECORD / ("call-%03d.json" % len(previous))).write_text(
    json.dumps({{
        "argv": args,
        "stdin": stdin,
        "cwd": os.getcwd(),
        "env": dict(os.environ),
        "cwd_entries": sorted(os.listdir(".")),
        "home_mode": (home.stat().st_mode & 0o777) if home and home.is_dir() else None,
        "settings": (home / "settings.json").read_text() if home and (home / "settings.json").is_file() else None,
        "session_known": bool(home and session and (home / "session-state" / session).is_dir()),
        "served": not refused,
    }})
)

code = run.get("returncode", 0)
# The session, and a lock beside it, exist before the model is looked at, so a run the
# CLI then refuses over the model leaves both. Signed out is the case nobody observed.
if SPEC["signed_in"] and home and session:
    (home / "session-state" / session).mkdir(parents=True, exist_ok=True)
    locks = home / "session-state" / ".session-operation-locks"
    locks.mkdir(exist_ok=True)
    (locks / (session + ".lock")).touch()
report, usage = flag("--usage-output-file"), run.get("usage")
if report and usage is not None and code == 0:
    Path(report).write_text(usage if isinstance(usage, str) else json.dumps(usage))

sys.stdout.write(run.get("stdout", "").replace("__SESSION__", session or ""))
sys.stdout.flush()
sys.stderr.write(run.get("stderr", ""))
sys.stderr.flush()
if run.get("sleep"):
    time.sleep(run["sleep"])
sys.exit(code)
'''


def install(
    tmp_path: Path,
    monkeypatch,
    *,
    signed_in: bool = True,
    models: tuple[str, ...] = ("auto",),
    runs: list[dict] | None = None,
) -> Path:
    """Put `copilot` on PATH and return the directory its calls are recorded in."""
    bindir = tmp_path / "bin"
    record = tmp_path / "calls"
    bindir.mkdir(exist_ok=True)
    record.mkdir(exist_ok=True)

    spec = {"signed_in": signed_in, "models": list(models), "runs": runs or [{}]}
    executable = bindir / "copilot"
    executable.write_text(STUB.format(spec=json.dumps(spec), record=str(record)))
    executable.chmod(0o755)

    # Prepended, not replacing: the stub's own `#!/usr/bin/env python3` needs a real
    # interpreter to be findable, and the child gets this same PATH.
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    return record


def calls(record: Path) -> list[dict]:
    return [json.loads(p.read_text()) for p in sorted(record.glob("call-*.json"))]
