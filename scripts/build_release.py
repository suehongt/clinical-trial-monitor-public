"""Build a source distribution with production SPA assets and no local data."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from core.version import APP_VERSION, SCHEMA_VERSION  # noqa: E402

RUNTIME_DIRS = ("collectors", "core", "ct_report", "db", "server")
RUNTIME_FILES = ("config.py", "run_monitor.py", "requirements.txt", "requirements.lock", "README.md", ".env.example")
RUNTIME_SCRIPTS = ("export_report_json.py", "quality_report.py", "trial_report.py", "verify_release.py")
REQUIRED = {"config.py", "run_monitor.py", "requirements.txt", "web/dist/index.html",
            "core/version.py", "server/app.py", "release-manifest.json", ".env.example", "requirements.lock",
            "docs/INSTALL.md", "docs/OPERATIONS.md",
            "deploy/DEPLOY.md", "deploy/install_systemd.sh"}


def _files() -> dict[str, bytes]:
    entries: dict[str, bytes] = {}
    for name in RUNTIME_FILES:
        path = ROOT / name
        if path.is_file():
            entries[name] = path.read_bytes()
    for dirname in RUNTIME_DIRS:
        for path in (ROOT / dirname).rglob("*"):
            if path.is_file() and path.suffix in {".py", ".json", ".txt", ".yaml", ".yml"} and "__pycache__" not in path.parts:
                entries[path.relative_to(ROOT).as_posix()] = path.read_bytes()
    for name in RUNTIME_SCRIPTS:
        path = ROOT / "scripts" / name
        entries[path.relative_to(ROOT).as_posix()] = path.read_bytes()
    for path in (ROOT / "web" / "dist").rglob("*"):
        if path.is_file() and "data" not in path.relative_to(ROOT / "web" / "dist").parts and path.name != ".DS_Store":
            entries[path.relative_to(ROOT).as_posix()] = path.read_bytes()
    # Ship only what a fresh install needs to deploy and run. Development
    # process records (CHANGELOG, RELEASE_CHECKLIST, UPGRADE) stay in the
    # repository, out of the artifact.
    for name in ("INSTALL.md", "OPERATIONS.md"):
        path = ROOT / "docs" / name
        if path.is_file():
            entries["docs/" + name] = path.read_bytes()
    # Linux deploy kit: shell/config/docs ship as-is, but never the real
    # deploy/ct-monitor.env (credentials; gitignored, may exist on disk).
    for path in (ROOT / "deploy").rglob("*"):
        if (path.is_file() and path.name != "ct-monitor.env"
                and "__pycache__" not in path.parts):
            entries[path.relative_to(ROOT).as_posix()] = path.read_bytes()
    return entries


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "release")
    parser.add_argument("--allow-dirty", action="store_true", help="Build a development snapshot from an uncommitted tree")
    args = parser.parse_args()
    dirty = bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT).strip())
    if dirty and not args.allow_dirty:
        parser.error("working tree is dirty; commit the release baseline or pass --allow-dirty for a non-release snapshot")
    subprocess.run(["npm", "ci"], cwd=ROOT / "web", check=True)
    subprocess.run(["npm", "run", "build"], cwd=ROOT / "web", check=True)
    entries = _files()
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    commit_epoch = subprocess.check_output(["git", "show", "-s", "--format=%ct", "HEAD"], cwd=ROOT, text=True).strip()
    source_epoch = int(os.environ.get("SOURCE_DATE_EPOCH", commit_epoch))
    built_at = datetime.fromtimestamp(source_epoch, timezone.utc).isoformat()
    manifest = {"app_version": APP_VERSION, "schema_version": SCHEMA_VERSION,
                "commit": commit, "built_at": built_at, "platform": "source",
                "dirty": dirty, "files": {name: hashlib.sha256(data).hexdigest()
                                           for name, data in sorted(entries.items())}}
    entries["release-manifest.json"] = (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode()
    if not REQUIRED <= entries.keys():
        raise RuntimeError(f"missing release files: {sorted(REQUIRED - entries.keys())}")
    args.output.mkdir(parents=True, exist_ok=True)
    path = args.output / f"clinical-trial-monitor-{APP_VERSION}{'-dirty' if dirty else ''}.zip"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, data in sorted(entries.items()):
            info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            info.external_attr = 0o644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, data, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    path.with_suffix(path.suffix + ".sha256").write_text(f"{digest}  {path.name}\n")
    print(path)
    print(f"SHA-256 {digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
