"""WAF 浏览器基类（collectors/browser_base.py）故障注入测试。

覆盖三块新能力，全程离线、不发任何真实网络请求：

1. 熔断器：连续 WAF 页计数、阈值触发 WafCircuitOpenError、成功页清零、
   阈值 0 禁用、熔断后快速失败零额外请求、reset_circuit() 显式复位、
   core.notify 告警被调用且告警异常被吞掉。
2. 节律抖动：CONFIG.waf.settle_jitter 开关对 settle 时长的影响
   （monkeypatch random.uniform 与页面等待记录）。
3. 代理：CONFIG.waf.proxy_url 非空时 new_context 带 proxy 参数、
   为空时不带（monkeypatch 假浏览器捕获 kwargs）。
"""
from __future__ import annotations

import pytest

from collectors.browser_base import WafCircuitOpenError, WAFBrowserCollector
from config import CONFIG

# ── 测试替身 ─────────────────────────────────────────────────────────

WAF_HTML = "WAF<cf-challenge>请完成安全验证</cf-challenge>"
OK_HTML = "<html><body><table class='searchTable'>真实内容</table></body></html>"


class StubCollector(WAFBrowserCollector):
    """离线桩采集器：HTML 以 "WAF" 开头即判定为挑战页。"""

    settle_ms = 1000
    retry_backoff_ms = 0
    total_attempts = 2
    block_images = False

    def _is_waf_page(self, html: str) -> bool:
        """以最简单的前缀规则模拟子类的 WAF 页判定。"""
        return html.startswith("WAF")

    def fetch_new_or_updated(self, since=None):
        """满足 BaseCollector 抽象要求（本测试不执行 run 流程）。"""
        return []

    def normalise(self, raw):
        """满足 BaseCollector 抽象要求（本测试不执行 run 流程）。"""
        raise NotImplementedError


class FakePage:
    """假页面：记录 wait_for_timeout 时长，返回固定 HTML。"""

    def __init__(self, html: str):
        self._html = html
        self.waited_ms: list = []

    def goto(self, url, wait_until=None, timeout=None):
        """模拟导航（无操作）。"""

    def wait_for_timeout(self, ms):
        """记录 settle 等待时长，供抖动断言使用。"""
        self.waited_ms.append(ms)

    def content(self):
        """返回构造时固定的页面 HTML。"""
        return self._html

    def close(self):
        """模拟关闭页面（无操作）。"""


class FakeContext:
    """假 context：总是返回同一个 FakePage。"""

    def __init__(self, html: str):
        self.page = FakePage(html)

    def new_page(self):
        """返回假页面。"""
        return self.page

    def add_init_script(self, script):
        """模拟注入脚本（无操作）。"""

    def route(self, pattern, handler):
        """模拟请求拦截（无操作）。"""

    def close(self):
        """模拟关闭 context（无操作）。"""


class FakeBrowser:
    """假浏览器：捕获 new_context kwargs，供代理与抖动断言使用。"""

    def __init__(self, html: str):
        self.html = html
        self.ctx_kwargs: list = []
        self.contexts: list = []

    def new_context(self, **kwargs):
        """记录创建参数并返回假 context。"""
        self.ctx_kwargs.append(kwargs)
        ctx = FakeContext(self.html)
        self.contexts.append(ctx)
        return ctx


class FakeFetcher:
    """假页面抓取器：按脚本逐次出牌，并统计调用次数。

    脚本元素为 str（页面 HTML）或 Exception 实例（模拟导航失败）；
    脚本耗尽后默认一直返回 WAF 页。
    """

    def __init__(self, script):
        self.script = list(script)
        self.calls: list = []

    def __call__(self, url, timeout=None):
        self.calls.append(url)
        if not self.script:
            return WAF_HTML
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


# ── fixtures ─────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    """所有测试禁用真实 time.sleep（重试退避等绝不真实等待）。"""
    monkeypatch.setattr("time.sleep", lambda secs: None)


@pytest.fixture
def waf_cfg(monkeypatch):
    """隔离 CONFIG.waf 三项参数（默认：阈值 3、抖动关、直连）。"""
    monkeypatch.setattr(CONFIG.waf, "circuit_breaker_threshold", 3)
    monkeypatch.setattr(CONFIG.waf, "settle_jitter", False)
    monkeypatch.setattr(CONFIG.waf, "proxy_url", None)
    return CONFIG.waf


