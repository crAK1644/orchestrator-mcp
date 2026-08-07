"""Regenerate the advertised-schema snapshot: `uv run python -m tests.snapshot`.

Deliberately a separate command rather than an auto-update flag on the test. A
snapshot that rewrites itself when it fails is not a guard.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path

from orchestrator_mcp.consult.config import HOST_RUNTIME_ENV

from .test_existing_contract import SNAPSHOT, advertised, snapshot_config


def main() -> None:
    # Both of these come from fixtures under pytest. The runtime decides which agent
    # is excluded from its own routing; the managed path keeps a developer's real
    # `~/.orchestrator-mcp/agents.yaml` out of the snapshot they are regenerating.
    os.environ[HOST_RUNTIME_ENV] = "claude"
    with tempfile.TemporaryDirectory() as scratch:
        config = snapshot_config()
        config["consult"]["managed_agents_path"] = str(Path(scratch) / "agents.yaml")
        advertised_tools = asyncio.run(advertised(config))
    SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
    SNAPSHOT.write_text(json.dumps(advertised_tools, indent=2) + "\n")
    print(f"wrote {SNAPSHOT}")


if __name__ == "__main__":
    main()
