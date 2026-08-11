#!/usr/bin/env python3
"""Check whether the tracked rconsole-plugin ref moved.

Exit codes: 0 = current, 2 = update available, 1 = check failed.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "upstream" / "manifest.json"


def main() -> int:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    repository = manifest["repository"]
    ref = manifest.get("ref", "HEAD")
    tracked = manifest["commit"]

    try:
        proc = subprocess.run(
            ["git", "ls-remote", repository, ref],
            check=True,
            capture_output=True,
            text=True,
            timeout=45,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"upstream check failed: {exc}", file=sys.stderr)
        return 1

    line = next((item for item in proc.stdout.splitlines() if item.strip()), "")
    remote = line.split(maxsplit=1)[0] if line else ""
    if not remote:
        print(f"upstream ref not found: {ref}", file=sys.stderr)
        return 1

    print(f"tracked: {tracked}")
    print(f"remote:  {remote}")
    if remote == tracked:
        print("status: current")
        return 0

    print("status: update available")
    print("follow docs/UPSTREAM_SYNC.md before updating the manifest")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