@pytest.fixture
def collector(waf_cfg):
    """离线桩采集器（不启动 Playwright、不发网络请求）。"""
    return StubCollector("chictr")


@pytest.fixture
def fake_browser(monkeypatch, collector):
    """用假浏览器替换进程级单例，供走真实 _fetch_attempt 的测试使用。"""
    browser = FakeBrowser(OK_HTML)
    monkeypatch.setattr("collectors.browser_base.get_shared_browser",
                        lambda: browser)
    return browser


# ── 熔断器 ───────────────────────────────────────────────────────────


def test_consecutive_waf_pages_open_circuit(collector, waf_cfg):
    """连续 WAF 页达到阈值即打开熔断并抛 WafCircuitOpenError。"""
    waf_cfg.circuit_breaker_threshold = 3
    fake = FakeFetcher([WAF_HTML] * 10)
    collector._fetch_attempt = fake  # monkeypatch 内部页面获取函数

    # 第 1 个 URL：2 次尝试全部 WAF → 计数 2 < 3，正常返回 None。
    assert collector._fetch_page("http://x/1") is None
    assert len(fake.calls) == 2
    assert collector._waf_streak == 2

    # 第 2 个 URL：第 1 次尝试后计数达 3 → 立即抛出，第 2 次尝试不执行。
    with pytest.raises(WafCircuitOpenError):
        collector._fetch_page("http://x/2")
    assert len(fake.calls) == 3  # 只多消耗一次请求
    assert collector._circuit_open is True
    assert collector._waf_streak == 3


def test_circuit_error_type_and_message(collector, waf_cfg):
    """异常类型正确（RuntimeError 子类）且信息含源名与阈值。"""
    waf_cfg.circuit_breaker_threshold = 3
    collector._fetch_attempt = FakeFetcher([WAF_HTML] * 10)

    # 第 1 个 URL：2 次尝试全部 WAF，计数 2 尚未熔断。
    assert collector._fetch_page("http://x/1") is None
    with pytest.raises(WafCircuitOpenError) as excinfo:
        collector._fetch_page("http://x/2")
    exc = excinfo.value
    assert isinstance(exc, RuntimeError)
    assert exc.source_key == "chictr"
    assert exc.threshold == 3
    assert exc.consecutive == 3
    assert "chictr" in str(exc)
    assert "3" in str(exc)


def test_threshold_zero_disables_breaker(collector, waf_cfg):
    """WAF 熔断阈值 0 = 禁用 WAF 页熔断（失败熔断需另行隔离）。"""
    waf_cfg.circuit_breaker_threshold = 0
    waf_cfg.failure_breaker_threshold = 0
    fake = FakeFetcher([WAF_HTML] * 10)
    collector._fetch_attempt = fake

    for i in range(1, 6):
        assert collector._fetch_page(f"http://x/{i}") is None
    assert len(fake.calls) == 10  # 5 个 URL × 2 次尝试，全部照常发生
    assert collector._circuit_open is False


def test_success_page_resets_counter(collector, waf_cfg):
    """成功非 WAF 页把连续计数清零；散布的成功页阻止熔断。"""
    waf_cfg.circuit_breaker_threshold = 3
    collector.total_attempts = 1
    fake = FakeFetcher([
        WAF_HTML, WAF_HTML,  # 计数 1, 2
        OK_HTML,             # 成功 → 清零
        WAF_HTML, WAF_HTML,  # 计数 1, 2 —— 若未清零这里已熔断
        WAF_HTML,            # 计数 3 → 熔断（证明前面确实清过零）
    ])
    collector._fetch_attempt = fake

    assert collector._fetch_page("http://x/1") is None     # WAF
    assert collector._fetch_page("http://x/2") is None     # WAF
    assert collector._fetch_page("http://x/3") == OK_HTML  # 成功清零
    assert collector._fetch_page("http://x/4") is None
    assert collector._fetch_page("http://x/5") is None
    assert collector._waf_streak == 2

    with pytest.raises(WafCircuitOpenError):
        collector._fetch_page("http://x/6")


