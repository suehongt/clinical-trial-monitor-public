"""Verify release checksum, manifest, contents, and data/secret exclusions."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import zipfile
from pathlib import Path

REQUIRED = {"config.py", "run_monitor.py", "requirements.txt", "web/dist/index.html",
            "core/version.py", "server/app.py", "release-manifest.json", ".env.example", "requirements.lock",
            "docs/INSTALL.md", "docs/OPERATIONS.md"}

# Development process records stay in the repository, out of the artifact.
EXCLUDED_DOCS = {"docs/CHANGELOG.md", "docs/RELEASE_CHECKLIST.md", "docs/UPGRADE.md"}


def verify(path: Path) -> dict:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    checksum_path = path.with_suffix(path.suffix + ".sha256")
    expected, filename = checksum_path.read_text().strip().split(maxsplit=1)
    if expected != digest or filename != path.name:
        raise ValueError("artifact checksum mismatch")
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)) or not REQUIRED <= set(names):
            raise ValueError("duplicate or missing required artifact files")
        if EXCLUDED_DOCS & set(names):
            raise ValueError("development process records must not ship in the artifact")
        for name in names:
            parts = Path(name).parts
            if (name.startswith("/") or ".." in parts or parts[0] in {"data", "backups", "logs"}
                    or name.startswith("web/dist/data/")
                    or any(p in {".git", ".env", "node_modules", "venv", "__pycache__"} for p in parts)
                    or name.endswith((".db", ".sqlite", ".sqlite3", ".pyc"))):
                raise ValueError(f"forbidden artifact entry: {name}")
        manifest = json.loads(archive.read("release-manifest.json"))
        version_source = archive.read("core/version.py")
        app_match = re.search(rb'^APP_VERSION = "([^"]+)"$', version_source, re.MULTILINE)
        schema_match = re.search(rb'^SCHEMA_VERSION = (\d+)$', version_source, re.MULTILINE)
        if (not app_match or not schema_match or
                manifest["app_version"] != app_match.group(1).decode() or
                manifest["schema_version"] != int(schema_match.group(1))):
            raise ValueError("release version does not match packaged version source")
        if set(manifest["files"]) != set(names) - {"release-manifest.json"}:
            raise ValueError("manifest file set mismatch")
        for name, expected in manifest["files"].items():
            if hashlib.sha256(archive.read(name)).hexdigest() != expected:
                raise ValueError(f"content checksum mismatch: {name}")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifact", type=Path)
    args = parser.parse_args()
    manifest = verify(args.artifact)
    print(f"Verified {args.artifact}: {manifest['app_version']} schema v{manifest['schema_version']} dirty={manifest['dirty']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
