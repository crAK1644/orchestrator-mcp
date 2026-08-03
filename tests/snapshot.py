"""Regenerate the advertised-schema snapshot: `uv run python -m tests.snapshot`.

Deliberately a separate command rather than an auto-update flag on the test. A
snapshot that rewrites itself when it fails is not a guard.
"""

from __future__ import annotations

import asyncio
import json

from .test_existing_contract import SNAPSHOT, advertised, snapshot_config


def main() -> None:
    SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
    SNAPSHOT.write_text(json.dumps(asyncio.run(advertised(snapshot_config())), indent=2) + "\n")
    print(f"wrote {SNAPSHOT}")


if __name__ == "__main__":
    main()
