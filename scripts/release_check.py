"""Local release gate. Run against a copy or controlled single-node database."""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    parser = argparse.ArgumentParser(description="Production and package release checks")
    parser.add_argument("--db", help="Database to audit; defaults to a disposable clean database")
    parser.add_argument("--browser", action="store_true", help="Include Chromium E2E suite")
    args = parser.parse_args()
    disposable = tempfile.TemporaryDirectory(prefix="ctm-release-check-")
    audit_db = args.db
    if audit_db is None:
        env = {**os.environ, "CT_DATA_DIR": disposable.name}
        init = subprocess.run([sys.executable, "run_monitor.py", "init"], cwd=ROOT,
                              env=env, check=False, stdout=subprocess.DEVNULL)
        if init.returncode:
            disposable.cleanup()
            return init.returncode
        audit_db = str(Path(disposable.name) / "db/ct_monitor.db")
    commands = [
        (["npm", "ci"], ROOT / "web"),
        (["npm", "run", "build"], ROOT / "web"),
        ([sys.executable, "-m", "pytest", "-q", "-m", "not e2e"], ROOT),
        (["npm", "test"], ROOT / "web"),
        ([sys.executable, "-m", "pyflakes", "core", "db", "server", "run_monitor.py",
          "scripts/release_check.py", "scripts/performance_smoke.py",
          "scripts/build_release.py", "scripts/verify_release.py"], ROOT),
        (["git", "diff", "--check"], ROOT),
        ([sys.executable, "run_monitor.py", "integrity-check", "--json", "--db", audit_db], ROOT),
    ]
    if args.browser:
        commands.insert(3, ([sys.executable, "-m", "pytest", "-q", "-m", "e2e",
                             "tests/test_e2e_browser.py"], ROOT))
        commands.insert(4, ([sys.executable, "-m", "pytest", "-q", "-m", "e2e",
                             "tests/test_packaged_release.py"], ROOT))
        commands.insert(5, ([sys.executable, "-m", "pytest", "-q"], ROOT))
    try:
        for command, cwd in commands:
            print(f"\n$ {' '.join(command)}", flush=True)
            result = subprocess.run(command, cwd=cwd, check=False)
            if result.returncode:
                print(f"Release check failed with exit {result.returncode}", file=sys.stderr)
                return result.returncode
        print("Release checks passed")
        return 0
    finally:
        disposable.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