def test_open_circuit_fast_fails_with_zero_requests(collector, waf_cfg):
    """熔断后后续 _fetch_page 立即抛异常且不再发起任何请求。"""
    waf_cfg.circuit_breaker_threshold = 3
    fake = FakeFetcher([WAF_HTML] * 10)
    collector._fetch_attempt = fake

    assert collector._fetch_page("http://x/1") is None  # 计数 2
    with pytest.raises(WafCircuitOpenError):
        collector._fetch_page("http://x/2")             # 计数 3 → 熔断
    calls_at_open = len(fake.calls)

    for _ in range(5):
        with pytest.raises(WafCircuitOpenError):
            collector._fetch_page("http://x/never-sent")
    assert len(fake.calls) == calls_at_open  # 零额外请求
    assert collector._waf_streak == 3


def test_navigation_error_does_not_change_counter(collector, waf_cfg):
    """导航异常既不清零也不增加连续 WAF 计数。"""
    waf_cfg.circuit_breaker_threshold = 3
    collector.total_attempts = 1
    fake = FakeFetcher([
        WAF_HTML,                  # 计数 1
        RuntimeError("net down"),  # 异常 → 计数保持 1
        WAF_HTML,                  # 计数 2
        WAF_HTML,                  # 计数 3 → 熔断
    ])
    collector._fetch_attempt = fake

    assert collector._fetch_page("http://x/1") is None  # WAF
    assert collector._fetch_page("http://x/2") is None  # 导航失败返回 None
    assert collector._waf_streak == 1
    assert collector._fetch_page("http://x/3") is None
    with pytest.raises(WafCircuitOpenError):
        collector._fetch_page("http://x/4")


# ── 连续失败熔断（fetch_failure，与 WAF 页计数互补） ─────────────────


def test_failure_breaker_trips_on_consecutive_exhaustion(collector, waf_cfg):
    """连续多个 URL「尝试耗尽无内容」（导航异常）→ fetch_failure 熔断。

    对应 CTR 瑞数 WAF 间歇性全页超时的实战场景：每行 3×45s 耗尽返回
    None，没有本熔断时 enrich 批次会连续烧穿 attempts（2026-09-13）。
    """
    waf_cfg.circuit_breaker_threshold = 0        # 隔离：只测失败熔断
    waf_cfg.failure_breaker_threshold = 3
    collector.total_attempts = 2
    collector._fetch_attempt = FakeFetcher(
        [RuntimeError("timeout")] * 10)

    assert collector._fetch_page("http://x/1") is None
    assert collector._failure_streak == 1
    assert collector._fetch_page("http://x/2") is None
    assert collector._failure_streak == 2
    with pytest.raises(WafCircuitOpenError) as excinfo:
        collector._fetch_page("http://x/3")      # 第 3 次耗尽 → 打开
    assert excinfo.value.reason == "fetch_failure"
    assert collector._circuit_open is True
    with pytest.raises(WafCircuitOpenError):
        collector._fetch_page("http://x/4")      # 快速失败（零请求）


def test_success_resets_failure_streak(collector, waf_cfg):
    """成功页同时清零失败计数：耗尽→成功→耗尽 不触发熔断。"""
    waf_cfg.circuit_breaker_threshold = 0
    waf_cfg.failure_breaker_threshold = 3
    collector.total_attempts = 1
    collector._fetch_attempt = FakeFetcher([
        RuntimeError("timeout"),   # x/1 耗尽 → streak 1
        OK_HTML,                   # x/2 成功 → 清零
        RuntimeError("timeout"),   # x/3 耗尽 → streak 1
        OK_HTML,                   # x/4 成功
    ])

    assert collector._fetch_page("http://x/1") is None
    assert collector._failure_streak == 1
    assert collector._fetch_page("http://x/2") == OK_HTML
    assert collector._failure_streak == 0
    assert collector._fetch_page("http://x/3") is None
    assert collector._failure_streak == 1
    assert collector._circuit_open is False
    assert collector._fetch_page("http://x/4") == OK_HTML


