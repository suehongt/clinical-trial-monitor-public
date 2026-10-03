# Install (single trusted user, one macOS host)

Requires Python 3.12 or newer, Node/npm for building from source, and a
persistent writable data directory. The reference deployment target is
macOS; Linux server deployment is covered by `deploy/DEPLOY.md`. Run every
command from the unpacked application directory — a fresh shell/PowerShell
starts in the user home folder, where `requirements.txt` does not exist.
On Windows (PowerShell) the same steps apply with `venv\Scripts\` instead
of `venv/bin/` (`python -m venv venv`, `venv\Scripts\pip install -r
requirements.lock`, `venv\Scripts\python run_monitor.py init|doctor|serve`);
scheduled jobs must be registered in Task Scheduler instead of launchd, and
`scripts/crawl_waf_batch.py` requires POSIX file locking (macOS/Linux only).
The release zip already contains built frontend assets, so Node is not
needed to run it.

1. Verify the zip with `shasum -a 256 -c <artifact>.zip.sha256` and, from a
   source checkout, `python scripts/verify_release.py <artifact>.zip`.
   Inspect its manifest. Unpack it to an
   application directory. The archive contains no database, backup or `.env`.
2. In the unpacked directory, run `python3 -m venv venv` and
   `venv/bin/python -m pip install -r requirements.lock`. The lock was
   resolved on macOS with Python 3.12; use `requirements.txt` only when
   intentionally refreshing dependency versions.
3. Choose a persistent data directory outside the unpacked application:
   `export CT_DATA_DIR="$HOME/Library/Application Support/Clinical Trial Monitor"`.
   Set `CT_MODE=production`; optionally set `CT_PORT` (default 8000).
4. Run `venv/bin/python run_monitor.py init`, then
   `venv/bin/python run_monitor.py doctor`.
5. Run `venv/bin/python run_monitor.py serve`. Open `http://127.0.0.1:8000/`.

`serve` runs in the foreground. Stop it with Ctrl-C, or use a local service
manager. It owns the HTTP API and built SPA. Scheduled ingestion, monitor
execution and email retry are **external CLI jobs**; `serve` does not spawn
workers. Configure only one scheduler owner for those jobs.
`scripts/install_launchd.sh` installs the reference macOS launchd set: the
daily pipeline, the Saturday ICTRP reconciliation and a 15-minute
`tick-monitors` scheduler tick that runs due topic monitors. Fresh startup
creates schema v19 and lookup rows, with no trial fixtures.

Existing users can point `CT_DB_PATH` to an existing v18 or v19 DB. A v18
database is backed up and migrated transactionally to v19 at startup. Set
`CT_DATA_DIR` as well for logs and backups. No existing database is silently
moved. Do not run another writer during upgrades or restore.
