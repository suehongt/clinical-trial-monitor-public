"""collectors/waf_http.py 离线测试：solver 回归向量 + WafHttpClient 状态机。

全程离线、零网络：requests.Session 由假会话替换，响应按序回放。
向量与行为依据 docs/data-source-comparison.md 2026-09-12 实测：
挑战页 200 + ``var arg1``、acw_sc__v2 即通行证、405 为会话级封禁、
新版挑战（aliyun_waf_aa/bb）离线不可解。
"""
from __future__ import annotations

import re

import pytest
import requests

from collectors.waf_http import (WafChallengeNewVersion, WafHttpClient,
                                 solve_acw_sc_v2)
from config import CONFIG
from core.waf_guard import WafCircuitOpenError

# ── 测试数据 ─────────────────────────────────────────────────────────

ARG1 = "0123456789ABCDEF0123456789ABCDEF01234567"
TOKEN = "d2c7186598ab1a508a4f6064e4fa746323ab17c6"  # 固定向量（防表/key 漂移）

CHALLENGE_PAGE = (
    "<html><script>var arg1='8D29199CB5A94751CF00EF45D3A0A9B579A8B72B';"
    "function reload(x){setCookie('acw_sc__v2',x);document.location.reload();}"
    "</script></html>"
)
NEW_VERSION_PAGE = (
    "<html><head>"
    '<meta name="aliyun_waf_aa" content="abc">'
    '<meta name="aliyun_waf_bb" content="def">'
    "</head><body></body></html>"
)
BLOCK_PAGE = "<html><title>405</title>blocked</html>"
CONTENT_PAGE = "<html><body>ChiCTR2600131888 注册号 table1</body></html>"


def _content_ok(html: str) -> bool:
    """与 ChiCTR marker 同构的极简内容判定。"""
    return "ChiCTR" in html and "arg1" not in html


class FakeResponse:
    """假 HTTP 响应。"""

    def __init__(self, status_code: int, text: str):
        self.status_code = status_code
        self.text = text


class FakeSession:
    """假 requests.Session：按序回放响应，记录 cookie 与调用。"""

    def __init__(self, responses):
        self._responses = list(responses)
        self.cookies = FakeCookieJar()
        self.headers: dict = {}
        self.proxies: dict = {}
        self.calls: list = []

    def get(self, url, timeout=None):
        self.calls.append(url)
        if not self._responses:
            raise AssertionError("FakeSession 响应耗尽（测试设计错误）")
        return self._responses.pop(0)


class FakeCookieJar:
    """极简 cookie 容器（set/clear/items 语义对齐 requests）。"""

    def __init__(self):
        self._data: dict = {}

    def set(self, name, value):
        self._data[name] = value

    def clear(self):
        self._data.clear()

    def __contains__(self, name):
        return name in self._data

    def __getitem__(self, name):
        return self._data[name]

    def get(self, name, default=None):
        return self._data.get(name, default)

    def __bool__(self):
        return bool(self._data)


def make_client(responses, threshold=3) -> tuple[WafHttpClient, FakeSession]:
    """构造带假会话的客户端（熔断阈值 3、节律地板 0，避免测试休眠）。"""
    CONFIG.waf.circuit_breaker_threshold = threshold
    CONFIG.waf.min_request_interval_sec = 0.0
    client = WafHttpClient("chictr-test", content_ok=_content_ok,
                           user_agent="test-ua", timeout_ms=1000)
    session = FakeSession(responses)
    client.session = session
    return client, session


# ── solve_acw_sc_v2 ──────────────────────────────────────────────────

def test_solve_fixed_vector():
    """固定向量：置换表或异或 key 漂移会立刻破坏该断言。"""
    assert solve_acw_sc_v2(ARG1) == TOKEN


def test_solve_fixture_arg1():
    """用真实捕获的挑战页 arg1 做第二个回归向量（40 位十六进制输出）。"""
    fixture = open("tests/fixtures/chictr_waf_challenge.html").read()
    arg1 = re.search(r"var arg1='([0-9A-Fa-f]{40})'", fixture).group(1)
    token = solve_acw_sc_v2(arg1)
    assert arg1 == "8D29199CB5A94751CF00EF45D3A0A9B579A8B72B"
    assert token == "6aa566e75b8edaf2d28f2e5f4e50b099e6a098ed"
    assert re.fullmatch(r"[0-9a-f]{40}", token)


