"""ChiCTR HTTP 热路径接线测试（waf-bypass-upgrade-plan §2.2/2.3）。

覆盖：auto 模式构建 HTTP 传输、browser_only 一键回滚、降级链三行为
（热路径优先 / 新版挑战降级浏览器 / 熔断穿透）、_fetch 缓存不变量。
全程离线：HTTP 传输与浏览器父实现均为替身。
"""
from __future__ import annotations

import pytest

from collectors.browser_base import WAFBrowserCollector
from collectors.chictr import ChiCTRCollector
from collectors.waf_http import WafChallengeNewVersion
from config import CONFIG
from core.waf_guard import WafCircuitOpenError

# _fetch 缓存阈值 500 字节：内容页需超过它才能命中缓存断言
CONTENT_PAGE = "<html>ChiCTR2600131888 table1 注册号 " + "x" * 600 + "</html>"


class FakeHttp:
    """WafHttpClient 替身：固定返回值或异常，记录调用。"""

    def __init__(self, result=None, exc=None):
        self.result = result
        self.exc = exc
        self.calls: list = []

    def get(self, url):
        """记录并回放预设结果。"""
        self.calls.append(url)
        if self.exc is not None:
            raise self.exc
        return self.result


def browser_sentinel(self, url, timeout_ms=None):
    """浏览器父实现哨兵：被调用即返回标记（用于断言降级发生）。"""
    return "BROWSER_HTML"


@pytest.fixture
def collector(monkeypatch):
    """auto 模式下的 ChiCTRCollector。"""
    monkeypatch.setattr(CONFIG.waf, "challenge_mode", "auto")
    return ChiCTRCollector()


def test_auto_mode_builds_http_transport(collector):
    """challenge_mode=auto（默认）→ 线程级 WafHttpClient 可用。"""
    assert collector._http_enabled is True
    assert collector._get_http().guard is collector._guard


def test_http_circuit_is_visible_and_resettable(collector, monkeypatch):
    """HTTP/browser 共用 guard；父类公开 reset 能恢复 HTTP 热路径。"""
    monkeypatch.setattr(CONFIG.waf, "failure_breaker_threshold", 1)
    with pytest.raises(WafCircuitOpenError):
        collector._get_http().guard.register_failure()
    assert collector._circuit_open is True
    assert collector._failure_streak == 1

    collector.reset_circuit()

    assert collector._get_http().guard.circuit_open is False
    assert collector._circuit_open is False
    assert collector._failure_streak == 0
    collector._get_http().guard.ensure_closed()


def test_browser_only_skips_http(monkeypatch):
    """challenge_mode=browser_only → 一键回滚：不构建 HTTP 传输。"""
    monkeypatch.setattr(CONFIG.waf, "challenge_mode", "browser_only")
    c = ChiCTRCollector()
    assert c._http_enabled is False


def test_http_hot_path_used(collector, monkeypatch):
    """HTTP 成功 → 直接返回，浏览器路径零调用。"""
    fake = FakeHttp(result=CONTENT_PAGE)
    monkeypatch.setattr(collector, "_get_http", lambda: fake)
    monkeypatch.setattr(WAFBrowserCollector, "_fetch_page",
                        browser_sentinel)
    assert collector._fetch_page("https://x/a") == CONTENT_PAGE
    assert fake.calls == ["https://x/a"]


def test_degrade_on_new_version_challenge(collector, monkeypatch):
    """新版挑战 → 弃用 HTTP 并降级浏览器；本 run 内不再回 HTTP。"""
    fake = FakeHttp(exc=WafChallengeNewVersion("https://x/a"))
    monkeypatch.setattr(collector, "_get_http", lambda: fake)
    monkeypatch.setattr(WAFBrowserCollector, "_fetch_page",
                        browser_sentinel)
    assert collector._fetch_page("https://x/a") == "BROWSER_HTML"
    assert collector._http_enabled is False
    assert collector._fetch_page("https://x/b") == "BROWSER_HTML"
    assert fake.calls == ["https://x/a"]  # 第二次未再触碰 HTTP


def test_circuit_error_propagates(collector, monkeypatch):
    """HTTP 熔断异常原样穿透：不降级浏览器、不再发起请求。"""
    fake = FakeHttp(exc=WafCircuitOpenError("chictr", 3, 3))
    monkeypatch.setattr(collector, "_get_http", lambda: fake)
    monkeypatch.setattr(WAFBrowserCollector, "_fetch_page",
                        browser_sentinel)
    with pytest.raises(WafCircuitOpenError):
        collector._fetch_page("https://x/a")
    assert collector._http_enabled is True  # 熔断即停，不做双通道加压


def test_fetch_cache_unchanged(collector, monkeypatch):
    """_fetch 的 per-run 页缓存语义在 HTTP 热路径下不变。"""
    fake = FakeHttp(result=CONTENT_PAGE)
    monkeypatch.setattr(collector, "_get_http", lambda: fake)
    assert collector._fetch("https://x/a") == CONTENT_PAGE
    assert collector._fetch("https://x/a") == CONTENT_PAGE
    assert len(fake.calls) == 1  # 第二次命中缓存
