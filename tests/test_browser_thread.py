"""浏览器线程执行器（browser_base._BrowserExecutor）与传输缺陷识别测试。

背景（2026-09-23 事故）：Playwright 同步对象绑定创建线程，server 的
FastAPI 线程池跨线程复用进程级单例，greenlet「cannot switch to a
different thread」连续 5 次误开失败熔断，CTR 实时检索长期卡在
「原站熔断保护中」。修复 = 单例生命周期收敛到唯一浏览器线程 + 熔断器
识别传输缺陷（不计入失败熔断、不重试、原样上抛）。

全程离线：不启动 Playwright、不发任何真实网络请求。覆盖：

1. _EXECUTOR：任务在唯一工作线程上串行执行、结果/异常原样回传、
   浏览器线程内再入内联、stop 后按需重建、线程死亡后丢弃旧单例。
2. _fetch_page 投递：多个调用方线程下 _fetch_attempt 始终在同一条
   浏览器线程上执行。
3. 传输缺陷：类型/消息两种识别路径；不计失败熔断、不重试、原样上抛，
   后续抓取不被锁死。
4. close_shared_browser：关闭逻辑经浏览器线程执行。
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

import collectors.browser_base as browser_base
from collectors.browser_base import (
    WafCircuitOpenError,
    WAFBrowserCollector,
    _BrowserExecutor,
    _is_transport_defect,
)
from config import CONFIG

OK_HTML = "<html><body><table class='searchTable'>真实内容</table></body></html>"

DEFECT_MSG = ("cannot switch to a different thread "
              "(which happens to have exited)")


# ── 测试替身 ─────────────────────────────────────────────────────────


class StubCollector(WAFBrowserCollector):
    """离线桩采集器（同 test_browser_base_circuit 约定）。"""

    settle_ms = 1000
    retry_backoff_ms = 0
    total_attempts = 2

    def _is_waf_page(self, html: str) -> bool:
        return html.startswith("WAF")

    def fetch_new_or_updated(self, since=None):
        return []

    def normalise(self, raw):
        raise NotImplementedError


def _greenlet_style_error() -> type:
    """构造 greenlet.error 同款异常类（不依赖 greenlet 包）。"""
    return type("error", (Exception,), {"__module__": "greenlet"})


# ── fixtures ─────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda secs: None)


@pytest.fixture
def waf_cfg(monkeypatch):
    monkeypatch.setattr(CONFIG.waf, "circuit_breaker_threshold", 3)
    monkeypatch.setattr(CONFIG.waf, "failure_breaker_threshold", 5)
    monkeypatch.setattr(CONFIG.waf, "settle_jitter", False)
    monkeypatch.setattr(CONFIG.waf, "proxy_url", None)
    return CONFIG.waf


# ── _BrowserExecutor 基础语义 ────────────────────────────────────────


def test_tasks_run_on_single_dedicated_thread():
    """并发投递的任务全部串行执行在同一条工作线程上（非调用方线程）。"""
    ex = _BrowserExecutor()
    seen: list = []

    def task(i: int) -> int:
        seen.append(threading.get_ident())
        return i * 2

    def submit(i: int) -> int:
        return ex.run(task, i)

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert list(pool.map(submit, range(8))) == [i * 2 for i in range(8)]

    assert len(set(seen)) == 1, "所有任务应落在同一条浏览器线程上"
    assert seen[0] != threading.get_ident()
    ex.stop()


def test_exception_propagates_and_reentrant_runs_inline():
    """任务异常原样重抛；浏览器线程内再入 run() 时内联执行。"""
    ex = _BrowserExecutor()

    def boom():
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        ex.run(boom)

    def nested() -> tuple:
        inner_id = ex.run(lambda: threading.get_ident())
        return inner_id == threading.get_ident()

    assert ex.run(nested) is True, "再入应在浏览器线程内联执行（同一线程）"
    ex.stop()


def test_restart_after_stop():
    """stop() 收掉工作线程后，下次 run() 按需重建、功能如常。"""
    ex = _BrowserExecutor()
    assert ex.run(lambda: 1) == 1
    ex.stop()
    assert ex.run(lambda: 2) == 2


def test_thread_death_drops_stale_singleton(monkeypatch):
    """工作线程意外死亡：重建线程并丢弃绑定死线程的旧单例。"""
    ex = _BrowserExecutor()
    ex.run(lambda: None)
    dead = threading.Thread(target=lambda: None)
    dead.start()
    dead.join()
    stale = {"browser": object(), "playwright": object()}
    monkeypatch.setattr(browser_base, "_SHARED", stale)
    monkeypatch.setattr(ex, "_thread", dead)  # 模拟工作线程死亡

    assert ex.run(lambda: "ok") == "ok"

    assert browser_base._SHARED == {}, "死线程上的旧单例必须被丢弃"
    assert ex._thread is not dead and ex._thread.is_alive()


# ── _fetch_page 投递到浏览器线程 ─────────────────────────────────────


def test_fetch_page_funnels_to_single_browser_thread(waf_cfg):
    """多个调用方线程依次走 _fetch_page：抓取始终发生在同一条线程上。"""
    collector = StubCollector("chictr")
    worker_ids: list = []
    caller_ids: list = []

    def recording_fetch_attempt(url, timeout):
        worker_ids.append(threading.get_ident())
        return OK_HTML

    collector._fetch_attempt = recording_fetch_attempt

    def call_from_thread():
        caller_ids.append(threading.get_ident())
        assert collector._fetch_page("http://x/1") == OK_HTML

    threads = [threading.Thread(target=call_from_thread) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert collector._circuit_open is False
    assert len(worker_ids) == 3
    assert len(set(worker_ids)) == 1, "抓取必须收敛到唯一浏览器线程"
    assert worker_ids[0] not in caller_ids, "不得在任何调用方线程上执行"


# ── 传输缺陷识别（greenlet 跨线程） ──────────────────────────────────


def test_transport_defect_detected_by_type_and_message():
    """greenlet.error 类型或消息文本两种路径都能识别；普通异常不误判。"""
    gls_error = _greenlet_style_error()
    assert _is_transport_defect(gls_error(DEFECT_MSG))
    assert _is_transport_defect(RuntimeError(DEFECT_MSG))
    assert not _is_transport_defect(RuntimeError("net timeout"))
    assert not _is_transport_defect(WafCircuitOpenError("ctr", 3, 3))


def test_transport_defect_no_retry_no_failure_count(waf_cfg):
    """传输缺陷：不计失败熔断、不重试、原样上抛，后续抓取不被锁死。"""
    waf_cfg.failure_breaker_threshold = 1  # 若被计入，首次即熔断
    collector = StubCollector("chictr")
    calls: list = []
    gls_error = _greenlet_style_error()

    def broken(url, timeout):
        calls.append(url)
        raise gls_error(DEFECT_MSG)

    collector._fetch_attempt = broken
    with pytest.raises(Exception) as excinfo:
        collector._fetch_page("http://x/1")
    assert "cannot switch to a different thread" in str(excinfo.value)

    assert len(calls) == 1, "确定性缺陷不应重试（total_attempts=2）"
    assert collector._failure_streak == 0
    assert collector._circuit_open is False

    collector._fetch_attempt = lambda url, timeout: OK_HTML
    assert collector._fetch_page("http://x/2") == OK_HTML, \
        "缺陷后熔断器必须保持关闭，正常抓取可继续"


# ── close_shared_browser 经浏览器线程 ────────────────────────────────


def test_close_shared_browser_runs_on_browser_thread(monkeypatch):
    """teardown 关闭单例：close 在浏览器线程上执行并清空 _SHARED。"""
    closed: list = []

    class FakePw:
        def __init__(self, name):
            self._name = name

        def close(self):
            closed.append(self._name)

    monkeypatch.setattr(browser_base, "_SHARED", {
        "browser": FakePw("browser"),
        "playwright": FakePw("playwright"),
    })

    browser_base.close_shared_browser()

    assert closed == ["browser", "playwright"]
    assert browser_base._SHARED == {}
