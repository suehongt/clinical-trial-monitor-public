"""server — Read-only FastAPI layer over the local registry database.

Web platform (Phase 7 P1): the browser SPA queries this API, which SELECTs
the pre-crawled SQLite database — clicking never triggers a registry crawl
(WAF red line: the API layer is strictly read-only over crawled data).

Run:  venv/bin/python run_monitor.py serve
"""
