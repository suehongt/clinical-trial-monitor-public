"""WAF 熔断守卫：browser / http 两个传输层共享的「连续挑战页 → 熔断」状态机。

抽出自 collectors/browser_base.py（原逻辑原语义），使
`collectors/waf_http.py`（HTTP 热路径）与 Playwright 路径共用同一套
熔断/告警行为——任一传输层连续抓到 WAF 挑战页都先于发现机制中止本轮，
避免「在同一被标记的会话/IP 上继续加深封锁」。

语义与设计依据：the project design notes §2.5、
the project design notes §2.1。

  - 状态为实例级、单进程内存（跨 run 不持久化）；
  - 阈值取 ``CONFIG.waf.circuit_breaker_threshold``（0 = 禁用熔断）；
  - 达到阈值时 ``register_waf_page`` 必抛 :class:`WafCircuitOpenError`，
    由调用方（_fetch_page / get）直接穿透重试循环向上传播；
  - 熔断打开后 ``ensure_closed`` 快速抛同款异常——调用方必须保证在此之前
    零网络请求；熔断持续超过 ``CONFIG.waf.circuit_reset_after_sec`` 时
    ``ensure_closed`` 自动半开复位（0 = 永不复位，保留批爬"本轮中止"语义；
    长驻 API 进程靠它避免"一次熔断、重启才恢复"，2026-09-24）；
  - 熔断前经 core.notify 发告警，告警失败只记日志不影响熔断路径。
"""
from __future__ import annotations

import logging
import threading
import time

from config import CONFIG

logger = logging.getLogger(__name__)


class WafCircuitOpenError(RuntimeError):
    """熔断器打开后由传输层抛出的异常。

    继承 ``RuntimeError``：chictr._fetch 用 RuntimeError 表达「WAF 拦截」，
    沿用同一基类让既有 ``except RuntimeError`` / ``except Exception`` 调用方
    兼容；但该异常语义是「本轮中止」，重试循环必须让它直接向上传播，
    不得计为一次普通失败重试。

    属性：
        source_key:   触发熔断的数据源 key（如 "chictr"）
        threshold:    触发时的连续计数阈值
        consecutive:  触发时的实际连续计数
        reason:       触发原因："waf_page"（连续 WAF 挑战页，阈值
                      circuit_breaker_threshold）或 "fetch_failure"（连续
                      抓取耗尽/超时，阈值 failure_breaker_threshold）
    """

    def __init__(self, source_key: str, threshold: int, consecutive: int,
                 reason: str = "waf_page"):
        self.source_key = source_key
        self.threshold = threshold
        self.consecutive = consecutive
        self.reason = reason
        label = ("WAF challenge pages" if reason == "waf_page"
                 else "fetch failures (timeout/network)")
        super().__init__(
            f"WAF circuit breaker open for source '{source_key}': "
            f"{consecutive} consecutive {label} reached "
            f"threshold {threshold}; no further network requests will be "
            f"made this run (call reset_circuit() to reset)."
        )


