"""Release identity shared by CLI, API, and package tooling."""

import json
from pathlib import Path

APP_VERSION = "1.10.0"
SCHEMA_VERSION = 20


def build_id() -> str:
    """Return packaged commit metadata without requiring Git at runtime."""
    manifest_path = Path(__file__).resolve().parent.parent / "release-manifest.json"
    if not manifest_path.is_file():
        return "source"
    try:
        manifest = json.loads(manifest_path.read_text())
        commit = str(manifest.get("commit") or "unknown")[:12]
        return commit + ("-dirty" if manifest.get("dirty") else "")
    except (OSError, ValueError, TypeError):
        return "unknown"
