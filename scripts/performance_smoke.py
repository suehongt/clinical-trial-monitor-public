"""Disposable synthetic SQLite load and endpoint timings; never touches user data."""
from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
import time
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trials", type=int, default=50000)
    args = parser.parse_args()
    if not 1000 <= args.trials <= 100000:
        parser.error("--trials must be 1000–100000")
    from config import CONFIG
    from db.connection import close_connection, get_connection
    from db.schema import create_schema
    from fastapi.testclient import TestClient
    from server.app import create_app

    old_path = CONFIG.db.path
    with tempfile.TemporaryDirectory(prefix="ct-perf-") as directory:
        db = Path(directory) / "synthetic.db"
        close_connection()
        CONFIG.db.path = db
        try:
            create_schema()
            conn = get_connection()
            source = conn.execute("SELECT source_id FROM registry_sources WHERE short_name='NCT'").fetchone()[0]
            batch = []
            for index in range(args.trials):
                batch.append((source, f"NCT{index:08d}",
                              f"Heart failure synthetic trial {index}" if index % 10 == 0 else f"Synthetic study {index}",
                              '["Heart Failure"]' if index % 10 == 0 else '["Other"]',
                              "2026-09-01", "2026-09-01"))
                if len(batch) == 1000:
                    conn.executemany("""INSERT INTO registry_records
                        (source_id,source_trial_id,title,conditions,last_updated_at_source,first_crawled_at)
                        VALUES (?,?,?,?,?,?)""", batch)
                    conn.commit()
                    batch.clear()
            if batch:
                conn.executemany("""INSERT INTO registry_records
                    (source_id,source_trial_id,title,conditions,last_updated_at_source,first_crawled_at)
                    VALUES (?,?,?,?,?,?)""", batch)
                conn.commit()
            project = conn.execute("INSERT INTO research_projects(name) VALUES ('Synthetic performance')").lastrowid
            conn.execute("""INSERT INTO project_trials(project_id,source,trial_id)
                SELECT ?,'NCT',source_trial_id FROM registry_records WHERE record_id<=1000""", (project,))
            rows = conn.execute("SELECT record_id FROM registry_records WHERE record_id<=500").fetchall()
            conn.executemany("""INSERT INTO trial_events(record_id,field_name,old_value,new_value,event_hash,detected_at,severity)
                VALUES (?,'enrollment','10','20',?,'2026-09-01 00:00:00','normal')""",
                [(row[0], hashlib.sha256(str(row[0]).encode()).hexdigest()) for row in rows])
            conn.commit()
            close_connection()
            app = create_app(str(db))
            paths = {
                "Search": "/api/trials/search?q=heart&page=1&page_size=25",
                "Dashboard": "/api/dashboard?window=30d",
                "Updates": "/api/updates?scope=all&window=30d&page=1",
                "Project Activity": f"/api/updates?scope=project&project_id={project}&window=30d&page=1",
                "Briefing": "/api/intelligence/briefing?scope=all&window=30d",
            }
            results = {}
            with TestClient(app) as client:
                for name, path in paths.items():
                    start = time.perf_counter()
                    response = client.get(path, timeout=120)
                    elapsed = round(time.perf_counter() - start, 3)
                    results[name] = {"seconds": elapsed, "status": response.status_code,
                                     "bytes": len(response.content)}
            print(json.dumps({"trials": args.trials, "events": len(rows),
                              "project_trials": 1000, "results": results}, indent=2))
        finally:
            close_connection()
            CONFIG.db.path = old_path


if __name__ == "__main__":
    main()