def test_solve_rejects_bad_arg1():
    """非 40 位十六进制输入必须拒绝（防把整页 HTML 当 arg1）。"""
    for bad in ("", "short", "zz" * 20, "8D29" * 9):  # 9×4=36 位
        with pytest.raises(ValueError):
            solve_acw_sc_v2(bad)


# ── WafHttpClient 状态机 ─────────────────────────────────────────────

def test_challenge_solved_then_content():
    """挑战页 → 离线求解 → 带 cookie 重试 → 内容；恰好 2 次请求。"""
    client, session = make_client(
        [FakeResponse(200, CHALLENGE_PAGE), FakeResponse(200, CONTENT_PAGE)])
    html = client.get("https://x/searchproj.html")
    assert html == CONTENT_PAGE
    assert session.cookies.get("acw_sc__v2") == solve_acw_sc_v2(
        "8D29199CB5A94751CF00EF45D3A0A9B579A8B72B")
    assert len(session.calls) == 2
    assert client.solved_count == 1
    assert client.guard.waf_streak == 0


def test_content_first_pass_no_challenge():
    """直接拿到内容：单请求返回，无求解。"""
    client, session = make_client([FakeResponse(200, CONTENT_PAGE)])
    assert client.get("https://x/") == CONTENT_PAGE
    assert len(session.calls) == 1
    assert client.solved_count == 0


def test_new_version_challenge_raises():
    """新版挑战（aliyun_waf_aa/bb）→ 抛 WafChallengeNewVersion。"""
    client, _ = make_client([FakeResponse(200, NEW_VERSION_PAGE)])
    with pytest.raises(WafChallengeNewVersion):
        client.get("https://x/searchproj.html")


def test_405_trips_circuit_then_fast_fails():
    """连续 405 达阈值 → 熔断异常；后续 get 零网络请求快速失败。"""
    responses = [FakeResponse(405, BLOCK_PAGE) for _ in range(5)]
    client, session = make_client(responses, threshold=3)
    with pytest.raises(WafCircuitOpenError):
        client.get("https://x/searchproj.html")
    assert client.guard.circuit_open is True
    calls_after_open = len(session.calls)
    with pytest.raises(WafCircuitOpenError):
        client.get("https://x/searchproj.html")
    assert len(session.calls) == calls_after_open  # 零额外网络请求


def test_405_recovers_via_fresh_challenge():
    """405 → 清 cookie → 重获挑战 → 求解 → 内容（会话级恢复路径）。"""
    client, session = make_client([
        FakeResponse(405, BLOCK_PAGE),
        FakeResponse(200, CHALLENGE_PAGE),
        FakeResponse(200, CONTENT_PAGE),
    ])
    session.cookies.set("acw_sc__v2", "poisoned")
    html = client.get("https://x/showproj.html?proj=1")
    assert html == CONTENT_PAGE
    assert client.blocked_count == 1
    assert session.cookies.get("acw_sc__v2") == solve_acw_sc_v2(
        "8D29199CB5A94751CF00EF45D3A0A9B579A8B72B")


def test_success_clears_streak():
    """成功内容页清零连续计数：405→内容 反复恢复，不触发熔断。"""
    client, _ = make_client([
        FakeResponse(405, BLOCK_PAGE),
        FakeResponse(200, CONTENT_PAGE),
        FakeResponse(405, BLOCK_PAGE),
        FakeResponse(200, CONTENT_PAGE),
    ], threshold=3)
    assert client.get("https://x/a") == CONTENT_PAGE
    assert client.get("https://x/b") == CONTENT_PAGE
    assert client.guard.circuit_open is False
    assert client.guard.waf_streak == 0


def test_unknown_200_page_counts_as_waf():
    """非 405、无 arg1、无内容的 200 页按 WAF 页计 streak。"""
    client, _ = make_client([
        FakeResponse(200, "<html>mystery</html>"),
        FakeResponse(200, CONTENT_PAGE),
    ])
    assert client.get("https://x/a") == CONTENT_PAGE
    assert client.guard.waf_streak == 0


def test_attempts_exhausted_returns_none():
    """反复下发挑战页：3 次尝试后返回 None（与 _fetch_page 契约一致）。"""
    client, session = make_client(
        [FakeResponse(200, CHALLENGE_PAGE) for _ in range(3)])
    assert client.get("https://x/a") is None
    assert len(session.calls) == 3