def test_reset_circuit_reopens_fetching(collector, waf_cfg):
    """reset_circuit() 复位后恢复抓取，计数从零重新累计。"""
    waf_cfg.circuit_breaker_threshold = 3
    collector.total_attempts = 1
    fake = FakeFetcher([WAF_HTML] * 10)
    collector._fetch_attempt = fake

    assert collector._fetch_page("http://x/1") is None  # 计数 1
    assert collector._fetch_page("http://x/2") is None  # 计数 2
    with pytest.raises(WafCircuitOpenError):
        collector._fetch_page("http://x/3")             # 计数 3 → 熔断
    calls_at_open = len(fake.calls)

    collector.reset_circuit()
    assert collector._circuit_open is False
    assert collector._waf_streak == 0

    # 复位后可继续发请求，且需重新累计满阈值才会再次熔断。
    assert collector._fetch_page("http://x/4") is None
    assert collector._fetch_page("http://x/5") is None
    assert len(fake.calls) == calls_at_open + 2
    with pytest.raises(WafCircuitOpenError):
        collector._fetch_page("http://x/6")


# ── 熔断 TTL 半开复位（长驻 API 进程，2026-09-24 演示站事故） ─────────


def _trip_circuit(collector):
    """连续 3 个 WAF 页把熔断打开（threshold=3、total_attempts=1）。"""
    collector.total_attempts = 1
    collector._fetch_attempt = FakeFetcher([WAF_HTML] * 10)
    assert collector._fetch_page("http://x/1") is None   # streak 1
    assert collector._fetch_page("http://x/2") is None   # streak 2
    with pytest.raises(WafCircuitOpenError):
        collector._fetch_page("http://x/3")              # streak 3 → 打开
    assert collector._circuit_open is True


def test_circuit_auto_resets_after_ttl(collector, waf_cfg, monkeypatch):
    """熔断持续超过 circuit_reset_after_sec → ensure_closed 半开复位放行。"""
    import time

    waf_cfg.circuit_breaker_threshold = 3
    monkeypatch.setattr(CONFIG.waf, "circuit_reset_after_sec", 1800)
    _trip_circuit(collector)

    # 冷却期未满：快速失败照旧。
    with pytest.raises(WafCircuitOpenError):
        collector._guard.ensure_closed()

    # 回拨打开时刻越过冷却期 → 半开复位，恢复放行。
    collector._guard._opened_at = time.monotonic() - 1801
    collector._guard.ensure_closed()                      # 不再抛
    assert collector._circuit_open is False
    assert collector._waf_streak == 0

    # 放行后真实发起请求（零请求保证已解除），且重新累计阈值可再熔断。
    assert collector._fetch_page("http://x/4") is None
    assert collector._fetch_page("http://x/5") is None
    with pytest.raises(WafCircuitOpenError):
        collector._fetch_page("http://x/6")
    # 再熔断时打开时刻必须是"现在"，不会沿用回拨值立即再次半开。
    assert collector._guard._opened_at > time.monotonic() - 60


def test_circuit_stays_open_within_ttl(collector, waf_cfg, monkeypatch):
    """冷却期内（< circuit_reset_after_sec）熔断保持打开、零请求。"""
    import time

    waf_cfg.circuit_breaker_threshold = 3
    monkeypatch.setattr(CONFIG.waf, "circuit_reset_after_sec", 1800)
    _trip_circuit(collector)
    collector._guard._opened_at = time.monotonic() - 10

    with pytest.raises(WafCircuitOpenError):
        collector._guard.ensure_closed()
    assert collector._circuit_open is True


def test_circuit_ttl_zero_disables_auto_reset(collector, waf_cfg, monkeypatch):
    """circuit_reset_after_sec=0 → 永不自动复位（批爬"本轮中止"语义）。"""
    import time

    waf_cfg.circuit_breaker_threshold = 3
    monkeypatch.setattr(CONFIG.waf, "circuit_reset_after_sec", 0)
    _trip_circuit(collector)
    collector._guard._opened_at = time.monotonic() - 999_999

    with pytest.raises(WafCircuitOpenError):
        collector._guard.ensure_closed()
    assert collector._circuit_open is True


# ── 告警（core.notify） ──────────────────────────────────────────────


