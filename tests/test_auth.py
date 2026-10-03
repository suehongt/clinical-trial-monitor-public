"""Basic-auth gate tests (server/app.py basic_auth_guard).

The gate is environment-driven: with CT_AUTH_USER / CT_AUTH_PASSWORD unset
the app behaves exactly as before — every existing contract test runs
without credentials and must keep passing (pinned here by
test_auth_off_by_default).  With both set, every path except the
/api/health liveness probe requires matching HTTP Basic credentials.

The fixture mirrors tests/test_server_api.py's use of the shared ``test_db``
fixture (temp schema, no seed data needed — auth gates before any data is
touched).
"""
from __future__ import annotations

import base64
from pathlib import Path

import pytest

SPA_INDEX = Path(__file__).resolve().parent.parent / "web" / "dist" / "index.html"


def _basic_header(user: str, password: str) -> dict:
    token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
    return {"Authorization": f"Basic {token}"}


@pytest.fixture
def client(test_db, monkeypatch):
    import ct_report.query as qmod
    from config import CONFIG
    monkeypatch.setattr(qmod, "DB_PATH", CONFIG.db.path)

    from fastapi.testclient import TestClient
    from server.app import create_app

    return TestClient(create_app())


def test_auth_off_by_default(client):
    """No CT_AUTH_* env — requests pass unauthenticated, as always."""
    assert client.get("/api/profiles").status_code == 200
    assert client.get("/api/ready").status_code == 200


def test_rejects_without_credentials(client, monkeypatch):
    monkeypatch.setenv("CT_AUTH_USER", "rango")
    monkeypatch.setenv("CT_AUTH_PASSWORD", "s3cret")
    resp = client.get("/api/profiles")
    assert resp.status_code == 401
    assert resp.headers["www-authenticate"].startswith('Basic realm="ct-monitor"')
    assert resp.json()["error"]["code"] == "UNAUTHORIZED"
    assert resp.headers["x-content-type-options"] == "nosniff"


def test_rejects_wrong_credentials(client, monkeypatch):
    monkeypatch.setenv("CT_AUTH_USER", "rango")
    monkeypatch.setenv("CT_AUTH_PASSWORD", "s3cret")
    assert client.get("/api/profiles", headers=_basic_header("rango", "wrong")).status_code == 401
    assert client.get("/api/profiles", headers=_basic_header("other", "s3cret")).status_code == 401


def test_accepts_correct_credentials(client, monkeypatch):
    monkeypatch.setenv("CT_AUTH_USER", "rango")
    monkeypatch.setenv("CT_AUTH_PASSWORD", "s3cret")
    assert client.get("/api/profiles", headers=_basic_header("rango", "s3cret")).status_code == 200


def test_health_probe_stays_open(client, monkeypatch):
    """The liveness probe must stay reachable for uptime checks without
    storing credentials — that is why it alone is exempt."""
    monkeypatch.setenv("CT_AUTH_USER", "rango")
    monkeypatch.setenv("CT_AUTH_PASSWORD", "s3cret")
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_malformed_authorization_headers_rejected(client, monkeypatch):
    monkeypatch.setenv("CT_AUTH_USER", "rango")
    monkeypatch.setenv("CT_AUTH_PASSWORD", "s3cret")
    assert client.get("/api/profiles", headers={"Authorization": "Bearer abc"}).status_code == 401
    assert client.get("/api/profiles", headers={"Authorization": "Basic !!!notb64"}).status_code == 401
    assert client.get("/api/profiles", headers={"Authorization": "Basic "}).status_code == 401


@pytest.mark.skipif(not SPA_INDEX.exists(), reason="web/dist not built")
def test_spa_gated_too(client, monkeypatch):
    """The static viewer ships no secrets but still sits behind the gate —
    one prompt covers both the SPA and the API (same browser realm)."""
    monkeypatch.setenv("CT_AUTH_USER", "rango")
    monkeypatch.setenv("CT_AUTH_PASSWORD", "s3cret")
    assert client.get("/").status_code == 401
    assert client.get("/", headers=_basic_header("rango", "s3cret")).status_code == 200


# ── CT_PUBLIC_READONLY gate (public demo box, Basic auth switched off) ────

def test_readonly_off_by_default(client):
    """Without the flag the app is fully writable, as always."""
    assert client.post("/api/projects", json={"name": "t", "description": ""}).status_code == 201


def test_readonly_blocks_mutations(client, monkeypatch):
    monkeypatch.setenv("CT_PUBLIC_READONLY", "1")
    resp = client.post("/api/projects", json={"name": "t", "description": ""})
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "FORBIDDEN_READONLY"
    assert resp.headers["x-content-type-options"] == "nosniff"
    # every other mutating method too — especially the crawl triggers
    assert client.post("/api/trials/live-check", json={"q": "x"}).status_code == 403
    assert client.post("/api/registries/chictr/sync").status_code == 403
    assert client.delete("/api/watches/1").status_code == 403


def test_readonly_allows_reads(client, monkeypatch):
    monkeypatch.setenv("CT_PUBLIC_READONLY", "1")
    assert client.get("/api/profiles").status_code == 200
    assert client.get("/api/health").status_code == 200
    # Local ICTRP mirror detail is a database read, not an outbound adapter.
    assert client.get("/api/ictrp/studies/NCT00000001").status_code == 404


def test_readonly_whitelists_browse_support(client, monkeypatch):
    """The two POSTs a read-only visitor still needs: the stateless local
    search interpreter behind "Ask naturally", and opening a saved search
    (last_opened_at only)."""
    monkeypatch.setenv("CT_PUBLIC_READONLY", "1")
    # gate test, not interpreter-vocabulary test: anything but 403 is fine
    resp = client.post("/api/search/interpret", json={"text": "breast cancer phase 3"})
    assert resp.status_code != 403
    # no such saved search here — 404 proves the gate let it through
    resp = client.post("/api/saved-searches/999/open")
    assert resp.status_code == 404


def test_readonly_flag_variants(client, monkeypatch):
    for value in ("true", "yes", "1", " 1 "):
        monkeypatch.setenv("CT_PUBLIC_READONLY", value)
        assert client.post("/api/projects", json={"name": "t", "description": ""}).status_code == 403
    for value in ("", "0", "off"):
        monkeypatch.setenv("CT_PUBLIC_READONLY", value)
        assert client.post("/api/projects", json={"name": "t", "description": ""}).status_code == 201


def test_readonly_blocks_live_registry_adapter_gets(client, monkeypatch):
    """Outbound registry adapters are unavailable on a public demo box."""
    monkeypatch.setenv("CT_PUBLIC_READONLY", "1")
    assert client.get("/api/nct/live?keywords=caffeine").status_code == 403
    assert client.get("/api/nct/study/NCT00000001").status_code == 403
    assert client.get("/api/chictr/search?q=heart").status_code == 403
    assert client.get("/api/chictr/studies/ChiCTR2600128415").status_code == 403
    assert client.get("/api/ctr/search?q=heart").status_code == 403
    assert client.get("/api/ctr/studies/CTR20262758").status_code == 403
    assert client.get("/api/ictrp/search?q=heart").status_code == 403


def test_readonly_off_keeps_nct_routes_reachable(client, monkeypatch):
    """Without the flag the NCT routes exist (validation runs before any
    outbound call, so no network happens in this test)."""
    monkeypatch.delenv("CT_PUBLIC_READONLY", raising=False)
    assert client.get("/api/nct/study/not-an-id").status_code == 422
