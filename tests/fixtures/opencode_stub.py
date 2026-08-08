"""Installs a fake `opencode` executable on PATH.

Separate from `agent_stub` because this runtime is not one command with one output:
a single consultation runs `debug config`, then `run`, then `export`, and each has to
answer differently. Sequencing those by call index -- which is all `agent_stub` can
do -- would silently reorder the moment the adapter stops making one of them.

The stub records argv, stdin, the working directory, and the environment as the child
actually received it, which is the only place a test can see what did *not* travel.
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
command = " ".join(args[:2]) if args[:1] == ["debug"] else (args[0] if args else "")

previous = sorted(RECORD.glob("call-*.json"))
stdin = "" if sys.stdin.isatty() else sys.stdin.read()


def read(path):
    # The working directory is a temporary one the adapter deletes on the way out, so
    # anything a test wants to say about the configuration has to be captured here.
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return None


(RECORD / ("call-%03d.json" % len(previous))).write_text(
    json.dumps({{
        "argv": args,
        "stdin": stdin,
        "cwd": os.getcwd(),
        "env": dict(os.environ),
        "project_config": read("opencode.json"),
        "env_config": read(os.environ.get("OPENCODE_CONFIG", "")),
        "cwd_entries": sorted(os.listdir(".")),
        "xdg_entries": sorted(os.listdir(os.environ["XDG_CONFIG_HOME"]))
        if os.environ.get("XDG_CONFIG_HOME") else None,
    }})
)

if command == "run":
    # `run` is the only command that can happen twice in one consultation -- the
    # repair turn -- so it alone is sequenced.
    runs = SPEC["run"]
    index = len([c for c in previous if json.loads(c.read_text())["argv"][:1] == ["run"]])
    run = runs[index] if index < len(runs) else runs[-1]
else:
    run = SPEC.get(command, {{}})

sys.stdout.write(run.get("stdout", ""))
sys.stdout.flush()
sys.stderr.write(run.get("stderr", ""))
sys.stderr.flush()
if run.get("sleep"):
    time.sleep(run["sleep"])
sys.exit(run.get("returncode", 0))
'''


def install(
    tmp_path: Path,
    monkeypatch,
    *,
    models: str = "",
    config: str = "{}",
    run: list[dict] | None = None,
    export: dict | None = None,
) -> Path:
    """Put `opencode` on PATH and return the directory its calls are recorded in."""
    bindir = tmp_path / "bin"
    record = tmp_path / "calls"
    bindir.mkdir(exist_ok=True)
    record.mkdir(exist_ok=True)

    spec = {
        "models": {"stdout": models},
        "debug config": {"stdout": config},
        "run": run or [{}],
        "export": export if export is not None else {"stdout": "{}"},
    }
    executable = bindir / "opencode"
    executable.write_text(STUB.format(spec=json.dumps(spec), record=str(record)))
    executable.chmod(0o755)

    # Prepended, not replacing: the stub's own `#!/usr/bin/env python3` needs a real
    # interpreter to be findable, and the child gets this same PATH.
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    return record


def calls(record: Path, command: str | None = None) -> list[dict]:
    found = [json.loads(p.read_text()) for p in sorted(record.glob("call-*.json"))]
    if command is None:
        return found
    return [c for c in found if " ".join(c["argv"]).startswith(command)]