def test_notify_called_once_on_open(collector, waf_cfg, monkeypatch):
    """熔断时经 core.notify.send_notification 发一次 error 级告警。"""
    waf_cfg.circuit_breaker_threshold = 3
    collector._fetch_attempt = FakeFetcher([WAF_HTML] * 10)
    sent: list = []

    def fake_send(title, text, level="error", timeout=10.0):
        sent.append((title, text, level))
        return {"wework": "skipped"}

    monkeypatch.setattr("core.notify.send_notification", fake_send)

    # 第 1 个 URL 累计 2 次 WAF（未达阈值，不应告警）；
    # 第 2 个 URL 第 1 次尝试后达到阈值 → 告警 + 熔断。
    assert collector._fetch_page("http://x/1") is None
    with pytest.raises(WafCircuitOpenError):
        collector._fetch_page("http://x/2")

    assert len(sent) == 1
    title, text, level = sent[0]
    assert level == "error"
    assert "chictr" in text
    assert "3" in text


def test_notify_exception_does_not_break_breaker(collector, waf_cfg,
                                                 monkeypatch):
    """send_notification 抛异常被吞掉，熔断路径照常完成。"""
    waf_cfg.circuit_breaker_threshold = 3
    fake = FakeFetcher([WAF_HTML] * 10)
    collector._fetch_attempt = fake

    def exploding_send(title, text, level="error", timeout=10.0):
        raise RuntimeError("notify backend down")

    monkeypatch.setattr("core.notify.send_notification", exploding_send)

    assert collector._fetch_page("http://x/1") is None  # 计数 2，未熔断
    with pytest.raises(WafCircuitOpenError):
        collector._fetch_page("http://x/2")             # 计数 3 → 熔断
    assert collector._circuit_open is True

    # 熔断状态不受告警失败影响：后续调用照常快速失败。
    with pytest.raises(WafCircuitOpenError):
        collector._fetch_page("http://x/3")
    assert len(fake.calls) == 3


# ── 节律抖动 ─────────────────────────────────────────────────────────


def test_settle_jitter_on_scales_duration(monkeypatch, collector, waf_cfg,
                                          fake_browser):
    """抖动开启：settle 时长 = settle_ms × uniform(0.8, 1.6)。"""
    waf_cfg.settle_jitter = True
    uniform_calls: list = []

    def fake_uniform(low, high):
        uniform_calls.append((low, high))
        return 1.5

    monkeypatch.setattr("collectors.browser_base.random.uniform",
                        fake_uniform)

    assert collector._fetch_page("http://x/ok") == OK_HTML

    assert uniform_calls == [(0.8, 1.6)]
    assert fake_browser.contexts, "应走过真实 _make_context / _settle 路径"
    page = fake_browser.contexts[0].page
    assert page.waited_ms == [1000 * 1.5]


def test_settle_jitter_off_keeps_fixed_duration(monkeypatch, collector,
                                                waf_cfg, fake_browser):
    """抖动关闭：settle 时长恒等于 settle_ms，不调用 random.uniform。"""
    waf_cfg.settle_jitter = False

    def forbidden_uniform(low, high):
        raise AssertionError("settle_jitter=False 不应调用 random.uniform")

    monkeypatch.setattr("collectors.browser_base.random.uniform",
                        forbidden_uniform)

    assert collector._fetch_page("http://x/ok") == OK_HTML

    assert fake_browser.contexts
    page = fake_browser.contexts[0].page
    assert page.waited_ms == [1000]


# ── 代理 ─────────────────────────────────────────────────────────────


def test_proxy_none_omits_proxy_kwarg(collector, waf_cfg, fake_browser):
    """proxy_url 为空：new_context 不携带 proxy 参数（行为不变）。"""
    waf_cfg.proxy_url = None

    assert collector._fetch_page("http://x/ok") == OK_HTML
    assert len(fake_browser.ctx_kwargs) == 1
    kwargs = fake_browser.ctx_kwargs[0]
    assert "proxy" not in kwargs
    # 其余隐身参数保持原样。
    assert kwargs["locale"] == "zh-CN"
    assert kwargs["timezone_id"] == "Asia/Shanghai"


def test_proxy_set_passes_server(collector, waf_cfg, fake_browser):
    """proxy_url 非空：new_context 收到 proxy={"server": url}。"""
    waf_cfg.proxy_url = "http://127.0.0.1:7890"

    assert collector._fetch_page("http://x/ok") == OK_HTML
    assert len(fake_browser.ctx_kwargs) == 1
    kwargs = fake_browser.ctx_kwargs[0]
    assert kwargs["proxy"] == {"server": "http://127.0.0.1:7890"}
    assert kwargs["locale"] == "zh-CN"  # 其余参数不受影响
