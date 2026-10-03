"""
Shared Playwright lifecycle for collectors that operate against sites
behind a WAF (ChiCTR, CTR).

Established pattern (see the project WAF notes):

  - a singleton Chromium browser is launched once per collector instance
  - the singleton lives on a dedicated module-level "browser thread"
    (_BrowserExecutor): Playwright sync objects are bound to their creating
    thread, and callers (FastAPI threadpool, schedulers) arrive on arbitrary
    threads — every Playwright call is therefore submitted to that one
    thread (greenlet "cannot switch to a different thread" made impossible
    by construction; concurrent callers serialize on it, which also paces
    the WAF rhythm)
  - each request uses a FRESH browser context — both WAFs only allow one
    successful page load per context — which is closed immediately after
  - pages wait a fixed settle time so the WAF JS challenge can finish
  - subclasses decide whether a fetched page contains real content via
    ``_is_waf_page(html)``; failed attempts get fresh contexts and retry

WAF engineering knobs (config.WafConfig, consumed here — never defined):

  - circuit breaker: ``CONFIG.waf.circuit_breaker_threshold`` consecutive
    WAF challenge pages open the circuit — the current ``_fetch_page`` call
    raises :class:`WafCircuitOpenError` immediately and every later call
    fast-fails with zero network requests for the rest of the run;
    ``reset_circuit()`` reopens the gate. 0 disables the breaker.
  - rhythm jitter: ``CONFIG.waf.settle_jitter`` scales each settle wait by
    ``uniform(0.8, 1.6)`` so page cadence looks less machine-like.
  - egress proxy: ``CONFIG.waf.proxy_url`` (env ``CT_WAF_PROXY_URL``) is
    passed to Playwright via ``new_context(proxy={"server": ...})``;
    empty means direct connection (behaviour unchanged).

The circuit-breaker state machine lives in ``core/waf_guard.py`` (shared
with the HTTP hot path in ``collectors/waf_http.py``); this module keeps
the same public methods/attributes and delegates to it.
"""
from __future__ import annotations

import logging
import queue
import random
import threading
import time
from typing import Any, Dict, Optional

from collectors.base import BaseCollector
from config import CONFIG
from core.waf_guard import WafCircuitOpenError, WafGuard

logger = logging.getLogger(__name__)

__all__ = ["WafCircuitOpenError", "WAFBrowserCollector",
           "get_shared_browser", "close_shared_browser"]

_STEALTH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-sandbox",
    "--disable-dev-shm-usage",
]

_STEALTH_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)

# Process-level singleton: a second sync_playwright().start() in the same
# process fails ("Sync API inside the asyncio loop"), and `run_monitor.py
# crawl` runs ChiCTR and CTR sequentially in one process — so the driver
# and browser must be shared across collector instances.
#
# 线程归属不变式（2026-09-23）：Playwright 同步对象（driver transport）
# 绑定创建它的线程，跨线程使用必报 greenlet「cannot switch to a different
# thread (which happens to have exited)」。server（FastAPI 线程池）里多线
# 程复用本单例曾连续触发该缺陷，5 连失败还误开了失败熔断（2026-09-23
# 事故：CTR 实时检索长期「原站熔断保护中」）。因此单例的创建/使用/关闭
# 一律收敛到进程级唯一的「浏览器线程」上执行，对外入口
# （get/close_shared_browser、_fetch_page）全部经 _EXECUTOR 投递。
_SHARED: dict[str, Any] = {}