class WafGuard:
    """单个数据源传输层的熔断状态（连续 WAF 页计数 + 打开标志）。

    browser_base 与 waf_http 各持一个实例；实测（2026-09-12）表明封禁是
    会话级的、原地重试无意义，因此熔断打开后唯一正确动作是本轮中止。
    """

    def __init__(self, source_key: str):
        self.source_key = source_key
        self._waf_streak: int = 0         # 连续 WAF 挑战页计数
        self._failure_streak: int = 0     # 连续抓取耗尽/超时计数
        self._circuit_open: bool = False  # 熔断是否已打开
        self._circuit_threshold: int = 0  # 打开熔断时的阈值快照（快速失败用）
        self._circuit_reason: str = "waf_page"  # 打开原因（快速失败保真）
        self._opened_at: float = 0.0      # 打开时刻（monotonic；TTL 半开用）
        # 多会话并行（enrich workers）下的计数一致性：所有变更持锁
        self._lock = threading.Lock()

    # ── 状态查询 ─────────────────────────────────────────────────────

    @property
    def waf_streak(self) -> int:
        """当前连续 WAF 挑战页计数。"""
        return self._waf_streak

    @property
    def failure_streak(self) -> int:
        """当前连续抓取耗尽/超时计数。"""
        return self._failure_streak

    @property
    def circuit_open(self) -> bool:
        """熔断是否已打开。"""
        return self._circuit_open

    # ── 动作 ─────────────────────────────────────────────────────────

    def reset_circuit(self) -> None:
        """显式复位熔断器：清零连续计数并重新放行抓取。"""
        with self._lock:
            self._reset_locked()

    def _reset_locked(self) -> None:
        """复位状态（调用方必须已持 ``self._lock``）。"""
        self._waf_streak = 0
        self._failure_streak = 0
        self._circuit_open = False
        self._circuit_threshold = 0
        self._circuit_reason = "waf_page"
        self._opened_at = 0.0

    def ensure_closed(self) -> None:
        """熔断已打开时快速抛 WafCircuitOpenError（零网络请求保证）。

        长驻进程豁免：熔断持续超过 ``CONFIG.waf.circuit_reset_after_sec``
        （0 = 永不复位，批爬语义）时自动半开复位、放行后续抓取——IP 标记
        冷却常以小时计，长驻 API 进程里"熔断直到重启"等于把单次故障放大
        成永久不可用（2026-09-24 演示站事故）；冷却后若 WAF 仍在拦，计数
        会重新达阈再次熔断，代价只是阈值次请求。
        """
        with self._lock:
            if not self._circuit_open:
                return
            ttl = CONFIG.waf.circuit_reset_after_sec
            if ttl > 0 and time.monotonic() - self._opened_at >= ttl:
                logger.info(
                    "Circuit breaker for '%s' auto-reset after %ds "
                    "cooldown (half-open); allowing fetches again",
                    self.source_key, ttl,
                )
                self._reset_locked()
                return
            consecutive = (self._failure_streak
                           if self._circuit_reason == "fetch_failure"
                           else self._waf_streak)
            threshold = self._circuit_threshold
            reason = self._circuit_reason
        raise WafCircuitOpenError(
            self.source_key, threshold, consecutive, reason=reason,
        )

    def register_success(self) -> None:
        """登记一次成功抓取：两类连续计数清零。"""
        with self._lock:
            self._waf_streak = 0
            self._failure_streak = 0

    def register_failure(self) -> None:
        """登记一次「尝试耗尽仍未取得内容」的抓取失败（超时/网络错误）。

        与 ``register_waf_page`` 互补：WAF 挑战页有专用计数，而站点侧
        波动（如 CTR 瑞数 WAF 间歇性全页超时）表现为每个 URL 重试耗尽
        返回 None——没有本计数时批次会连续烧穿 enrich attempts。
        连续 ``CONFIG.waf.failure_breaker_threshold`` 次（0 = 禁用）打开
        熔断并抛 WafCircuitOpenError(reason="fetch_failure")。
        """
        with self._lock:
            self._failure_streak += 1
            threshold = CONFIG.waf.failure_breaker_threshold
            if threshold <= 0 or self._failure_streak < threshold:
                return
            self._circuit_threshold = threshold
            self._circuit_reason = "fetch_failure"
            self._circuit_open = True
            self._opened_at = time.monotonic()
        logger.error(
            "Circuit breaker OPEN for '%s': %d consecutive fetch failures "
            "(threshold=%d); aborting fetches for this run",
            self.source_key, self._failure_streak, threshold,
        )
        self._notify_circuit_open(threshold, reason="fetch_failure")
        raise WafCircuitOpenError(self.source_key, threshold,
                                  self._failure_streak,
                                  reason="fetch_failure")

    def register_waf_page(self) -> None:
        """登记一次 WAF 挑战/拦截页；连续次数达到阈值则打开熔断并抛异常。

        - 阈值取 ``CONFIG.waf.circuit_breaker_threshold``（0 = 禁用熔断）；
        - 打开熔断前先经 core.notify 发告警（告警失败只记日志）；
        - 达到阈值时本调用必抛 WafCircuitOpenError，由调用传输层直接
          穿透 attempt 重试循环向上传播。
        """
        with self._lock:
            self._waf_streak += 1
            threshold = CONFIG.waf.circuit_breaker_threshold
            if threshold <= 0 or self._waf_streak < threshold:
                return
            self._circuit_threshold = threshold
            self._circuit_reason = "waf_page"
            self._circuit_open = True
            self._opened_at = time.monotonic()
        logger.error(
            "Circuit breaker OPEN for '%s': %d consecutive WAF pages "
            "(threshold=%d); aborting fetches for this run",
            self.source_key, self._waf_streak, threshold,
        )
        self._notify_circuit_open(threshold)
        raise WafCircuitOpenError(self.source_key, threshold, self._waf_streak)

    def _notify_circuit_open(self, threshold: int,
                             reason: str = "waf_page") -> None:
        """熔断告警：走 core.notify 扇出；任何失败只记日志不影响熔断。"""
        try:
            from core.notify import send_notification  # 延迟导入，失败可兜底

            detail = (
                f"连续 {self._waf_streak} 次抓到 WAF 挑战页"
                if reason == "waf_page"
                else f"连续 {self._failure_streak} 次抓取耗尽（超时/网络错误）"
            )
            send_notification(
                title="WAF 熔断器触发",
                text=(
                    f"数据源 {self.source_key} {detail}"
                    f"（阈值 {threshold}），本轮采集已中止，"
                    "不再发起网络请求以避免加深封锁。"
                ),
                level="error",
            )
        except Exception as exc:  # noqa: BLE001 — 告警失败绝不能影响熔断路径
            logger.warning("Circuit-breaker notify failed: %s", exc)
