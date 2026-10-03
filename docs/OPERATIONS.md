# Operations

Use `venv/bin/python run_monitor.py <command> --help` for arguments.

The canonical foreground process is `serve`. It binds localhost in production
and serves `web/dist` without Vite. `/api/health` is liveness; `/api/ready`
checks database startup requirements. `version`, `status`, `doctor`,
`diagnostics`, and `integrity-check` are operator queries. `doctor` exits 1
for a missing database, unsupported schema, bad integrity, unwritable paths,
missing frontend assets or invalid SMTP config. Source freshness is visible in
`status` and Data Sources; source staleness does not fail readiness.

With `CT_DATA_DIR` set, paths are `<data-dir>/db/ct_monitor.db`,
`<data-dir>/backups/`, `<data-dir>/logs/monitor.log` (rotated at 50 MiB,
three older files), and `<data-dir>/data/raw/`. `CT_DB_PATH` overrides only
the database file. `CT_HOST` defaults to 127.0.0.1 and production refuses a
non-loopback bind. `CT_PORT` defaults to 8000. `CT_MODE` defaults to
development for `python -m server`, but `run_monitor.py serve` selects
production by default. `.env.example` lists supported deployment settings;
shell/source/service configuration must provide them because the program does
not load `.env` files.

Run `backup --out <directory> --keep 7` manually or from one local scheduler.
`restore --input <file> --offline` requires every server, collector and job
to be stopped. The `--offline` flag is your explicit assertion. Restore makes
a safety backup and verifies the source before replacing the DB. Run
`integrity-check --json` afterward. Email has at-least-once provider semantics;
an uncertain send after a crash requires manual review.