def test_request_exception_then_content():
    """网络异常不计 WAF streak，重试拿到内容。"""
    client, session = make_client([FakeResponse(200, CONTENT_PAGE)])
    real_get = session.get

    state = {"n": 0}

    def flaky_get(url, timeout=None):
        state["n"] += 1
        if state["n"] == 1:
            raise requests.ConnectionError("boom")
        return real_get(url, timeout=timeout)

    session.get = flaky_get
    assert client.get("https://x/a") == CONTENT_PAGE
    assert len(session.calls) == 1
    assert client.guard.waf_streak == 0


def test_proxy_config_applied(monkeypatch):
    """CT_WAF_PROXY_URL 非空时代理透传到 requests 会话。"""
    monkeypatch.setattr(CONFIG.waf, "proxy_url", "http://127.0.0.1:9")
    client = WafHttpClient("chictr-test", content_ok=_content_ok,
                           user_agent="test-ua")
    assert client.session.proxies["https"] == "http://127.0.0.1:9"


def test_no_proxy_by_default():
    """默认（无代理）时不得设置 proxies。"""
    client = WafHttpClient("chictr-test", content_ok=_content_ok,
                           user_agent="test-ua")
    assert client.session.proxies == {}


def test_min_request_interval_floor(monkeypatch):
    """节律地板：相邻请求间隔不足 floor 时补足等待（节律守恒不变量）。"""
    client, session = make_client([
        FakeResponse(200, CONTENT_PAGE),
        FakeResponse(200, CONTENT_PAGE),
    ])
    monkeypatch.setattr(CONFIG.waf, "min_request_interval_sec", 0.3)
    sleeps: list = []

    def fake_sleep(sec):
        sleeps.append(sec)

    monkeypatch.setattr("collectors.waf_http.time.sleep", fake_sleep)
    assert client.get("https://x/a") == CONTENT_PAGE
    assert client.get("https://x/b") == CONTENT_PAGE
    # 第二次 get 时距上次请求 < 0.3s → 补睡
    assert any(s > 0 for s in sleeps)
    assert all(s <= 0.35 for s in sleeps)  # 补足量不超过地板本身


# ── 连续失败熔断（fetch_failure） ────────────────────────────────────

def test_failure_breaker_trips_on_consecutive_exhaustion(monkeypatch):
    """连续多次「尝试耗尽未取得内容」（全网络异常）→ 打开熔断并快速失败。"""
    client, session = make_client([], threshold=0)  # 禁用 WAF 页熔断隔离本测
    monkeypatch.setattr(CONFIG.waf, "failure_breaker_threshold", 3)

    def always_raise(url, timeout=None):
        raise requests.ConnectionError("station down")

    session.get = always_raise
    for i in range(2):
        assert client.get("https://x/a") is None  # 每次内部 3 连异常 → 耗尽
        assert client.guard.circuit_open is False
        assert client.guard.failure_streak == i + 1
    with pytest.raises(WafCircuitOpenError) as excinfo:
        client.get("https://x/a")  # 第 3 次耗尽 → 失败熔断打开
    assert excinfo.value.reason == "fetch_failure"
    assert "fetch failures" in str(excinfo.value)
    assert client.guard.circuit_open is True
    with pytest.raises(WafCircuitOpenError) as fast_exc:
        client.get("https://x/a")  # 快速失败（零网络请求）
    assert fast_exc.value.reason == "fetch_failure"
    assert fast_exc.value.consecutive == 3


def test_success_resets_failure_streak(monkeypatch):
    """成功抓取同时清零失败计数：耗尽→成功→耗尽 不触发熔断。"""
    monkeypatch.setattr(CONFIG.waf, "failure_breaker_threshold", 3)
    client, session = make_client([
        FakeResponse(200, CONTENT_PAGE),
        FakeResponse(200, CONTENT_PAGE),
    ])

    original_get = session.get

    def fail_first_call(url, timeout=None):
        raise requests.ConnectionError("station down")

    session.get = fail_first_call
    assert client.get("https://x/a") is None          # 耗尽 → failure_streak=1
    assert client.guard.circuit_open is False
    session.get = original_get
    assert client.get("https://x/b") == CONTENT_PAGE  # 成功 → 清零
    assert client.guard.failure_streak == 0
    session.get = fail_first_call
    assert client.get("https://x/c") is None          # 再耗尽 → streak=1
    assert client.guard.circuit_open is False         # 未达阈值 3
    assert client.guard.failure_streak == 1
