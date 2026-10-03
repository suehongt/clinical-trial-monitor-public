"""Browser E2E tests (Phase 3E) — real Chromium, real backend, real frontend.

These tests are the acceptance layer the backend unit/contract tests cannot
provide: they launch the FastAPI server over a seeded fixture database AND
the built React SPA (served by that same server, production style), then
drive a headless Chromium through the product journeys.

Mandatory regression (#35 of the Phase 3E brief): the text
"Monitoring features require the local server." must NEVER appear under a
normal full-stack startup.

Infrastructure (session fixtures):
  - ``web_build``  → `npm run build` once (set CT_SKIP_WEB_BUILD=1 to reuse
    an existing web/dist, e.g. when iterating on tests only);
  - ``e2e_server`` → per-test seeded temp DB + uvicorn on a free port;
  - ``browser``    → one Playwright chromium instance for the session.

Run:  venv/bin/python -m pytest tests/test_e2e_browser.py -m e2e -q
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import sqlite3
import subprocess
import threading
import time
from functools import lru_cache
from pathlib import Path

import pytest

from playwright.sync_api import expect  # noqa: E402  (importorskip'd via browser fixture)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WEB_DIST = PROJECT_ROOT / "web" / "dist"
FALLBACK_TEXT = "Monitoring features require the local server."

# Dashboard / briefing / updates filter on a rolling 7-day window, so the
# fixture's "recent" activity must float with the clock: a hardcoded batch
# (2026-09-16) aged out of the window and started failing days later.
_RECENT = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=2)
RECENT_TS = _RECENT.strftime("%Y-%m-%d %H:%M:%S")
RECENT_DAY = RECENT_TS[:10]
SEEN_TS = (dt.datetime.now(dt.timezone.utc)
           - dt.timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")

pytestmark = [pytest.mark.e2e]


# ── fixture database seeding ───────────────────────────────────────────────


def _seed(db_path: Path) -> None:  # noqa: C901 - straight-line inserts
    """Seed the exact world the E2E journeys expect (all times recent)."""
    from config import CONFIG
    from db.connection import close_connection, get_connection
    from db.schema import create_schema

    CONFIG.db.path = db_path
    close_connection()
    create_schema()
    conn = get_connection()

    def insert_record(trial_id, title, *, status="Recruiting", enrollment=120,
                      first_crawled="2026-01-10 08:00:00", version=1, latest=1,
                      superseded_by=None, primary="Overall survival at 12 months",
                      source="NCT", conditions=None, sponsors=None,
                      countries=None, phase="Phase 2", sci=None):
        src = conn.execute(
            "SELECT source_id FROM registry_sources WHERE short_name=?",
            (source,)).fetchone()[0]
        cur = conn.execute(
            """INSERT INTO registry_records
               (source_id, source_trial_id, title, scientific_title, status_id,
                study_phase, enrollment, conditions, sponsors, countries, locations,
                interventions, primary_endpoint, secondary_endpoints,
                registration_date, start_date, completion_date, last_updated_at_source,
                source_url, first_crawled_at, last_crawled_at, version_number,
                is_latest, superseded_by)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (src, trial_id, title, sci or f"{title} (scientific)", None, phase,
             enrollment,
             json.dumps(conditions if conditions is not None else ["Heart Failure"]),
             json.dumps(sponsors if sponsors is not None else ["Acme Medical"]),
             json.dumps(countries if countries is not None else ["China", "United States"]),
             json.dumps([json.dumps({"facility": "Beijing Heart Hospital", "city": "Beijing",
                                     "state": "", "country": "China"}),
                         json.dumps({"facility": "Shanghai General", "city": "Shanghai",
                                     "state": "", "country": "China"})]),
             json.dumps(["Device: LVAD"]), primary,
             json.dumps(["Quality of life at 6 months"]),
             "2026-01-05", "2026-02-01", "2026-12-31", "2026-09-01",
             f"https://clinicaltrials.gov/study/{trial_id}",
             first_crawled, first_crawled, version, latest, superseded_by))
        rid = cur.lastrowid
        sid = conn.execute(
            "SELECT status_type_id FROM status_types WHERE label=?", (status,)).fetchone()
        if sid is not None:
            conn.execute("UPDATE registry_records SET status_id=? WHERE record_id=?",
                         (sid[0], rid))
        return rid

    # Trial 1: rich change history (status + enrollment + endpoint), 3 versions
    v1 = insert_record("NCT00000001", "Heart failure device trial in adults",
                       status="Not yet recruiting", enrollment=80,
                       first_crawled="2026-01-10 08:00:00", version=1, latest=0)
    v2 = insert_record("NCT00000001", "Heart failure device trial in adults",
                       status="Recruiting", enrollment=120,
                       first_crawled="2026-01-10 08:00:00", version=2, latest=0,
                       superseded_by=None)
    conn.execute("UPDATE registry_records SET superseded_by=? WHERE record_id=?", (v2, v1))
    v3 = insert_record("NCT00000001", "Heart failure device trial in adults",
                       status="Recruiting", enrollment=148, version=3, latest=1)
    conn.execute("UPDATE registry_records SET superseded_by=? WHERE record_id=?", (v3, v2))

    def insert_event(record_id, field, old, new, category, severity,
                     detected="2026-09-10 08:00:00"):
        event_hash = hashlib.sha256(
            f"{record_id}:{field}:{old}:{new}:{detected}".encode()).hexdigest()
        conn.execute(
            """INSERT INTO trial_events
               (record_id, field_name, old_value, new_value, change_category,
                change_type, severity, importance_score, event_hash, detected_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (record_id, field, old, new, category, "modified", severity,
             {"critical": 90, "important": 60, "normal": 30, "minor": 10}[severity],
             event_hash, detected))

    # v1→v2 (detected on v2), v2→v3 (detected on v3)
    insert_event(v2, "status_id", "Not yet recruiting", "Recruiting", "status", "important",
                 detected="2026-07-03 09:30:00")
    insert_event(v2, "enrollment", "80", "120", "size", "important",
                 detected="2026-07-03 09:30:00")
    insert_event(v3, "enrollment", "120", "148", "size", "normal",
                 detected=RECENT_TS)
    insert_event(v3, "primary_endpoint", "Overall survival at 12 months",
                 "Overall survival at 24 months", "endpoint", "critical",
                 detected=RECENT_TS)

    # Trial 2: quiet trial for watching from scratch
    insert_record("NCT00000002", "Myocardial infarction registry study",
                  status="Completed", enrollment=50)

    # Trial 3: fresh registration this week (dashboard "new trials")
    insert_record("NCT00000003", "Cardiac rehabilitation digital coach study",
                  first_crawled=time.strftime("%Y-%m-%d 06:00:00", time.gmtime()))

    # Phase 3E.1: myocarditis discovery corpus spanning two registries.
    # 4 trials match "myocarditis"; 3 of them Recruiting; 2 of those Phase 2.
    insert_record("NCT09000301", "Myocarditis after CAR-T therapy trial",
                  status="Recruiting", phase="Phase 2", conditions=["Myocarditis"],
                  sponsors=["Acme Biotech"], countries=["United States"],
                  sci="CAR-T therapy in myocarditis")
    insert_record("NCT09000302", "Myocarditis imaging biomarker study",
                  status="Recruiting", phase="Phase 3", conditions=["Myocarditis"],
                  sponsors=["Imaging Corp"], countries=["United States", "China"])
    insert_record("NCT09000309", "Myocarditis rehabilitation study",
                  status="Completed", phase="Phase 2", conditions=["Myocarditis"],
                  sponsors=["Rehab Org"], countries=["United States"])
    insert_record("ChiCTR2200060001", "Acute myocarditis registry study",
                  status="Recruiting", phase="Phase 2", source="ChiCTR",
                  conditions=["Myocarditis"], sponsors=["Beijing Hospital"],
                  countries=["China"], sci="急性心肌炎注册研究")

    # Watch trial 1 (with seen marker before the last change → unseen count 2)
    cur = conn.execute(
        "INSERT INTO trial_watches (trial_id, enabled, last_change_seen_at) VALUES (?, 1, ?)",
        ("NCT00000001", SEEN_TS))
    watch_id = cur.lastrowid
    from server.app import WATCH_ALL_GROUPS, WATCH_DEFAULTS
    for group in WATCH_ALL_GROUPS:
        conn.execute(
            """INSERT INTO watch_preferences (trial_watch_id, field_group, enabled, minimum_severity)
               VALUES (?,?,?, 'normal')""",
            (watch_id, group, int(group in WATCH_DEFAULTS)))

    # Topic monitor with trial 1 in population + one changed event
    mid = conn.execute(
        """INSERT INTO monitors (name, description, enabled, schedule_enabled,
                                 schedule_frequency, schedule_timezone)
           VALUES ('Heart failure devices', 'fixture', 1, 1, 'daily', 'UTC')""").lastrowid
    conn.execute("INSERT INTO monitor_rules (monitor_id, rules_json) VALUES (?,?)",
                 (mid, json.dumps({"condition": "heart failure"})))
    conn.execute(
        """INSERT INTO monitor_trials (monitor_id, trial_id, currently_matches,
                                       first_matched_at, last_matched_at)
           VALUES (?, 'NCT00000001', 1, '2026-06-01 00:00:00', ?)""",
        (mid, RECENT_TS))
    changed_event_id = conn.execute(
        "SELECT event_id FROM trial_events WHERE field_name='enrollment' AND new_value='148'"
    ).fetchone()[0]
    me = conn.execute(
        """INSERT INTO monitor_events (monitor_id, trial_id, event_type,
                                       related_change_event_id, detected_at)
           VALUES (?, 'NCT00000001', 'trial_changed', ?, ?)""",
        (mid, changed_event_id, RECENT_TS)).lastrowid
    conn.execute(
        f"""INSERT INTO monitor_runs (monitor_id, started_at, completed_at, status,
                                     matched_count, new_count, changed_count, left_count, trigger)
           VALUES (?, '{RECENT_TS}', '{RECENT_TS}', 'completed', 1, 0, 1, 0, 'manual')""",
        (mid,))
    conn.execute(
        f"UPDATE monitors SET last_checked_at='{RECENT_TS}' WHERE id=?", (mid,))

    # One unread in-app notification for the changed event
    conn.execute(
            """INSERT INTO notifications (monitor_id, monitor_event_id, trial_id, event_type,
                                          channel, status, summary, monitor_name_snapshot,
                                          trial_title_snapshot, created_at)
               VALUES (?, ?, 'NCT00000001', 'trial_changed', 'in_app', 'delivered',
                       'Enrollment changed: 120 → 148', 'Heart failure devices',
                       'Heart failure device trial in adults', ?)""",
            (mid, me, RECENT_TS))

    conn.commit()
    conn.close()


# ── session fixtures ───────────────────────────────────────────────────────


@lru_cache(maxsize=1)
def _ensure_web_build() -> None:
    if WEB_DIST.joinpath("index.html").exists() and __import__("os").environ.get("CT_SKIP_WEB_BUILD"):
        return
    subprocess.run(["npm", "run", "build"], cwd=PROJECT_ROOT / "web", check=True,
                   capture_output=True, text=True)


@pytest.fixture(scope="session")
def browser():
    _ensure_web_build()
    pytest.importorskip("playwright")
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        holder = {"browser": p.chromium.launch(headless=True)}

        def _relaunch():
            try:
                holder["browser"].close()
            except Exception:
                pass
            holder["browser"] = p.chromium.launch(headless=True)
            return holder["browser"]

        def _transient(exc) -> bool:
            # CI runners occasionally kill Chromium mid-session; the next
            # context/page call then dies with a disposed-response error.
            text = str(exc)
            return ("has been disposed" in text or "Target closed" in text
                    or "has been closed" in text)

        class _Context:
            def __init__(self, kwargs, context):
                self._kwargs, self._context = kwargs, context

            def new_page(self, **kwargs):
                try:
                    return self._context.new_page(**kwargs)
                except Exception as exc:
                    if not _transient(exc):
                        raise
                    time.sleep(0.5)
                    if not holder["browser"].is_connected():
                        self._context = _relaunch().new_context(**self._kwargs)
                    return self._context.new_page(**kwargs)

            def __getattr__(self, name):
                return getattr(self._context, name)

        class _Browser:
            def new_context(self, **kwargs):
                try:
                    return _Context(kwargs, holder["browser"].new_context(**kwargs))
                except Exception as exc:
                    if not _transient(exc):
                        raise
                    time.sleep(0.5)
                    if not holder["browser"].is_connected():
                        _relaunch()
                    return _Context(kwargs, holder["browser"].new_context(**kwargs))

            def __getattr__(self, name):
                return getattr(holder["browser"], name)

        yield _Browser()
        holder["browser"].close()


@pytest.fixture()
def production_server(tmp_path, monkeypatch):
    """Production mode serves the built SPA over a disposable v19 database."""
    import uvicorn
    from server.app import create_app

    db_path = tmp_path / "production.db"
    _seed(db_path)
    monkeypatch.setenv("CT_MODE", "production")
    app = create_app(db_path=str(db_path))
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    assert server.started
    port = server.servers[0].sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)


@pytest.fixture()
def e2e_server(tmp_path):
    """Seeded temp DB + uvicorn serving API + SPA on a free port."""
    import uvicorn

    from server.app import create_app

    db_path = tmp_path / "e2e.db"
    _seed(db_path)

    app = create_app(db_path=str(db_path))
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    assert server.started, "uvicorn did not start"
    port = server.servers[0].sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)


@pytest.fixture()
def page(browser, e2e_server):
    from playwright.sync_api import expect

    context = browser.new_context(viewport={"width": 1440, "height": 900},
                                  base_url=e2e_server)
    pg = context.new_page()
    # 30s: shared CI runners are routinely slower than a dev machine; 15s
    # produced spurious interaction timeouts on the 3.9 leg.
    pg.set_default_timeout(30_000)
    expect.set_options(timeout=30_000)
    yield pg
    context.close()


# ── helpers ────────────────────────────────────────────────────────────────


def _no_fallback_text(pg) -> None:
    body = pg.locator("body").inner_text()
    assert FALLBACK_TEXT not in body, (
        f"fallback text appeared at {pg.url} — monitoring backend not detected")


# ── the journeys ───────────────────────────────────────────────────────────


class TestBackendConnection:
    """Mandatory regression: monitoring pages load from the real backend."""

    def test_monitoring_pages_show_no_fallback(self, page):
        for path in ("/monitors", "/updates?view=notifications", "/trials?view=watched"):
            page.goto(path)
            page.wait_for_load_state("domcontentloaded")
            expect(page.get_by_role("heading", name="Dashboard").or_(
                page.locator("h1").first)).to_be_visible()
            _no_fallback_text(page)


    def test_monitors_page_lists_fixture_monitor(self, page):
        page.goto("/monitors")
        expect(page.get_by_role("cell", name="Heart failure devices")).to_be_visible()
        expect(page.get_by_text("Active")).to_be_visible()
        expect(page.get_by_text("Heart failure devices")).to_be_visible()
        _no_fallback_text(page)

    def test_monitor_frequency_can_be_changed_from_next_run_column(self, page):
        page.goto("/monitors")
        frequency = page.get_by_role(
            "combobox", name="Frequency: Heart failure devices")
        expect(frequency).to_have_value("daily")

        with page.expect_response(
                lambda response: response.request.method == "PATCH"
                and response.url.endswith("/api/monitors/1")):
            frequency.select_option("weekly")
        expect(frequency).to_have_value("weekly")
        monitor = page.request.get("/api/monitors/1").json()["monitor"]
        assert monitor["schedule_frequency"] == "weekly"
        assert monitor["next_run_at"] is not None
        assert page.url.endswith("/monitors")

        with page.expect_response(
                lambda response: response.request.method == "PATCH"
                and response.url.endswith("/api/monitors/1")):
            frequency.select_option("manual")
        expect(frequency).to_have_value("manual")
        monitor = page.request.get("/api/monitors/1").json()["monitor"]
        assert monitor["schedule_enabled"] == 0
        assert monitor["next_run_at"] is None

    def test_deep_link_refresh_route_integrity(self, page):
        # every important route must survive a reload (#25)
        for path in ("/dashboard", "/trials", "/monitors", "/updates",
                     "/trials/NCT/NCT00000001?tab=changes", "/monitors/1?tab=trials"):
            page.goto(path)
            page.wait_for_load_state("domcontentloaded")
            page.reload()
            page.wait_for_load_state("domcontentloaded")
            _no_fallback_text(page)
            assert page.locator("#root").evaluate("el => el.children.length") > 0

    def test_root_redirect_renders_dashboard(self, page):
        # regression: the "/" redirect early-return used to change the shell's
        # hook count between renders and crash the app (React error #310)
        page.goto("/")
        page.wait_for_url("**/dashboard")
        expect(page.get_by_role("heading", name="All Monitored Topics")).to_be_visible()
        assert "Something went wrong" not in page.locator("body").inner_text()

    def test_legacy_routes_redirect(self, page):
        cases = {
            "/watched": "/trials?view=watched",
            "/changes": "/updates",
            "/notifications": "/updates?view=notifications",
            "/timeline": "/updates?view=digest",
        }
        for old, new in cases.items():
            page.goto(old)
            page.wait_for_url(f"**{new}")
            _no_fallback_text(page)


class TestTrialChanges:
    """#36: trial change history visible in the browser with labels/values."""

    def test_changes_tab_shows_history(self, page):
        page.goto("/trials/NCT/NCT00000001?tab=changes")
        expect(page.get_by_role("heading", name="Heart failure device trial in adults")).to_be_visible()
        # human-readable labels (not raw field paths)
        expect(page.get_by_text("Recruitment Status").first).to_be_visible()
        expect(page.get_by_text("Enrollment", exact=True).first).to_be_visible()
        expect(page.get_by_text("Primary Outcome").first).to_be_visible()
        # old → new values
        expect(page.get_by_text("120", exact=True).first).to_be_visible()
        expect(page.get_by_text("148", exact=True).first).to_be_visible()
        expect(page.get_by_text("Recruiting").first).to_be_visible()
        # severity badges (text, not color-only)
        expect(page.get_by_text("Important", exact=True).first).to_be_visible()
        expect(page.get_by_text("Critical", exact=True).first).to_be_visible()
        # event dates visible (recent events float with the clock, old stay fixed)
        expect(page.get_by_text(RECENT_DAY).first).to_be_visible()
        expect(page.get_by_text("2026-07-03").first).to_be_visible()

    def test_history_tab_versions_and_compare(self, page):
        page.goto("/trials/NCT/NCT00000001?tab=history")
        expect(page.get_by_text("Version 3").first).to_be_visible()
        expect(page.get_by_text("Version 2").first).to_be_visible()
        expect(page.get_by_text("Version 1").first).to_be_visible()
        page.get_by_role("button", name="Compare", exact=True).click()
        expect(page.get_by_text("Enrollment", exact=True).first).to_be_visible()

    def test_overview_tab_sites_table(self, page):
        page.goto("/trials/NCT/NCT00000001")
        expect(page.get_by_role("heading", name="Locations")).to_be_visible()
        expect(page.get_by_text("Beijing Heart Hospital")).to_be_visible()
        expect(page.get_by_text("Shanghai General")).to_be_visible()
        body = page.locator("body").inner_text()
        assert '{"facility"' not in body, "raw JSON leaked into locations rendering"


class TestWatchWorkflow:
    """#37: watch → watched list → persists across reload."""

    def test_watch_persists(self, page):
        page.goto("/trials/NCT/NCT00000002")
        watch_btn = page.get_by_role("button", name="☆ Watch")
        expect(watch_btn).to_be_visible()
        watch_btn.click()
        expect(page.get_by_role("button", name="★ Watching")).to_be_visible()

        page.goto("/trials?view=watched")
        expect(page.get_by_text("Myocardial infarction registry study")).to_be_visible()
        # reload → state persists (real backend storage)
        page.reload()
        expect(page.get_by_text("Myocardial infarction registry study")).to_be_visible()


class TestResearchProjects:
    def test_saved_search_and_monitor_link_from_existing_surfaces(self, page):
        created = page.request.post("/api/projects", data={"name": "Linked assets"}).json()["project"]
        sid = page.request.post("/api/saved-searches", data={"name": "Heart failure search", "state": {"q": "heart failure"}}).json()["saved_search"]["id"]
        page.goto("/trials?view=saved")
        page.get_by_role("button", name="Add to Project").click()
        page.get_by_role("button", name="Linked assets").click()
        page.goto("/monitors/1")
        page.get_by_role("button", name="Add to Project").click()
        page.get_by_role("button", name="Linked assets").click()
        page.goto(f"/projects/{created['id']}?tab=searches")
        expect(page.get_by_text("Heart failure search")).to_be_visible()
        assert page.request.get(f"/api/projects/{created['id']}").json()["project"]["counts"]["monitors"] == 1
        page.get_by_role("heading", name="Linked Monitors").locator("..").get_by_role("button", name="Remove").click()
        assert page.request.get("/api/monitors/1").status == 200
        assert page.request.get(f"/api/saved-searches/{sid}").status == 200

    def test_create_curate_note_evidence_and_deep_link(self, page):
        page.goto("/projects")
        page.get_by_role("textbox", name="Project name").fill("Heart failure research")
        page.get_by_role("textbox", name="Project description").fill("Local topic notes")
        page.get_by_role("button", name="New Project").click()
        expect(page.get_by_role("heading", name="Heart failure research")).to_be_visible()
        page.get_by_role("button", name="Open", exact=True).click()
        page.wait_for_url("**/projects/**")
        project_id = int(page.url.split("/projects/")[1].split("?")[0])
        page.get_by_role("tab", name="Notebook").click()
        page.get_by_role("textbox", name="New note").fill("Review enrollment change")
        page.get_by_role("button", name="Add Note").click()
        expect(page.get_by_text("Review enrollment change")).to_be_visible()
        page.goto("/trials/NCT/NCT00000001?tab=changes")
        page.get_by_role("button", name="Add to Project").click()
        page.get_by_role("button", name="Heart failure research").click()
        expect(page.get_by_text("Added to project")).to_be_visible()
        page.locator(".change-row").first.get_by_role("button", name="Save evidence").click()
        page.locator(".change-row").first.get_by_role("button", name="Heart failure research").click()
        # The picker shows "Added to project" only after the evidence POST
        # resolves; navigating earlier aborts the request mid-flight on slow
        # runners and the bookmark never exists.
        expect(page.get_by_text("Added to project")).to_be_visible()
        page.goto(f"/projects/{project_id}?tab=trials")
        expect(page.get_by_text("Heart failure device trial in adults")).to_be_visible()
        assert page.request.get(f"/api/projects/{project_id}").json()["project"]["trials"][0]["watching"] == 1  # fixture watch, not created by Project
        page.get_by_role("tab", name="Notebook").click()
        expect(page.get_by_role("heading", name="Evidence bookmarks")).to_be_visible()
        page.get_by_role("heading", name="Evidence bookmarks").locator("..").get_by_role("button", name=re.compile("Heart failure device trial")).click()
        page.wait_for_url("**tab=changes&event=**")
        expect(page.locator(".change-row-target")).to_be_visible()

    def test_scoped_updates_briefing_archive_and_delete_isolated(self, page):
        page.goto("/projects")
        page.get_by_role("textbox", name="Project name").fill("Project scope")
        page.get_by_role("button", name="New Project").click()
        page.get_by_role("button", name="Open", exact=True).click()
        project_id = int(page.url.split("/projects/")[1].split("?")[0])
        page.goto("/trials/NCT/NCT00000001")
        page.get_by_role("button", name="Add to Project").click()
        page.get_by_role("button", name="Project scope").click()
        page.goto(f"/projects/{project_id}?tab=updates")
        page.get_by_role("combobox", name="Time window").select_option("all")
        expect(page.get_by_text("Heart failure device trial in adults").first).to_be_visible()
        page.get_by_role("tab", name="Intelligence").click()
        expect(page.get_by_role("heading", name="Project Intelligence")).to_be_visible()
        page.locator(".analysis-row").filter(has_text="Heart failure device trial in adults").first.click()
        page.wait_for_url("**tab=changes&event=**")
        expect(page.locator(".change-row-target")).to_be_visible()
        page.goto("/projects")
        page.get_by_role("button", name="More actions").click()
        page.get_by_role("menuitem", name="Archive").click()
        expect(page.get_by_role("heading", name="Project scope")).to_have_count(0)
        page.get_by_role("checkbox", name="Show archived").check()
        expect(page.get_by_role("heading", name=re.compile("Project scope"))).to_be_visible()
        page.get_by_role("button", name="More actions").click()
        page.get_by_role("menuitem", name="Delete").click()
        page.get_by_role("dialog").get_by_role("button", name="Delete").click()
        assert page.request.get("/api/trials/NCT/NCT00000001").status == 200

class TestSearchDiscoveryWorkflow:
    """Global search → refine → review → save as a quiet rule monitor."""

    def test_global_search_refine_review_and_track(self, page):
        page.goto("/dashboard")
        page.get_by_role("textbox", name="Search trials").fill("heart failure")
        page.get_by_role("button", name="Search", exact=True).click()
        page.wait_for_url("**/trials?q=heart%20failure")
        expect(page.get_by_text("Heart failure device trial in adults").first).to_be_visible()
        expect(page.get_by_text("3 trials found")).to_be_visible()

        page.get_by_role("checkbox", name="Recruiting").click()
        expect(page.get_by_text("Heart failure device trial in adults").first).to_be_visible()
        # Result opens the real detail route; browser Back retains its search URL.
        page.get_by_text("Heart failure device trial in adults").first.click()
        page.wait_for_url("**/trials/NCT/NCT00000001")
        page.go_back()
        page.wait_for_url("**/trials?**")
        assert "q=heart" in page.url and "statuses=Recruiting" in page.url

        page.get_by_role("button", name="Track this search").click()
        expect(page.get_by_role("dialog", name="Track this search")).to_contain_text("Current matches: 2 trials")
        page.get_by_role("button", name="Create Monitor").click()
        page.wait_for_url("**/monitors/**")
        expect(page.get_by_role("heading", name="heart failure")).to_be_visible()


class TestNaturalSearch:
    def test_english_interpret_edit_run_reload_and_track(self, page):
        page.goto("/dashboard")
        page.get_by_role("radio", name="Ask naturally").click()
        page.get_by_role("textbox", name="Ask naturally across trials").fill(
            "Recruiting phase 2 or 3 myocarditis trials in China")
        page.get_by_role("button", name="Search", exact=True).click()
        expect(page.get_by_role("heading", name="Interpreted search")).to_be_visible()
        expect(page.get_by_label("query 1")).to_have_value("myocarditis")
        expect(page.get_by_label("country 1")).to_have_value("China")
        expect(page.get_by_label("statuses 1")).to_have_value("Recruiting")
        expect(page.get_by_label("phase 1")).to_have_value("Phase 2")
        expect(page.get_by_role("textbox", name="phase 2")).to_have_value("Phase 3")
        # Manual correction is authoritative for the subsequent search/monitor.
        page.get_by_role("button", name="Remove Phase 3").click()
        page.get_by_role("button", name="Run search").click()
        page.wait_for_url("**/trials?**")
        assert "phase=Phase+2" in page.url and "country=China" in page.url
        expect(page.get_by_text("1 trial found")).to_be_visible()
        page.reload()
        expect(page.get_by_text("1 trial found")).to_be_visible()
        assert "ask=" not in page.url
        page.get_by_role("button", name="Track this search").click()
        page.get_by_role("button", name="Create Monitor").click()
        page.wait_for_url("**/monitors/**")
        expect(page.locator(".rules-grid")).to_contain_text("China")
        expect(page.locator(".rules-grid")).to_contain_text("Phase 2")

    def test_chinese_country_and_chictr_registry(self, page):
        page.goto("/trials?ask=" + "中国正在招募的II期心肌炎临床试验")
        expect(page.get_by_label("query 1")).to_have_value("心肌炎")
        expect(page.get_by_label("country 1")).to_have_value("China")
        expect(page.get_by_label("statuses 1")).to_have_value("Recruiting")
        page.get_by_role("button", name="Toggle language").click()
        expect(page.get_by_role("heading", name="搜索解析结果")).to_be_visible()
        expect(page.get_by_text("中国被解析为国家/地区，不是 ChiCTR 注册平台。")).to_be_visible()
        page.get_by_role("button", name="切换语言").click()
        page.get_by_role("button", name="Run search").click()
        expect(page.get_by_text("1 trial found")).to_be_visible()
        page.goto("/trials?ask=ChiCTR%20心肌炎试验")
        expect(page.get_by_label("registries 1")).to_have_value("ChiCTR")
        expect(page.get_by_label("query 1")).to_have_value("心肌炎")
        assert page.get_by_label("country 1").count() == 0

    def test_ambiguity_unsupported_and_keyword_fallback(self, page):
        page.goto("/trials?ask=recent%20myocarditis%20trials")
        expect(page.get_by_role("heading", name="Ambiguous")).to_be_visible()
        expect(page.get_by_label("query 1")).to_have_value("myocarditis")
        page.goto("/trials?ask=most%20promising%20myocarditis%20trials")
        expect(page.get_by_role("heading", name="Unsupported constraints")).to_be_visible()
        expect(page.get_by_label("query 1")).to_have_value("myocarditis")
        page.get_by_role("button", name="Search as keywords").click()
        expect(page.get_by_role("heading", name="Trials", exact=True)).to_be_visible()
        expect(page.get_by_text("Scope: most promising myocarditis trials")).to_be_visible()

    def test_interpreter_failure_keeps_keyword_search(self, page, monkeypatch):
        def unavailable(_text):
            raise RuntimeError("interpreter unavailable")
        monkeypatch.setattr("core.query_interpreter.interpret", unavailable)
        page.goto("/trials?ask=myocarditis%20trials")
        expect(page.get_by_role("alert")).to_contain_text("Could not interpret")
        page.get_by_role("button", name="Search as keywords").click()
        expect(page.get_by_role("heading", name="Trials", exact=True)).to_be_visible()
        expect(page.get_by_text("Scope: myocarditis trials")).to_be_visible()
        expect(page.get_by_text("0 trials found")).to_be_visible()

    def test_saved_monitor_does_not_call_interpreter(self, page, monkeypatch):
        page.goto("/trials?ask=Recruiting%20myocarditis%20trials%20in%20China")
        expect(page.get_by_label("query 1")).to_have_value("myocarditis")
        page.get_by_role("button", name="Run search").click()
        expect(page.get_by_text("2 trials found")).to_be_visible()
        page.get_by_role("button", name="Track this search").click()
        page.get_by_role("button", name="Create Monitor").click()
        page.wait_for_url("**/monitors/**")
        monitor_id = page.url.rsplit("/", 1)[-1]
        before = page.request.get(f"/api/monitors/{monitor_id}").json()["monitor"]["rules"]
        def unavailable(_text):
            raise RuntimeError("interpreter unavailable")
        monkeypatch.setattr("core.query_interpreter.interpret", unavailable)
        response = page.request.post(f"/api/monitors/{monitor_id}/run")
        assert response.status == 200
        after = page.request.get(f"/api/monitors/{monitor_id}").json()["monitor"]["rules"]
        assert after == before


class TestSavedSearchWorkflow:
    def test_empty_library_has_clear_create_path(self, page):
        page.goto("/trials?view=saved")
        expect(page.get_by_text("No Saved Searches yet")).to_be_visible()
        expect(page.get_by_text("Run a trial search, then choose Save Search on the results page.")).to_be_visible()
        create_actions = page.get_by_role("button", name="Search and save")
        expect(create_actions).to_have_count(2)
        create_actions.last.click()
        page.wait_for_url("**/trials")
        expect(page.get_by_role("textbox", name="Search within trials")).to_be_visible()

    def _open_saved_library(self, page):
        """The Saved Searches entry lives in the search page's "More ▾" menu
        (a details/summary popover — the summary element carries no button role)."""
        page.locator(".search-actions details.action-menu > summary").click()
        page.get_by_role("button", name="Saved Searches").click()

    def _save_myocarditis(self, page, name="Myocarditis research"):
        page.goto("/trials?q=myocarditis&statuses=Recruiting&phase=Phase%202")
        expect(page.get_by_text("2 trials found")).to_be_visible()
        page.get_by_role("button", name="Save Search", exact=True).click()
        dialog = page.get_by_role("dialog", name="Save Search")
        expect(dialog).to_contain_text("Current matches: 2 trials")
        expect(dialog).to_contain_text("does not run automatically")
        dialog.get_by_role("textbox", name="Name").fill(name)
        dialog.get_by_role("button", name="Save Search").click()
        expect(page.get_by_text(f"Opened from {name}")).to_be_visible()
        return int(page.url.split("saved_search=")[1].split("&")[0])

    def test_save_open_refine_no_autosave_update_and_save_as(self, page):
        sid = self._save_myocarditis(page)
        assert page.request.get(f"/api/saved-searches/{sid}").status == 200
        page.goto("/dashboard")
        page.get_by_role("navigation", name="Primary navigation").get_by_role("link", name="Saved Searches").click()
        page.reload()
        expect(page.get_by_role("heading", name="Myocarditis research")).to_be_visible()
        page.get_by_role("button", name="Open Myocarditis research").click()
        expect(page.get_by_text("2 trials found")).to_be_visible()
        page.get_by_text("Myocarditis after CAR-T therapy trial").first.click()
        page.wait_for_url("**/trials/NCT/NCT09000301")
        page.go_back()
        assert f"saved_search={sid}" in page.url
        page.get_by_role("checkbox", name=re.compile(r"^China")).click()
        expect(page.get_by_text("1 trial found")).to_be_visible()
        expect(page.get_by_text("Filters changed; the Saved Search has not been updated.")).to_be_visible()
        self._open_saved_library(page)
        page.get_by_role("button", name="Open Myocarditis research").click()
        expect(page.get_by_text("2 trials found")).to_be_visible()
        assert "country=" not in page.url
        page.get_by_role("checkbox", name=re.compile(r"^China")).click()
        page.get_by_role("button", name="Update Saved Search").click()
        expect(page.get_by_text("Saved search updated.")).to_be_visible()
        page.reload()
        assert "country=China" in page.url
        self._open_saved_library(page)
        page.get_by_role("button", name="Open Myocarditis research").click()
        page.wait_for_url("**/trials?*saved_search=*")
        assert "country=China" in page.url
        page.get_by_role("checkbox", name=re.compile(r"Phase 2.*1")).click()
        expect(page.get_by_role("button", name="Save as New Search")).to_be_visible()
        page.get_by_role("button", name="Save as New Search").click()
        dialog = page.get_by_role("dialog", name="Save Search")
        dialog.get_by_role("textbox", name="Name").fill("Myocarditis China broad")
        dialog.get_by_role("button", name="Save Search").click()
        self._open_saved_library(page)
        expect(page.get_by_role("heading", name="Myocarditis research")).to_be_visible()
        expect(page.get_by_role("heading", name="Myocarditis China broad")).to_be_visible()
        assert page.request.get(f"/api/saved-searches/{sid}").json()["saved_search"]["state"]["phase"] == ["Phase 2"]

    def test_library_pin_duplicate_rename_delete_isolated(self, page):
        sid = self._save_myocarditis(page, "Library item")
        self._open_saved_library(page)
        page.get_by_role("button", name="Pin Library item").click()
        expect(page.get_by_role("heading", name="★ Library item")).to_be_visible()
        page.get_by_role("button", name="Duplicate Library item").click()
        expect(page.get_by_role("heading", name="Library item (Copy)")).to_be_visible()
        page.get_by_role("button", name="Rename Library item", exact=True).click()
        dialog = page.get_by_role("dialog", name="Rename")
        dialog.get_by_role("textbox", name="Name").fill("Pinned research")
        dialog.get_by_role("button", name="Save name and description").click()
        page.reload()
        expect(page.get_by_role("heading", name="★ Pinned research")).to_be_visible()
        page.get_by_role("button", name="Delete Pinned research").click()
        confirm = page.get_by_role("alertdialog", name="Delete")
        expect(confirm).to_contain_text("will not delete trials, monitors, watches, or notifications")
        confirm.get_by_role("button", name="Delete Saved Search").click()
        expect(page.get_by_role("heading", name="★ Pinned research")).to_have_count(0)
        expect(page.get_by_role("heading", name="Library item (Copy)")).to_be_visible()
        assert page.request.get(f"/api/saved-searches/{sid}").status == 404

    def test_track_saved_search_quiet_baseline_and_future_entry(self, page):
        import sqlite3 as sq
        from config import CONFIG
        sid = self._save_myocarditis(page)
        before_notifications = page.request.get("/api/notifications/unread-count").json()["count"]
        self._open_saved_library(page)
        page.get_by_role("button", name="Track as Monitor Myocarditis research").click()
        dialog = page.get_by_role("dialog", name="Track this search")
        expect(dialog).to_contain_text("Current matches: 2 trials")
        dialog.get_by_role("button", name="Create Monitor").click()
        page.wait_for_url("**/monitors/**")
        mid = int(page.url.rsplit("/", 1)[-1])
        assert page.request.get(f"/api/saved-searches/{sid}").status == 200
        monitor = page.request.get(f"/api/monitors/{mid}").json()["monitor"]
        assert monitor["rules"] == {"query": ["myocarditis"], "statuses": ["Recruiting"], "phase": ["Phase 2"]}
        assert page.request.get(f"/api/monitors/{mid}/activity").json()["activity"] == []
        assert page.request.get("/api/notifications/unread-count").json()["count"] == before_notifications
        conn = sq.connect(str(CONFIG.db.path))
        conn.execute("UPDATE registry_records SET status_id=(SELECT status_type_id FROM status_types WHERE label='Recruiting') WHERE source_trial_id='NCT09000309' AND is_latest=1")
        conn.commit(); conn.close()
        page.request.post(f"/api/monitors/{mid}/run")
        assert any(x["event_type"] == "trial_entered" for x in page.request.get(f"/api/monitors/{mid}/activity").json()["activity"])
        assert page.request.get(f"/api/saved-searches/{sid}").status == 200
        page.request.delete(f"/api/saved-searches/{sid}")
        assert page.request.get(f"/api/monitors/{mid}").status == 200

    def test_chinese_natural_origin_reopens_without_interpreter(self, page, monkeypatch):
        page.goto("/trials?ask=中国正在招募的II期心肌炎临床试验")
        expect(page.get_by_label("query 1")).to_have_value("心肌炎")
        page.get_by_role("button", name="Run search").click()
        page.get_by_role("button", name="Save Search", exact=True).click()
        dialog = page.get_by_role("dialog", name="Save Search")
        expect(dialog).to_contain_text("中国正在招募的II期心肌炎临床试验")
        dialog.get_by_role("button", name="Save Search").click()
        page.wait_for_url("**/trials?*saved_search=*")
        sid = int(page.url.split("saved_search=")[1].split("&")[0])
        def unavailable(_text):
            raise RuntimeError("interpreter unavailable")
        monkeypatch.setattr("core.query_interpreter.interpret", unavailable)
        self._open_saved_library(page)
        page.reload()
        expect(page.get_by_text("中国正在招募的II期心肌炎临床试验").first).to_be_visible()
        page.get_by_role("button", name="Open 心肌炎").click()
        expect(page.get_by_text("1 trial found")).to_be_visible()
        assert page.request.get(f"/api/saved-searches/{sid}").json()["saved_search"]["original_nl_query"] == "中国正在招募的II期心肌炎临床试验"

    def test_monitor_open_as_search_can_be_saved(self, page):
        page.goto("/monitors/1")
        page.get_by_role("button", name="Open as Search").click()
        page.get_by_role("button", name="Save Search", exact=True).click()
        dialog = page.get_by_role("dialog", name="Save Search")
        dialog.get_by_role("textbox", name="Name").fill("From existing monitor")
        dialog.get_by_role("button", name="Save Search").click()
        page.wait_for_url("**/trials?*saved_search=*")
        sid = int(page.url.split("saved_search=")[1].split("&")[0])
        stored = page.request.get(f"/api/saved-searches/{sid}").json()["saved_search"]["state"]
        assert stored["condition"] == ["heart failure"]
        assert page.request.get("/api/monitors/1").status == 200

    def test_saved_search_reopen_preserves_freshness_warning(self, page, tmp_path):
        sid = self._save_myocarditis(page)
        conn = sqlite3.connect(tmp_path / "e2e.db")
        conn.execute("UPDATE registry_sources SET enabled=1 WHERE short_name='NCT'")
        # Genuinely stale: no successful sync AND no record re-verified since
        # 09-01 (the e2e seed includes a record crawled today, which alone
        # proves the library holds current NCT data).
        conn.execute("UPDATE registry_records SET last_crawled_at='2026-08-01 00:00:00' "
                     "WHERE source_id=(SELECT source_id FROM registry_sources WHERE short_name='NCT')")
        conn.execute("INSERT INTO sync_status (source_id,last_successful_sync) VALUES ((SELECT source_id FROM registry_sources WHERE short_name='NCT'),'2026-09-01 00:00:00')")
        conn.commit(); conn.close()
        self._open_saved_library(page)
        page.get_by_role("button", name="Open Myocarditis research").click()
        expect(page.get_by_text("Some registry data is delayed", exact=False)).to_be_visible()
        assert f"saved_search={sid}" in page.url
        conn = sqlite3.connect(tmp_path / "e2e.db")
        # Enrich-style recovery: records re-verified now while the sync cursor
        # stays old — the exact case that used to false-positive as stale.
        now = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
        conn.execute("UPDATE registry_records SET last_crawled_at=? "
                     "WHERE source_id=(SELECT source_id FROM registry_sources WHERE short_name='NCT')", (now,))
        conn.commit(); conn.close()
        page.reload()
        expect(page.get_by_text("Some registry data is delayed", exact=False)).to_have_count(0)

class TestMonitorWorkflow:
    """#38: monitor list → detail → trials → trial changes."""

    def test_monitor_detail_to_trial_changes(self, page):
        page.goto("/monitors")
        page.get_by_role("cell", name="Heart failure devices").click()
        expect(page.get_by_role("heading", name="Heart failure devices")).to_be_visible()
        page.get_by_role("tab", name="Trials").click()
        row = page.get_by_role("cell", name="NCT00000001")
        expect(row).to_be_visible()
        row.get_by_role("link").click()
        page.wait_for_url("**/trials/NCT/NCT00000001**")
        page.get_by_role("tab", name="Changes").click()
        expect(page.get_by_text("Recruitment Status").first).to_be_visible()


class TestNotifications:
    """#39: unread badge → mark read → count changes → persists."""

    def test_notification_read_state(self, page):
        page.goto("/updates?view=notifications")
        expect(page.get_by_text("Enrollment changed: 120 → 148")).to_be_visible()
        # unread badge in the shell shows 1
        expect(page.locator(".bell-count")).to_have_text("1")
        # mark the one notification read
        page.get_by_role("button", name="Mark read").click()
        expect(page.locator(".bell-count")).to_have_count(0)
        # reload → read state persisted
        page.reload()
        expect(page.get_by_text("Enrollment changed: 120 → 148")).to_be_visible()
        assert page.locator(".notification-unread").count() == 0


class TestDashboard:
    """#6/#34: real counts from the aggregate endpoint."""

    def test_dashboard_shows_real_activity(self, page):
        page.goto("/dashboard")
        expect(page.get_by_role("heading", name="All Monitored Topics")).to_be_visible()
        # fixture: 1 new trial this week, 1 trial with changes, watched update 2
        expect(page.locator(".kpi-strip").first).to_be_visible()
        body = page.locator("body").inner_text()
        assert FALLBACK_TEXT not in body
        # the changed trial surfaces in Important updates with old→new
        expect(page.get_by_text("Heart failure device trial in adults").first).to_be_visible()
        page.get_by_role("button", name=re.compile("Critical Heart failure device trial")).click()
        page.wait_for_url("**tab=changes&event=**")
        expect(page.locator(".change-row-target")).to_be_visible()


class TestMyocarditisSearchJourney:
    """#51 (mandatory): myocarditis discovery — search, refine, open, return."""

    def test_reopen_restores_search_without_refetching_results(self, page):
        page.goto("/trials?q=myocarditis")
        expect(page.get_by_role("heading", name="Trials", exact=True)).to_be_visible()
        expect(page.get_by_text("4 trials found")).to_be_visible()

        repeated_search_requests = []
        page.on("request", lambda request: repeated_search_requests.append(request.url)
                if "/api/trials/search?" in request.url else None)

        # The validated response is persisted: a full document reload paints
        # it directly and does not silently execute the same search again.
        page.reload()
        expect(page.get_by_text("4 trials found")).to_be_visible()
        assert repeated_search_requests == []

        # Opening the app's bare root resumes the last useful screen, including
        # its complete search/filter URL, instead of resetting to Dashboard.
        page.goto("/")
        page.wait_for_url("**/trials?q=myocarditis")
        expect(page.get_by_text("4 trials found")).to_be_visible()
        assert repeated_search_requests == []

    def test_search_filter_open_and_return(self, page):
        page.goto("/trials")
        box = page.get_by_role("textbox", name="Search trials")
        expect(box).to_be_visible()
        box.fill("myocarditis")
        box.press("Enter")
        page.wait_for_url("**/trials?q=myocarditis**")
        expect(page.get_by_role("heading", name="Trials", exact=True)).to_be_visible()
        expect(page.get_by_text("4 trials found")).to_be_visible()

        # cross-registry identity: a ChiCTR facet chip with a full-set count
        page.get_by_role("checkbox", name=re.compile(r"ChiCTR.*1")).click()
        page.wait_for_url("**registries=ChiCTR**")
        expect(page.get_by_text("1 trial found")).to_be_visible()

        # status refinement stacks onto the registry filter
        page.get_by_role("checkbox", name=re.compile(r"Recruiting.*1")).click()
        page.wait_for_url("**statuses=Recruiting**")
        expect(page.get_by_text("1 trial found")).to_be_visible()

        # open the trial → back → search state preserved
        page.get_by_role("link", name="Acute myocarditis", exact=False).first.click()
        page.wait_for_url("**/trials/ChiCTR/ChiCTR2200060001**")
        page.go_back()
        page.wait_for_url("**statuses=Recruiting**")
        expect(page.get_by_role("heading", name="Trials", exact=True)).to_be_visible()
        assert "q=myocarditis" in page.url

    def test_registry_specific_zero_result_is_explained(self, page):
        # myocarditis exists globally, but no ChiCTR record is Completed (#44)
        page.goto("/trials?q=myocarditis&registries=ChiCTR&statuses=Completed")
        expect(page.get_by_text('No trials found for "myocarditis"')).to_be_visible()
        expect(page.get_by_role("button", name="Show all 4 matching trials")).to_be_visible()


class TestWatchFromSearch:
    """#52: watch directly from search results; persists across reload."""

    def test_watch_from_search_results_persists(self, page):
        page.goto("/trials?q=myocarditis")
        card = page.locator(".trial-row").filter(has_text="Myocarditis after CAR-T").first
        watch = card.get_by_role("button", name="☆ Watch")
        expect(watch).to_be_visible()
        watch.click()
        expect(card.get_by_role("button", name="★ Watching")).to_be_visible()

        page.goto("/trials?view=watched")
        expect(page.get_by_text("Myocarditis after CAR-T therapy trial")).to_be_visible()
        page.reload()
        expect(page.get_by_text("Myocarditis after CAR-T therapy trial")).to_be_visible()


class TestTrackMyocarditisSearch:
    """#53 (mandatory): myocarditis + Recruiting + Phase 2 → quiet monitor."""

    def test_track_search_creates_rule_monitor(self, page):
        page.goto("/trials?q=myocarditis&statuses=Recruiting&phase=Phase%202")
        expect(page.get_by_text("2 trials found")).to_be_visible()

        page.get_by_role("button", name="Track this search").click()
        dialog = page.get_by_role("dialog", name="Track this search")
        expect(dialog).to_contain_text("Current matches: 2 trials")
        expect(dialog).to_contain_text("Recruiting")
        expect(dialog).to_contain_text("Phase 2")

        page.get_by_role("button", name="Create Monitor").click()
        page.wait_for_url("**/monitors/**")
        expect(page.get_by_role("heading", name="myocarditis")).to_be_visible()

        # persisted rule preserves every search dimension (#47)
        rules = page.locator(".rules-grid")
        expect(rules).to_contain_text("myocarditis")
        expect(rules).to_contain_text("Recruiting")
        expect(rules).to_contain_text("Phase 2")

        # baseline membership = the two current matches; zero activity (#23)
        page.get_by_role("tab", name="Trials").click()
        expect(page.get_by_role("cell", name="NCT09000301")).to_be_visible()
        expect(page.get_by_role("cell", name="ChiCTR2200060001")).to_be_visible()
        page.get_by_role("tab", name="Activity").click()
        expect(page.get_by_text("No monitor activity yet.")).to_be_visible()


class TestSearchMonitorLifecycle:
    """#54/#55: the saved monitor tracks the RULE — enters and leaves."""

    def test_future_match_enters_and_nonmatching_leaves(self, page):
        import datetime as dt
        import sqlite3 as sq

        from config import CONFIG

        # track myocarditis + Recruiting (baseline: 3 recruiting myocarditis trials)
        page.goto("/trials?q=myocarditis&statuses=Recruiting")
        expect(page.get_by_text("3 trials found")).to_be_visible()
        page.get_by_role("button", name="Track this search").click()
        page.get_by_role("button", name="Create Monitor").click()
        page.wait_for_url("**/monitors/**")
        monitor_url = page.url

        # Upstream change: NCT09000309 (Completed → non-matching) turns Recruiting
        db_path = str(CONFIG.db.path)
        now = (dt.datetime.utcnow() + dt.timedelta(seconds=2)).strftime("%Y-%m-%d %H:%M:%S")
        conn = sq.connect(db_path)
        conn.execute(
            "UPDATE registry_records SET status_id="
            "(SELECT status_type_id FROM status_types WHERE label='Recruiting') "
            "WHERE source_trial_id='NCT09000309' AND is_latest=1")
        rid = conn.execute(
            "SELECT record_id FROM registry_records "
            "WHERE source_trial_id='NCT09000309' AND is_latest=1").fetchone()[0]
        conn.execute(
            """INSERT INTO trial_events (record_id, field_name, old_value, new_value,
                                         change_category, change_type, severity,
                                         importance_score, event_hash, detected_at)
               VALUES (?, 'status_id', 'Completed', 'Recruiting', 'status',
                       'modified', 'important', 60, ?, ?)""",
            (rid, f"hash-{rid}-{now}", now))
        conn.commit()
        conn.close()

        page.get_by_role("button", name="Run now").click()
        page.get_by_role("tab", name="Trials").click()
        expect(page.get_by_role("cell", name="NCT09000309")).to_be_visible()
        page.get_by_role("tab", name="Activity").click()
        expect(page.get_by_text("entered", exact=True).first).to_be_visible()
        # the entered trial's Changes tab shows the upstream status change
        page.goto("/trials/NCT/NCT09000309?tab=changes")
        expect(page.get_by_text("Recruitment Status").first).to_be_visible()
        expect(page.get_by_text("Recruiting").first).to_be_visible()

        # #55: NCT09000301 stops matching (Completed) → leaves on the next run
        conn = sq.connect(db_path)
        conn.execute(
            "UPDATE registry_records SET status_id="
            "(SELECT status_type_id FROM status_types WHERE label='Completed') "
            "WHERE source_trial_id='NCT09000301' AND is_latest=1")
        conn.commit()
        conn.close()
        page.goto(monitor_url)
        page.get_by_role("button", name="Run now").click()
        page.get_by_role("tab", name="Trials").click()
        expect(page.get_by_text("Left", exact=True).first).to_be_visible()
        expect(page.get_by_role("cell", name="NCT09000301")).to_be_visible()


class TestSearchReadOnly:
    """#26/#56: search is exploration — repeated searches change nothing."""

    def test_repeated_search_has_no_side_effects(self, page):
        page.goto("/monitors")
        expect(page.get_by_role("cell", name="Heart failure devices")).to_be_visible()
        rows_before = page.locator(".monitors-table tbody tr").count()

        for _ in range(3):
            page.goto("/trials?q=myocarditis")
            expect(page.get_by_text("4 trials found")).to_be_visible()

        page.goto("/monitors")
        expect(page.get_by_role("cell", name="Heart failure devices")).to_be_visible()
        assert page.locator(".monitors-table tbody tr").count() == rows_before
        # the seeded unread notification was neither duplicated nor cleared
        expect(page.locator(".bell-count")).to_have_text("1")


class TestOpenAsSearch:
    """#28: a monitor's rule can be reopened as a search, unmodified."""

    def test_monitor_rule_opens_as_search(self, page):
        page.goto("/monitors")
        page.get_by_role("cell", name="Heart failure devices").click()
        page.get_by_role("button", name="Open as Search").click()
        page.wait_for_url("**/trials?**condition=heart+failure**")
        expect(page.get_by_role("heading", name="Trials")).to_be_visible()
        expect(page.get_by_text("3 trials found")).to_be_visible()
        # the monitor itself is untouched (still exactly one rule dimension)
        page.goto("/monitors/1")
        rules = page.locator(".rules-grid")
        expect(rules).to_contain_text("heart failure")


class TestChangeIntelligence:
    """Phase 3E.2: persisted event IDs drive the detail and scoped feeds."""

    def test_change_deep_link_and_monitor_activity_resolve_exact_event(self, page):
        page.goto("/trials/NCT/NCT00000001?tab=changes&event=3")
        expect(page.locator("#event-3")).to_be_visible()
        expect(page.locator("#event-3")).to_have_class(re.compile(r".*change-row-target.*"))
        expect(page.get_by_text("Enrollment", exact=True).first).to_be_visible()

        page.goto("/monitors/1")
        page.get_by_role("tab", name="Activity").click()
        page.get_by_role("link", name="Changes").click()
        page.wait_for_url("**tab=changes&event=**")
        expect(page.locator(".change-row-target")).to_be_visible()


class TestPhase2TrialResearchWorkspace:
    """Phase 2 acceptance: dense discovery, preview/compare and evidence reading."""

    def test_filters_preview_and_comparison(self, page):
        page.goto("/trials?q=myocarditis&sort=id")
        expect(page.get_by_role("heading", name="Trials", exact=True)).to_be_visible()
        expect(page.locator(".trial-row")).to_have_count(4)

        # Native fieldset filters update the URL and removable chips. Clearing
        # structured filters preserves both the free-text query and sort.
        page.get_by_role("checkbox", name=re.compile(r"Recruiting.*3")).click()
        page.wait_for_url("**statuses=Recruiting**")
        expect(page.get_by_role("button", name="Remove filter: Recruiting")).to_be_visible()
        page.get_by_role("button", name="Clear all").first.click()
        page.wait_for_url(lambda url: "q=myocarditis" in str(url) and "sort=id" in str(url) and "statuses=" not in str(url))

        first = page.locator(".trial-row").first
        first.get_by_role("button", name="Quick preview").click()
        preview = page.get_by_role("dialog", name="Trial preview")
        expect(preview).to_be_visible()
        expect(preview.get_by_role("button", name="Open full detail")).to_be_visible()
        preview.get_by_role("button", name="Close").click()
        expect(preview).to_be_hidden()

        selectors = page.get_by_role("checkbox", name=re.compile("Select trial:"))
        selectors.nth(0).check()
        selectors.nth(1).check()
        compare_bar = page.get_by_label("Trial comparison selection")
        expect(compare_bar).to_be_visible()
        compare_bar.get_by_role("button", name="Compare 2–3").click()
        dialog = page.get_by_role("dialog", name="Compare trials")
        expect(dialog).to_be_visible()
        dialog.get_by_role("checkbox", name="Show differences only").check()
        expect(dialog.locator(".comparison-different").first).to_be_visible()
        expect(dialog.get_by_role("button", name="Remove from comparison").first).to_be_visible()

    def test_mobile_filter_draft_preview_and_no_horizontal_overflow(self, page):
        page.set_viewport_size({"width": 375, "height": 812})
        page.goto("/trials?q=myocarditis")
        page.get_by_role("button", name=re.compile(r"Filters · 0")).click()
        drawer = page.get_by_role("dialog", name="Filters")
        expect(drawer).to_be_visible()
        drawer.get_by_role("checkbox", name=re.compile(r"Recruiting.*3")).check()
        drawer.get_by_role("button", name="Cancel").click()
        assert "statuses=" not in page.url

        page.get_by_role("button", name=re.compile(r"Filters · 0")).click()
        drawer.get_by_role("checkbox", name=re.compile(r"Recruiting.*3")).check()
        drawer.get_by_role("button", name="Apply filters").click()
        page.wait_for_url("**statuses=Recruiting**")
        page.locator(".trial-row").first.get_by_role("button", name="Quick preview").click()
        expect(page.get_by_role("dialog", name="Trial preview")).to_be_visible()
        overflow = page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
        assert overflow <= 1

    def test_detail_tabs_anchors_eligibility_and_print(self, page):
        def add_criteria(route):
            response = route.fetch()
            payload = response.json()
            payload["inclusion"] = "Age 18 years or older\nConfirmed cardiovascular disease"
            payload["exclusion"] = "Pregnancy\nUnable to provide consent"
            route.fulfill(response=response, json=payload)

        page.route("**/api/trials/NCT/NCT00000001", add_criteria)
        page.goto("/trials/NCT/NCT00000001")
        nav = page.get_by_role("navigation", name="Overview sections")
        expect(nav.get_by_role("link", name="Eligibility")).to_be_visible()
        nav.get_by_role("link", name="Eligibility").click()
        expect(page.locator("#eligibility")).to_be_visible()
        expand = page.locator(".criteria-in button")
        expect(expand).to_have_attribute("aria-expanded", "false")
        expand.click()
        expect(expand).to_have_attribute("aria-expanded", "true")

        changes = page.get_by_role("tab", name=re.compile("Changes"))
        changes.click()
        assert "tab=changes" in page.url
        changes.press("ArrowRight")
        expect(page.get_by_role("tab", name="History")).to_have_attribute("aria-selected", "true")
        assert "tab=history" in page.url and "event=" not in page.url

        page.goto("/trials/NCT/NCT00000001")
        page.emulate_media(media="print")
        expect(page.locator(".trial-print-meta")).to_be_visible()
        expect(page.locator(".shell-sidebar")).to_be_hidden()


class TestDataSources:
    """Phase 3F operational visibility is a real SPA/backend journey."""

    def test_data_sources_shows_operational_registry_state(self, page):
        page.goto("/data-sources")
        expect(page.get_by_role("heading", name="Data Sources")).to_be_visible()
        expect(page.get_by_text("ClinicalTrials.gov")).to_be_visible()
        expect(page.get_by_text("WHO ICTRP")).to_be_visible()
        expect(page.locator("dt", has_text="Last successful sync").first).to_be_visible()


class TestIntelligence:
    """Real API and browser acceptance for structured Phase 3G intelligence."""

    def test_dashboard_intelligence_opens_briefing(self, page):
        page.goto("/dashboard")
        expect(page.get_by_role("heading", name="What Changed")).to_be_visible()
        expect(page.get_by_text("Changed Trials").first).to_be_visible()
        page.get_by_label("Research scope").select_option(label="Monitor · Heart failure devices")
        expect(page.get_by_text("Critical Changes").first).to_be_visible()
        page.get_by_role("button", name="Research Briefing").click()
        page.wait_for_url("**/briefing?scope=monitor**")
        expect(page.get_by_role("heading", name="Intelligence")).to_be_visible()

    def test_briefing_comparison_drilldown_and_freshness_recovery(self, page, tmp_path):
        db_path = tmp_path / "e2e.db"
        conn = sqlite3.connect(db_path)
        now = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
        record_id = conn.execute("SELECT record_id FROM registry_records WHERE source_trial_id='NCT00000001' AND is_latest=1").fetchone()[0]
        event_hash = hashlib.sha256(f"briefing:status:{record_id}:{now}".encode()).hexdigest()
        event_id = conn.execute("""INSERT INTO trial_events
            (record_id,field_name,old_value,new_value,change_category,change_type,severity,event_hash,detected_at)
            VALUES (?,'status_id','Recruiting','Completed','status','modified','important',?,?)""",
            (record_id, event_hash, now)).lastrowid
        conn.execute("INSERT INTO monitor_events (monitor_id,trial_id,event_type,related_change_event_id,detected_at) VALUES (1,'NCT00000001','trial_changed',?,?)", (event_id, now))
        conn.execute("INSERT INTO monitor_events (monitor_id,trial_id,event_type,detected_at) VALUES (1,'NCT00000003','trial_entered',?)", (now,))
        conn.execute("INSERT INTO monitor_trials (monitor_id,trial_id,currently_matches) VALUES (1,'NCT00000003',1)")
        second = conn.execute("INSERT INTO monitors (name,enabled) VALUES ('Second monitor',1)").lastrowid
        conn.execute("INSERT INTO monitor_trials (monitor_id,trial_id,currently_matches) VALUES (?, 'NCT00000001',1)", (second,))
        conn.execute("INSERT INTO monitor_events (monitor_id,trial_id,event_type,related_change_event_id,detected_at) VALUES (?,'NCT00000001','trial_changed',?,?)", (second, event_id, now))
        conn.execute("INSERT INTO monitor_events (monitor_id,trial_id,event_type,detected_at) VALUES (?,'NCT00000003','trial_left',?)", (second, now))
        conn.execute("UPDATE registry_sources SET enabled=1 WHERE short_name='NCT'")
        # Genuinely stale: no sync cursor and no re-verified record since 09-01
        # (the shared seed includes a record crawled today, which alone proves
        # the library holds current NCT data).
        conn.execute("UPDATE registry_records SET last_crawled_at='2026-08-01 00:00:00' "
                     "WHERE source_id=(SELECT source_id FROM registry_sources WHERE short_name='NCT')")
        conn.execute("INSERT INTO sync_status (source_id,last_successful_sync) VALUES ((SELECT source_id FROM registry_sources WHERE short_name='NCT'),'2026-09-01 00:00:00')")
        conn.commit(); conn.close()

        page.goto("/briefing?scope=monitor&monitor=1&window=7d&registry=clinicaltrials_gov")
        expect(page.get_by_role("heading", name="Intelligence")).to_be_visible()
        expect(page.get_by_text("Data completeness warning")).to_be_visible()
        expect(page.get_by_role("heading", name="Recruiting → Completed (1)")).to_be_visible()
        expect(page.get_by_text("New / Entered Trials", exact=False)).to_be_visible()
        expect(page.get_by_text("Enrollment / Timeline Changes", exact=False)).to_be_visible()
        expect(page.locator(".trend-day").first).to_be_visible()
        assert page.locator(".trend-day").count() >= 2
        category = page.locator(".briefing-cat-row").first
        expect(category).to_be_visible()
        expect(category).to_have_attribute("aria-expanded", "false")
        category.click()
        expect(category).to_have_attribute("aria-expanded", "true")
        expect(page.locator(".briefing-category-detail-panel")).to_be_visible()
        expect(page.locator(".briefing-event").first).to_be_visible()
        page.locator(".briefing-event").first.click()
        page.wait_for_url("**tab=changes&event=**")
        expect(page.locator(".change-row-target")).to_be_visible()

        page.goto("/briefing?view=monitors&window=7d&registry=clinicaltrials_gov")
        first_row = page.get_by_role("row").filter(has_text="Heart failure devices")
        second_row = page.get_by_role("row").filter(has_text="Second monitor")
        expect(first_row.locator("td").nth(1)).to_have_text("2")
        expect(second_row.locator("td").nth(1)).to_have_text("1")
        assert first_row.inner_text() != second_row.inner_text()
        page.get_by_role("button", name="Heart failure devices").click()
        page.wait_for_url("**scope=monitor**")
        expect(page.get_by_text("Data completeness warning")).to_be_visible()

        conn = sqlite3.connect(db_path)
        # Enrich-style recovery: records re-verified now while the sync cursor
        # stays old — the exact case that used to false-positive as stale.
        conn.execute("UPDATE registry_records SET last_crawled_at=? "
                     "WHERE source_id=(SELECT source_id FROM registry_sources WHERE short_name='NCT')", (now,))
        conn.commit(); conn.close()
        page.reload()
        expect(page.get_by_text("Data completeness warning")).to_have_count(0)

        page.goto("/briefing?scope=watched&window=7d&registry=clinicaltrials_gov")
        expect(page.get_by_role("heading", name="Executive Summary")).to_be_visible()
        page.goto("/briefing?scope=monitored&window=7d&registry=clinicaltrials_gov")
        expect(page.get_by_role("heading", name="Executive Summary")).to_be_visible()

        # The same underlying event belongs to two monitors but appears once
        # in all-monitored trial-change intelligence.
        payload = page.request.get("/api/intelligence/overview?scope=monitored&window=7d&registry=clinicaltrials_gov").json()
        assert sum(e["event_id"] == event_id for e in payload["priority_events"]) == 1

    def test_scoped_updates_and_notification_link(self, page):
        page.goto("/updates?scope=monitored&window=all")
        expect(page.get_by_text("All Monitored Topics", exact=True)).to_be_visible()
        expect(page.get_by_text("Heart failure device trial in adults").first).to_be_visible()

        page.goto("/updates?scope=watched&window=all")
        expect(page.get_by_text("Watched Trial Updates")).to_be_visible()
        expect(page.get_by_text("Heart failure device trial in adults").first).to_be_visible()

        page.goto("/updates?view=notifications")
        page.get_by_role("button", name="Heart failure device trial in adults").click()
        page.wait_for_url("**tab=changes&event=**")
        expect(page.locator(".change-row-target")).to_be_visible()


class TestPhase3KWorkspace:
    def test_workspace_labels_follow_language(self, page):
        page.goto("/dashboard")
        page.get_by_role("button", name="Toggle language").click()
        expect(page.get_by_role("heading", name="试验活动趋势")).to_be_visible()
        expect(page.get_by_label("研究范围")).to_be_visible()
        page.goto("/updates")
        expect(page.get_by_role("heading", name="变化类别")).to_be_visible()
        expect(page.get_by_role("combobox", name="类别")).to_be_visible()
        project = page.request.post("/api/projects", data={"name": "Myocarditis"}).json()["project"]
        page.goto(f"/projects/{project['id']}")
        expect(page.get_by_role("heading", name="研究设置")).to_be_visible()
        expect(page.get_by_role("tab", name="活动")).to_be_visible()

    def test_sidebar_search_scope_and_mobile_drawer(self, page):
        page.goto("/dashboard")
        expect(page.get_by_role("navigation", name="Primary navigation").get_by_role("link", name="Dashboard")).to_have_attribute("aria-current", "page")
        for label in ("Trials", "Projects", "Saved Searches", "Monitors", "Updates", "Research Briefing", "Data Sources", "Guide & Releases"):
            expect(page.get_by_role("navigation", name="Primary navigation").get_by_role("link", name=re.compile(label))).to_be_visible()
        assert page.get_by_role("navigation", name="Primary navigation").get_by_role("link", name="Watched Trials").count() == 0
        assert page.get_by_role("navigation", name="Primary navigation").get_by_role("link", name="Notifications").count() == 0
        expect(page.get_by_role("heading", name="All Monitored Topics")).to_be_visible()
        page.get_by_label("Research scope").select_option(label="Monitor · Heart failure devices")
        expect(page.get_by_role("heading", name="Heart failure devices")).to_be_visible()
        assert "scope=monitor" in page.url and "monitor_id=1" in page.url
        page.go_back()
        expect(page.get_by_role("heading", name="All Monitored Topics")).to_be_visible()
        page.go_forward()
        expect(page.get_by_role("heading", name="Heart failure devices")).to_be_visible()
        page.reload()
        expect(page.get_by_role("heading", name="Heart failure devices")).to_be_visible()
        page.get_by_role("textbox", name="Search trials").fill("myocarditis")
        page.get_by_role("button", name="Search", exact=True).click()
        page.wait_for_url("**/trials?q=myocarditis")
        page.set_viewport_size({"width": 390, "height": 844})
        page.get_by_role("button", name="Open navigation").click()
        expect(page.get_by_role("navigation", name="Primary navigation").get_by_role("link", name="Projects")).to_be_visible()
        page.get_by_role("navigation", name="Primary navigation").get_by_role("link", name="Updates").click()
        page.wait_for_url("**/updates")
        expect(page.get_by_role("heading", name="All Monitored Topics")).to_be_visible()
        page.goto("/trials?view=watched")
        expect(page.get_by_role("heading", name="Watched Trials")).to_be_visible()
        page.goto("/updates?view=notifications")
        expect(page.get_by_role("heading", name="Notifications")).to_be_visible()

    def test_phase1_shell_navigation_search_and_responsive_state(self, page):
        page.goto("/trials/NCT/NCT00000001")
        expect(page.get_by_role("navigation", name="Primary navigation").get_by_role("link", name="Trials")).to_have_attribute("aria-current", "page")

        page.get_by_role("button", name="Notifications").click()
        page.wait_for_url("**/updates?view=notifications")
        expect(page.get_by_role("navigation", name="Primary navigation").get_by_role("link", name="Updates")).to_have_attribute("aria-current", "page")

        page.goto("/dashboard")
        keyword_mode = page.get_by_role("radio", name="Keyword")
        keyword_mode.focus()
        page.keyboard.press("ArrowRight")
        expect(page.get_by_role("radio", name="Ask naturally")).to_have_attribute("aria-checked", "true")
        page.keyboard.press("Home")
        expect(keyword_mode).to_have_attribute("aria-checked", "true")
        page.get_by_role("button", name="Collapse sidebar").click()
        expect(page.locator(".shell")).to_have_class(re.compile("shell-is-collapsed"))
        page.reload()
        expect(page.locator(".shell")).to_have_class(re.compile("shell-is-collapsed"))
        expect(page.get_by_role("navigation", name="Primary navigation").get_by_role("link", name="Trials")).to_have_attribute("title", "Trials")

        page.get_by_role("textbox", name="Search trials").fill("heart & lung")
        page.get_by_role("button", name="Search", exact=True).click()
        page.wait_for_url("**/trials?q=heart%20%26%20lung")
        page.get_by_role("radio", name="Ask naturally").click()
        page.get_by_role("textbox", name="Ask naturally across trials").fill("recruiting heart trials")
        page.get_by_role("button", name="Search", exact=True).click()
        page.wait_for_url("**/trials?ask=recruiting%20heart%20trials")

        page.set_viewport_size({"width": 375, "height": 812})
        menu = page.get_by_role("button", name="Open navigation")
        menu.click()
        expect(page.get_by_role("dialog", name="Navigation")).to_be_visible()
        page.keyboard.press("Escape")
        expect(page.get_by_role("dialog", name="Navigation")).to_have_count(0)
        expect(menu).to_be_focused()
        assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")

        page.get_by_role("button", name="Use dark theme").click()
        assert page.evaluate("document.documentElement.dataset.theme") == "dark"
        page.get_by_role("button", name="Toggle language").click()
        assert page.evaluate("document.documentElement.lang") == "zh-CN"

    def test_dashboard_and_updates_analytics_precede_feed(self, page):
        page.goto("/dashboard")
        expect(page.locator(".kpi-strip").first).to_be_visible()
        expect(page.get_by_role("heading", name="Priority changes")).to_be_visible()
        expect(page.get_by_role("heading", name="Trial Activity Trend")).to_be_visible()
        expect(page.get_by_role("heading", name="What Changed")).to_be_visible()
        assert page.get_by_role("heading", name="Priority changes").bounding_box()["y"] < page.get_by_role("heading", name="Trial Activity Trend").bounding_box()["y"]
        page.goto("/updates")
        expect(page.get_by_role("heading", name="Trial Activity Trend")).to_be_visible()
        expect(page.get_by_role("heading", name="Changes by Category")).to_be_visible()
        assert page.get_by_role("heading", name="Changes by Category").bounding_box()["y"] < page.get_by_role("heading", name="Detailed Changes").bounding_box()["y"]
        page.get_by_role("combobox", name="Severity").select_option("priority")
        assert "severity=priority" in page.url
        page.get_by_role("combobox", name="Category").select_option("outcomes")
        assert "category=outcomes" in page.url
        page.reload()
        expect(page.get_by_role("combobox", name="Category")).to_have_value("outcomes")
        page.get_by_role("combobox", name="Update scope").select_option("watched")
        page.reload()
        expect(page.get_by_role("heading", name="Watched Trial Updates")).to_be_visible()

    def test_project_five_tabs_and_legacy_urls(self, page):
        project = page.request.post("/api/projects", data={"name": "Myocarditis", "description": "Research context"}).json()["project"]
        page.goto(f"/projects/{project['id']}")
        expect(page.get_by_role("heading", name="Myocarditis")).to_be_visible()
        assert page.get_by_role("tablist", name="Project views").get_by_role("tab").count() == 5
        expect(page.get_by_role("heading", name="Research Setup")).to_be_visible()
        for legacy, selected in (("searches", "Overview"), ("monitors", "Overview"), ("updates", "Activity"), ("briefing", "Intelligence")):
            page.goto(f"/projects/{project['id']}?tab={legacy}")
            expect(page.get_by_role("tab", name=selected)).to_have_attribute("aria-selected", "true")
        page.get_by_role("tab", name="Activity").click()
        page.get_by_role("combobox", name="Time window").select_option("30d")
        assert "window=30d" in page.url
        page.get_by_role("combobox", name="Category").select_option("outcomes")
        assert "category=outcomes" in page.url
        page.reload()
        expect(page.get_by_role("combobox", name="Time window")).to_have_value("30d")
        expect(page.get_by_role("combobox", name="Category")).to_have_value("outcomes")
        page.get_by_role("tab", name="Trials").click()
        for label in ("Curated", "Monitor matches", "Watched curated"):
            expect(page.get_by_role("radio", name=label, exact=True)).to_be_visible()


class TestPhase3IntelligenceWorkflow:
    def test_dashboard_window_category_and_freshness(self, page):
        page.goto("/dashboard?scope=monitored&window=7d")
        page.get_by_role("combobox", name="Time window").select_option("30d")
        assert "window=30d" in page.url
        page.get_by_role("button", name=re.compile("Outcomes")).click()
        assert "/updates" in page.url and "category=outcomes" in page.url and "window=30d" in page.url
        page.go_back()
        page.get_by_role("button", name=re.compile("Source freshness")).click()
        expect(page.get_by_role("button", name="View data sources")).to_be_visible()

    def test_updates_grouping_chips_clear_and_pagination_state(self, page):
        page.goto("/updates?scope=all&window=all&severity=critical&category=outcomes")
        expect(page.locator(".date-change-group").first).to_be_visible()
        expect(page.locator(".trial-change-group").first).to_contain_text("ClinicalTrials.gov")
        expect(page.get_by_role("button", name=re.compile("severity: critical"))).to_be_visible()
        page.get_by_role("button", name="Clear filters").click()
        assert "severity=" not in page.url and "category=" not in page.url
        assert "scope=all" in page.url and "window=all" in page.url

    def test_updates_long_change_expansion_and_deep_link(self, page):
        page.goto("/updates?scope=all&window=all&category=outcomes")
        row = page.locator(".intel-change-row").first
        expect(row.get_by_role("link", name="Heart failure device trial in adults")).to_be_visible()
        row.get_by_role("button", name=re.compile("Critical Heart failure")).click()
        expect(page).to_have_url(re.compile(r"/trials/NCT/NCT00000001\?tab=changes&event="))

    def test_notification_read_unread_and_shell_count(self, page):
        page.goto("/updates?view=notifications")
        expect(page.get_by_text("Unread", exact=True).first).to_be_visible()
        page.get_by_role("button", name="Mark read").click()
        expect(page.get_by_role("button", name="Mark unread")).to_be_visible()
        page.get_by_role("button", name="Mark unread").click()
        expect(page.get_by_role("button", name="Mark read")).to_be_visible()

    def test_mobile_filter_drawer_escape_and_focus(self, page):
        page.set_viewport_size({"width": 375, "height": 812})
        page.goto("/updates?scope=all&window=all")
        trigger = page.get_by_role("button", name="Open filters")
        trigger.click()
        expect(page.get_by_role("dialog", name="Filters")).to_be_visible()
        page.keyboard.press("Escape")
        expect(page.get_by_role("dialog", name="Filters")).to_have_count(0)
        expect(trigger).to_be_focused()
        assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")

    def test_briefing_hierarchy_category_comparison_and_print(self, page):
        page.goto("/briefing?scope=monitored&window=7d")
        expect(page.get_by_role("heading", name="Executive Summary")).to_be_visible()
        expect(page.get_by_role("heading", name="Detailed findings")).to_be_visible()
        page.get_by_role("button", name=re.compile("Outcomes")).click()
        expect(page.locator("#briefing-category-detail")).to_be_visible()
        page.get_by_role("tab", name="Monitor comparison").click()
        expect(page.get_by_text(re.compile("monitors compared"))).to_be_visible()
        assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
        page.emulate_media(media="print")
        assert page.locator(".shell-sidebar").evaluate("el => getComputedStyle(el).display") == "none"


class TestProductionReadiness:
    def test_production_health_ready_and_headers(self, browser, production_server):
        context = browser.new_context(base_url=production_server)
        page = context.new_page()
        try:
            page.goto("/dashboard")
            expect(page.get_by_role("heading", name="All Monitored Topics")).to_be_visible()
            ready = page.request.get("/api/ready")
            assert ready.status == 200
            assert ready.json()["schema_version"] == 20
            assert ready.headers["x-frame-options"] == "DENY"
            assert page.request.get("/api/docs").status == 404
        finally:
            context.close()

    def test_note_html_is_rendered_as_text(self, browser, production_server):
        context = browser.new_context(base_url=production_server)
        page = context.new_page()
        try:
            project = page.request.post("/api/projects", data={"name": "XSS fixture"}).json()["project"]
            payload = '<img src=x onerror="window.__xss=1"><script>window.__xss=2</script>'
            response = page.request.post(f"/api/projects/{project['id']}/notes", data={"body": payload})
            assert response.status == 201
            page.goto(f"/projects/{project['id']}?tab=notebook")
            expect(page.get_by_text(payload)).to_be_visible()
            assert page.evaluate("window.__xss") is None
        finally:
            context.close()