class _BrowserExecutor:
    """把所有 Playwright 同步调用收敛到一条专用线程串行执行。

    - ``run(fn, *args)``：投递到浏览器线程执行并阻塞取结果，异常原样
      重抛；已在浏览器线程上时内联执行（teardown 再入）。
    - 工作线程逐任务捕获 BaseException，绝不因任务异常退出；意外死亡时
      （实际仅进程退出可能发生）下次 ``run`` 重建线程并丢弃旧单例——旧
      对象绑定死线程，留着只会复现 greenlet 缺陷（driver 进程如旧代码
      一样随管道关闭由系统回收，此处不新增清理手段）。
    - 附带收益：并发调用在浏览器线程上天然串行，等价于对原站的请求节流。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._queue: "queue.Queue[tuple[Any, tuple, dict]]" = queue.Queue()
        self._thread: Optional[threading.Thread] = None

    def _worker(self) -> None:
        while True:
            fn, args, box = self._queue.get()
            if fn is None:  # stop() 哨兵
                return
            try:
                box["value"] = fn(*args)
            except BaseException as exc:  # noqa: BLE001 — 原样回传调用方
                box["error"] = exc
            finally:
                box["done"].set()

    def _ensure_thread(self) -> None:
        with self._lock:
            thread = self._thread
            if thread is not None and thread.is_alive():
                return
            if thread is not None:
                # 换掉死线程：旧单例绑定死线程不可复用，必须丢弃
                # （首次启动时 _SHARED 尚无任何写入，不清）。
                _SHARED.clear()
            self._thread = threading.Thread(
                target=self._worker, name="waf-playwright", daemon=True)
            self._thread.start()

    def run(self, fn, *args):
        """在浏览器线程上执行 ``fn(*args)``；阻塞至完成，异常原样重抛。

        等待不另设超时，与旧实现同语义：Playwright 自身的导航/超时参数
        兜底任务时长（旧代码同样同步阻塞在调用线程上等待）。
        """
        if (self._thread is not None
                and threading.current_thread() is self._thread):
            return fn(*args)  # 浏览器线程内再入：内联执行
        self._ensure_thread()
        box: dict = {"done": threading.Event()}
        self._queue.put((fn, args, box))
        box["done"].wait()
        if "error" in box:
            raise box["error"]
        return box.get("value")

    def stop(self) -> None:
        """收掉工作线程（teardown 后调用；下次 ``run`` 按需重建）。"""
        with self._lock:
            thread = self._thread
            self._thread = None
        if thread is not None and thread.is_alive():
            self._queue.put((None, (), {}))


_EXECUTOR = _BrowserExecutor()


def _get_shared_browser_on_thread() -> Any:
    """单例创建/获取——只在浏览器线程上执行（见 _SHARED 注释）。"""
    if _SHARED.get("browser") is not None:
        return _SHARED["browser"]
    from playwright.sync_api import sync_playwright

    if _SHARED.get("playwright") is None:
        _SHARED["playwright"] = sync_playwright().start()
    _SHARED["browser"] = _SHARED["playwright"].chromium.launch(
        headless=True,
        channel="chromium",
        args=_STEALTH_ARGS,
    )
    return _SHARED["browser"]


def get_shared_browser() -> Any:
    """Launch (once per process) and return the shared Chromium browser.

    经浏览器线程执行：无论哪个调用方线程先到，单例都归 waf-playwright
    线程所有；handle 也只能继续在该线程上使用（_fetch_attempt 即如此）。
    """
    return _EXECUTOR.run(_get_shared_browser_on_thread)


def _close_shared_browser_on_thread() -> None:
    """单例关闭——Playwright 对象只能在拥有它的线程上 close。"""
    for key in ("browser", "playwright"):
        obj = _SHARED.pop(key, None)
        if obj is not None:
            try:
                obj.close()
            except Exception:
                pass


def close_shared_browser() -> None:
    """Explicitly stop the shared Playwright driver (CLI teardown)."""
    _EXECUTOR.run(_close_shared_browser_on_thread)
    _EXECUTOR.stop()


def _is_transport_defect(exc: BaseException) -> bool:
    """判定是否 greenlet 跨线程缺陷（自身传输层 bug，与原站状态无关）。

    典型形态是 ``greenlet.error: cannot switch to a different thread
    (which happens to have exited)``；字符串兜底覆盖 playwright 把
    greenlet 异常再包装成其他类型的情况。
    """
    if type(exc).__module__ == "greenlet" and type(exc).__name__ == "error":
        return True
    return "cannot switch to a different thread" in str(exc)


class WAFBrowserCollector(BaseCollector):
    """BaseCollector + WAF-tolerant Playwright page fetching.

    Per-source knobs (override as class attributes in subclasses):

    - ``wait_until``        Playwright ``goto`` wait condition
                            (ChiCTR needs ``domcontentloaded``: its WAF JS
                            polling prevents ``networkidle`` from firing)
    - ``page_timeout_ms``   navigation timeout
    - ``settle_ms``         extra wait after navigation for the challenge
    - ``retry_backoff_ms``  wait between attempts (lets challenge JS run)
    - ``total_attempts``    fresh-context attempts per URL
    - ``block_images``      abort image requests for faster loads

    Circuit-breaker state (consecutive-WAF counter, open flag) lives on the
    instance — in-process only, no cross-run persistence.  Call
    ``reset_circuit()`` to explicitly reopen a tripped breaker.
    """

    wait_until: str = "domcontentloaded"
    page_timeout_ms: int = 25000
    settle_ms: int = 2000
    retry_backoff_ms: int = 0
    total_attempts: int = 2
    block_images: bool = False
    # 搜索列表页每页行数（短页 = 该词末页，live_search 翻页停止判据）
    list_page_size: int = 20

    def __init__(self, source_key: str):
        super().__init__(source_key)
        self._browser: Any = None  # shared process-level browser handle
        # ── 熔断器状态机：共享实现（core/waf_guard），HTTP 热路径同款 ──
        self._guard = WafGuard(source_key)

    # ── 实时检索（live-search，server /api/trials/live-check 同步调用） ──

    def _entry_identity(self, entry: Dict[str, Any]) -> tuple[Optional[str], str]:
        """(source_trial_id, title) from one parsed search-result row."""
        raise NotImplementedError

    def _entry_url(self, entry: Dict[str, Any]) -> Optional[str]:
        """Detail-page URL for one parsed row（无详情键时返回 None）。"""
        return None

    def _absorb_live_entries(self, entries: list) -> int:
        """Queue live-found rows（默认：逐行入队，INSERT OR IGNORE 幂等）。

        子类覆盖以同时维护详情键辅助表（ChiCTR proj_ids / CTR
        detail_keys）——否则 enrich 侧无法解析详情键。
        """
        queued = 0
        for entry in entries:
            source_trial_id, title = self._entry_identity(entry)
            if source_trial_id and self._enqueue(source_trial_id, "live_search",
                                                 title=title):
                queued += 1
        return queued

    def live_search(self, keyword: str, max_pages: int = 1,
                    start_page: int = 1) -> dict:
        """同步查原站：关键词走搜索列表页，命中行入补爬队列。

        与 discover_new 共用 _search_results_page/_enqueue 基建，但**不触碰
        list_walk 水位线**（重复行由入队幂等吸收，夜间增量照常复核）；
        详情键辅助表由子类 _absorb_live_entries 维护。页间按
        ``cfg.request_delay_sec`` 控速；熔断时抛 WafCircuitOpenError。

        分批抓取契约（WAF 友好的「抓全部页面」）：
          - ``start_page``：本批起始页（服务端会话续抓用，>1 时不再重置
            _last_site_total，沿用第 1 页解析出的原站总数）；
          - 停止条件：空页/短页（该词末页）、原站总数已取满（site_total，
            第 1 页顺带解析）、或本批 max_pages 用尽（→ has_more=True，
            供调用方以 start_page=next_page 续批，批间由服务端强制间隔）；
          - 返回新增 ``has_more`` / ``next_page``：是否还有后续页、下批起始页。

        返回 {keyword, found, queued, pages_walked, stopped_reason,
        site_total, has_more, next_page, entries}；
        entries 为 {source_trial_id, title, url} 规范形，供前端直接渲染。
        """
        entries_all: list = []
        pages_walked = 0
        stopped_reason = "completed"
        max_pages = max(1, int(max_pages))
        start_page = max(1, int(start_page))
        # 原站总命中数（可选协议）：子类 _search_results_page 抓第 1 页时
        # 顺带解析并写入 _last_site_total（零额外请求）；首轮先清防串味。
        if start_page == 1 and hasattr(self, "_last_site_total"):
            self._last_site_total = None
        last_page_full = False
        for page in range(start_page, start_page + max_pages):
            try:
                entries = self._search_results_page(keyword, page)
            except WafCircuitOpenError:
                stopped_reason = "circuit_open"
                raise
            except Exception as exc:
                logger.warning("live_search 列表页失败 '%s' p%d: %s",
                               keyword, page, exc)
                stopped_reason = "page_failed"
                last_page_full = False
                break
            if not entries:
                last_page_full = False
                break  # 空页 → 该词末尾
            pages_walked += 1
            entries_all.extend(entries)
            last_page_full = len(entries) >= self.list_page_size
            if not last_page_full:
                break  # 短页 → 该词末页
            site_total = getattr(self, "_last_site_total", None)
            fetched_overall = (start_page - 1) * self.list_page_size + len(entries_all)
            if site_total and fetched_overall >= site_total:
                break  # 已取满原站总数
            if pages_walked < max_pages:
                time.sleep(self.cfg.request_delay_sec)

        # has_more：本批页数用尽且最后一页是满页（否则本身就是末页/失败）
        has_more = stopped_reason == "completed" and last_page_full \
            and pages_walked > 0
        next_page = start_page + pages_walked if has_more else None
        queued = self._absorb_live_entries(entries_all) if entries_all else 0
        seen: set = set()
        norm: list = []
        for entry in entries_all:
            source_trial_id, title = self._entry_identity(entry)
            if not source_trial_id or source_trial_id in seen:
                continue
            seen.add(source_trial_id)
            norm.append({"source_trial_id": source_trial_id, "title": title,
                         "url": self._entry_url(entry)})
        stats = {
            "keyword": keyword, "found": len(norm), "queued": queued,
            "pages_walked": pages_walked, "stopped_reason": stopped_reason,
            "site_total": getattr(self, "_last_site_total", None),
            "has_more": has_more, "next_page": next_page,
            "entries": norm,
        }
        logger.info("live_search '%s': found=%d queued=%d pages=%d site_total=%s more=%s (%s)",
                    keyword, len(norm), queued, pages_walked,
                    stats["site_total"], has_more, stopped_reason)
        return stats

    # ── Playwright lifecycle ────────────────────────────────────────

    def _get_browser(self) -> Any:
        """Get the shared (process-level) Chromium browser."""
        self._browser = get_shared_browser()
        return self._browser

    def _make_context(self) -> Any:
        """Create a fresh browser context with stealth settings.

        ``CONFIG.waf.proxy_url`` 非空时经 Playwright 原生
        ``proxy={"server": ...}`` 走代理出口；为空（默认）时参数与直连
        完全一致。
        """
        ctx_kwargs: dict[str, Any] = {
            "viewport": {"width": 1920, "height": 1080},
            "locale": "zh-CN",
            "timezone_id": "Asia/Shanghai",
            "user_agent": _STEALTH_UA,
        }
        proxy_url = CONFIG.waf.proxy_url
        if proxy_url:
            ctx_kwargs["proxy"] = {"server": proxy_url}
        # attach (or reuse) the process-level singleton here — _fetch_page
        # no longer launches eagerly, so first context creation owns it
        ctx = self._get_browser().new_context(**ctx_kwargs)
        ctx.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', "
            "{get: () => undefined});"
        )
        if self.block_images:
            ctx.route(
                "**/*.{png,jpg,jpeg,gif,svg,ico,webp}",
                lambda route: route.abort(),
            )
        return ctx

    def _close_browser(self) -> None:
        """Detach from the shared browser without killing it.

        The driver/browser are process-level singletons: other collectors
        (or later runs of this one) still need them.  Use
        close_shared_browser() for explicit teardown.
        """
        self._browser = None

    def __del__(self) -> None:
        self._close_browser()

    # ── Page fetching ───────────────────────────────────────────────

    def _is_waf_page(self, html: str) -> bool:
        """Return True if the HTML is a WAF challenge page, not content."""
        raise NotImplementedError

    # ── 熔断器（circuit breaker，委托 core.waf_guard.WafGuard） ─────

    @property
    def _waf_streak(self) -> int:
        """连续 WAF 挑战页计数（守卫状态只读投影，供测试断言）。"""
        return self._guard.waf_streak

    @property
    def _circuit_open(self) -> bool:
        """熔断是否已打开（守卫状态只读投影，供测试断言）。"""
        return self._guard.circuit_open

    @property
    def _failure_streak(self) -> int:
        """连续抓取耗尽/超时计数（守卫状态只读投影，供测试断言）。"""
        return self._guard.failure_streak

    def reset_circuit(self) -> None:
        """显式复位熔断器：清零连续 WAF 计数并重新放行抓取。"""
        self._guard.reset_circuit()

    def _ensure_circuit_closed(self) -> None:
        """熔断已打开时快速抛 WafCircuitOpenError（零网络请求保证）。"""
        self._guard.ensure_closed()

    def _register_waf_page(self) -> None:
        """登记一次 WAF 挑战页；达阈值打开熔断并抛异常（语义见 WafGuard）。"""
        self._guard.register_waf_page()

    def _notify_circuit_open(self, threshold: int) -> None:
        """熔断告警（委托守卫，保留方法名兼容既有调用方）。"""
        self._guard._notify_circuit_open(threshold)

    # ── 节律抖动（settle jitter） ───────────────────────────────────

    def _settle_duration_ms(self) -> float:
        """计算本次 settle 时长（毫秒）：settle_ms × 抖动系数。

        ``CONFIG.waf.settle_jitter`` 为 True 时系数取 ``random.uniform
        (0.8, 1.6)``（不固定 seed）；为 False 时恒为 1.0（行为不变）。
        """
        if CONFIG.waf.settle_jitter:
            return self.settle_ms * random.uniform(0.8, 1.6)
        return float(self.settle_ms)

    def _settle(self, page: Any) -> None:
        """导航后等待 WAF JS 挑战完成（settle，可带抖动）。"""
        page.wait_for_timeout(self._settle_duration_ms())

    # ── 单次尝试与整页抓取 ─────────────────────────────────────────

    def _fetch_attempt(self, url: str, timeout: int) -> str:
        """执行单次抓取尝试（全新 context）：导航 + settle + 取 HTML。

        导航/取页失败时抛原始异常（由 _fetch_page 统一记日志并重试）；
        WAF 判定与熔断计数不在此处，属 _fetch_page 的职责。
        """
        context = self._make_context()
        page = context.new_page()
        try:
            page.goto(url, wait_until=self.wait_until, timeout=timeout)
            self._settle(page)
            return page.content()
        finally:
            try:
                page.close()
            except Exception:
                pass
            try:
                context.close()
            except Exception:
                pass

    def _fetch_page(self, url: str,
                    timeout_ms: Optional[int] = None) -> Optional[str]:
        """Fetch one page with a fresh browser context per attempt.

        Returns the page HTML once real content is detected, or None when
        all attempts ended in WAF challenges / navigation errors.

        熔断语义（``CONFIG.waf.circuit_breaker_threshold`` > 0 时启用）：
        实例级「连续 WAF 页」计数达到阈值即打开熔断 —— 当前调用立即抛
        WafCircuitOpenError（穿透下方 attempt 循环），本次 run 内后续
        _fetch_page 调用在发起任何网络请求前快速抛同款异常；成功非 WAF
        页将计数清零；导航异常不改变计数；reset_circuit() 显式复位。
        """
        # 熔断快速失败：不启动浏览器、不发任何网络请求。
        self._ensure_circuit_closed()

        # NOTE: the browser is NOT launched eagerly here — the real
        # _fetch_attempt opens a context via _make_context → _get_browser,
        # which launches (or reuses) the process-level singleton.  Launching
        # eagerly would start Playwright even when _fetch_attempt is stubbed
        # (tests) or the circuit is about to trip, leaving a stray singleton
        # whose event loop breaks any later sync_playwright() in-process.
        timeout = timeout_ms or self.page_timeout_ms

        for attempt in range(1, self.total_attempts + 1):
            try:
                # 所有 Playwright 调用经浏览器线程执行——跨线程 greenlet
                # 缺陷的根治；测试桩（实例属性替换 _fetch_attempt）同样
                # 被投递，投递开销相对页面 settle 可忽略。
                html = _EXECUTOR.run(self._fetch_attempt, url, timeout)
            except WafCircuitOpenError:
                raise  # 熔断异常不是普通失败，不得计入重试
            except Exception as exc:
                if _is_transport_defect(exc):
                    # 自伤性传输缺陷 ≠ 原站状态：不计入失败熔断（防止把
                    # 熔断器误锁成「原站不可用」，2026-09-23 事故），也
                    # 不重试（确定性失败），直接上抛如实暴露。
                    logger.error(
                        "Transport defect fetching %s (Playwright object "
                        "used from a foreign thread?): %s — not counting "
                        "toward the circuit breaker", url, exc,
                    )
                    raise
                logger.warning(
                    "Fetch failed for %s (attempt %d/%d): %s",
                    url, attempt, self.total_attempts, exc,
                )
            else:
                if not self._is_waf_page(html):
                    self._guard.register_success()  # 成功非 WAF 页清零计数
                    return html
                logger.warning(
                    "WAF challenge on %s (attempt %d/%d, %d bytes)",
                    url, attempt, self.total_attempts, len(html),
                )
                # 达到阈值时在此抛出（else 块不被上方 except 捕获），
                # 直接穿透 attempt 循环向上传播。
                self._register_waf_page()

            if self.retry_backoff_ms:
                time.sleep(self.retry_backoff_ms / 1000)

        logger.error("All %d attempts exhausted for %s",
                     self.total_attempts, url)
        # 连续失败熔断（超时/网络错误，与 WAF 页计数互补）：站点侧波动时
        # 尽快止损，避免 enrich 批次连续烧穿 attempts（见 waf_guard）。
        self._guard.register_failure()
        return None
