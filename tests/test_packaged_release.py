"""Chromium acceptance against an unpacked source release, never Vite."""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from zipfile import ZipFile

import pytest
from playwright.sync_api import sync_playwright

from scripts.verify_release import verify
from tests.test_e2e_browser import _seed

ROOT = Path(__file__).resolve().parent.parent
# The acceptance target is `run_monitor.py serve`, which refuses production
# mode below Python 3.12 by design — the packaged flows cannot run on the
# 3.9 CI leg at all.
pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(sys.version_info < (3, 12),
                       reason="production serve requires Python 3.12+"),
]


def _port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _json(port: int, route: str, payload=None) -> dict:
    body = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(f"http://127.0.0.1:{port}{route}", data=body,
                                     headers={"Content-Type": "application/json"} if body else {})
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.load(response)


def _browse_packaged_routes(port: int, routes: tuple[str, ...], *, exact_event: bool = False) -> None:
    # The other browser suite keeps a synchronous Playwright event loop open
    # for its session. This independent thread makes combined pytest safe.
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            for route in routes:
                response = page.goto(f"http://127.0.0.1:{port}{route}", wait_until="networkidle")
                assert response.status == 200
                assert "Monitoring features require the local server." not in page.locator("body").inner_text()
            if exact_event:
                target = page.locator("#event-3")
                assert target.is_visible()
                assert "change-row-target" in (target.get_attribute("class") or "")
        finally:
            browser.close()


@pytest.fixture(scope="module")
def release_app(tmp_path_factory):
    target = tmp_path_factory.mktemp("packaged_release")
    subprocess.run([sys.executable, "scripts/build_release.py", "--allow-dirty",
                    "--output", str(target)], cwd=ROOT, check=True, stdout=subprocess.DEVNULL)
    archive = next(target.glob("*.zip"))
    verify(archive)
    app = target / "app"
    app.mkdir()
    with ZipFile(archive) as release:
        release.extractall(app)
    assert not list(app.rglob("*.db"))
    assert not (app / "web/dist/data").exists()
    return app


@pytest.fixture
def start_release(release_app, tmp_path):
    processes = []

    def start(*, existing=False):
        data_dir = tmp_path / ("existing" if existing else "fresh")
        db_path = data_dir / "db/ct_monitor.db"
        if existing:
            db_path.parent.mkdir(parents=True)
            from config import CONFIG
            from db.connection import close_connection
            prior = CONFIG.db.path
            try:
                _seed(db_path)
            finally:
                close_connection()
                CONFIG.db.path = prior
        env = {**os.environ, "CT_DATA_DIR": str(data_dir), "CT_MODE": "production",
               "CT_PORT": str(_port())}
        port = int(env["CT_PORT"])
        process = subprocess.Popen([sys.executable, "run_monitor.py", "serve"],
                                   cwd=release_app, env=env, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=True)
        processes.append(process)
        for _ in range(80):
            if process.poll() is not None:
                raise RuntimeError(process.stdout.read())
            try:
                if _json(port, "/api/ready")["status"] == "ready":
                    return port, data_dir, env
            except Exception:
                time.sleep(.2)
        raise RuntimeError("packaged server did not become ready")

    yield start
    for process in processes:
        if process.poll() is None:
            process.terminate()
            process.communicate(timeout=5)


def test_fresh_package_initializes_empty_database_and_spa(release_app, start_release):
    port, data_dir, env = start_release()
    assert _json(port, "/api/health")["status"] == "ok"
    assert _json(port, "/api/trials/search", None)["total"] == 0
    subprocess.run([sys.executable, "run_monitor.py", "doctor"], cwd=release_app,
                   env=env, check=True, stdout=subprocess.DEVNULL)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(_browse_packaged_routes, port, ("/", "/projects", "/data-sources")).result()
    assert (data_dir / "db/ct_monitor.db").is_file()


def test_existing_v19_package_projects_and_exact_event_link(release_app, start_release):
    port, data_dir, env = start_release(existing=True)
    saved_id = _json(port, "/api/saved-searches", {"name": "Packaged search",
                       "state": {"q": "heart failure"}})["saved_search"]["id"]
    project_id = _json(port, "/api/projects", {"name": "Packaged project"})["project"]["id"]
    _json(port, f"/api/projects/{project_id}/assets/searches", {"id": saved_id})
    _json(port, f"/api/projects/{project_id}/assets/monitors", {"id": 1})
    _json(port, f"/api/projects/{project_id}/notes", {"body": "Packaged note"})
    _json(port, f"/api/projects/{project_id}/evidence", {"event_id": 3, "note": "Packaged evidence"})
    detail = _json(port, f"/api/projects/{project_id}")["project"]
    assert detail["counts"]["notes"] == detail["counts"]["evidence"] == 1
    assert detail["counts"]["saved_searches"] == detail["counts"]["monitors"] == 1
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(_browse_packaged_routes, port,
                    ("/projects", "/saved-searches", "/monitors", "/updates",
                     "/trials/NCT/NCT00000001?tab=changes&event=3"), exact_event=True).result()
    assert list((data_dir / "db").glob("*.db"))
