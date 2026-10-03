"""WAF HTTP 传输层：requests + 离线 acw_sc__v2 挑战求解（无浏览器热路径）。

实测依据（2026-09-12，详见 docs/data-source-comparison.md「反爬机制详解」
与「同类问题与开源解法」）：

  - ChiCTR 受保护路径对浏览器 TLS 下发 JS 挑战（HTTP 200 + ~17KB 挑战页，
    ``var arg1='...'``）；对 requests 无 cookie 时同样下发挑战页（curl 才
    直接 405）。
  - 挑战可离线求解：固定置换表 + 与固定 key 异或（RSSHub getAcwScV2ByArg1
    同算法），得到的 ``acw_sc__v2`` cookie 即通行证——列表/详情/翻页实测
    3/3 通过，cookie 有效期 3600s。
  - 封禁是会话级的：被 405 的会话原地重试无意义，清 cookie 重获挑战即恢复。
  - 阿里云已出现新版挑战（``aliyun_waf_aa``/``aliyun_waf_bb``，需真实执行
    JS，离线算法解不出，参见 chictr-mcp-server/metapi#611 案例）——本层
    检测到即抛 :class:`WafChallengeNewVersion`，由采集器降级浏览器路径。

传输层不管节律：调用方（采集器 enrich/discover 循环）继续按
``cfg.request_delay_sec`` 控速；本层仅在 405 恢复前短暂等待，避免在同一
被标记会话上立即重试。熔断状态机与 Playwright 路径共享（core/waf_guard）。
"""
from __future__ import annotations

import logging
import re
import time
from typing import Callable, Optional

import requests

from config import CONFIG
from core.waf_guard import WafCircuitOpenError, WafGuard

logger = logging.getLogger(__name__)

__all__ = ["solve_acw_sc_v2", "WafChallengeNewVersion", "WafHttpClient",
           "WafCircuitOpenError"]

# ── acw_sc__v2 离线求解（RSSHub getAcwScV2ByArg1 算法移植） ──────────────
#
# 挑战页服务端每次随机下发 40 位十六进制 arg1；token = hexXor(unsbox(arg1), PWD)。
# 2026-09-12 对 www.chictr.org.cn 实测端到端通过（3/3 真实内容）。

# 置换表：res[j] = arg1[pos[j] - 1]（1-based 位置重排）
_ACW_POS = [
    15, 35, 29, 24, 33, 16, 1, 38, 10, 9, 19, 31, 40, 27, 22, 23, 25, 13, 6,
    11, 39, 18, 20, 8, 14, 21, 32, 26, 2, 30, 7, 4, 17, 5, 3, 28, 34, 37, 12,
    36,
]
# 固定异或 key（阿里系 acw_sc__v2 公开常量）
_ACW_PWD = "3000176000856006061501533003690027800375"

_ARG1_RE = re.compile(r"var arg1='([0-9A-Fa-f]{40})'")


def solve_acw_sc_v2(arg1: str) -> str:
    """由挑战页 arg1 计算 acw_sc__v2 cookie 值（纯函数，可单测）。

    arg1 必须是 40 位十六进制串；输出为 40 位十六进制 token。
    """
    if len(arg1) != 40 or not _ARG1_RE.match(f"var arg1='{arg1}'"):
        raise ValueError(f"arg1 must be 40 hex chars, got: {arg1!r}")
    box = "".join(arg1[pos - 1] for pos in _ACW_POS)
    return "".join(
        format(int(box[i:i + 2], 16) ^ int(_ACW_PWD[i:i + 2], 16), "02x")
        for i in range(0, 40, 2)
    )


class WafChallengeNewVersion(RuntimeError):
    """站点下发了新版阿里云挑战（aliyun_waf_aa/bb），离线算法无法求解。

    调用方应捕获后降级到浏览器传输层（真实执行 JS 是当前唯一可靠解法）。
    """

    def __init__(self, url: str):
        self.url = url
        super().__init__(
            "New-version Aliyun WAF challenge (aliyun_waf_aa/bb) detected "
            f"at {url}; offline acw_sc__v2 solving does not apply — "
            "fall back to the browser transport."
        )


class WafHttpClient:
    """requests 会话 + 挑战自动求解的 WAF 页面抓取器。

    与 ``WAFBrowserCollector._fetch_page`` 同构的契约：

    - 成功（内容判定通过）→ 返回 HTML；
    - 重试耗尽 → 返回 ``None``（调用方按「WAF 拦截」处理）；
    - 熔断打开 → 抛 :class:`WafCircuitOpenError`（穿透，不计入普通重试）；
    - 新版挑战 → 抛 :class:`WafChallengeNewVersion`（触发降级）。

    ``content_ok(html)`` 注入内容判定（采集器既有 marker 逻辑），同时承担
    「拿到 cookie 但仍被拦」的假成功防护。

    节律地板：``CONFIG.waf.min_request_interval_sec``（默认 4.5s）在传输层
    强制相邻两次请求的最小间隔——HTTP 路径远快于浏览器，没有地板会突破
    WAF 频控（waf-bypass-upgrade-plan §2.6「节律守恒」）。调用方自身的
    ``cfg.request_delay_sec`` 睡眠叠加在其上。
    """

    MAX_ATTEMPTS = 3          # 单 URL：首发 + 挑战重试 + 封禁恢复各占其一
    BLOCK_RECOVER_WAIT_SEC = 2.0  # 405/未知拦截页清 cookie 后的冷却

    def __init__(self, source_key: str, content_ok: Callable[[str], bool],
                 user_agent: str, timeout_ms: int = 25000,
                 guard: Optional[WafGuard] = None):
        self.source_key = source_key
        self._content_ok = content_ok
        self.timeout_sec = timeout_ms / 1000
        # The collector may inject its browser-path guard so transport
        # fallback, public state projections and reset_circuit() all observe
        # one circuit rather than two independent state machines.
        self.guard = guard if guard is not None else WafGuard(source_key)
        self.solved_count = 0   # 观测指标：本次 run 离线求解次数（G6）
        self.blocked_count = 0  # 观测指标：405 拦截页次数
        self._last_request_ts: float = 0.0  # 节律地板基准（单调节律）
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": user_agent,
            "Accept-Language": "zh-CN,zh;q=0.9",
        })
        if CONFIG.waf.proxy_url:
            self.session.proxies = {
                "http": CONFIG.waf.proxy_url,
                "https": CONFIG.waf.proxy_url,
            }

    # ── 单 URL 抓取 ──────────────────────────────────────────────────

    def _pace(self) -> None:
        """节律地板：距上次请求不足 min_request_interval_sec 时补足等待。"""
        floor = CONFIG.waf.min_request_interval_sec
        if floor <= 0:
            return
        elapsed = time.monotonic() - self._last_request_ts
        if elapsed < floor:
            time.sleep(floor - elapsed)

    def get(self, url: str) -> Optional[str]:
        """抓取一个页面（含挑战/封禁自动恢复）。契约见类 docstring。"""
        self.guard.ensure_closed()

        for attempt in range(1, self.MAX_ATTEMPTS + 1):
            self._pace()
            self._last_request_ts = time.monotonic()
            try:
                resp = self.session.get(url, timeout=self.timeout_sec)
            except requests.RequestException as exc:
                logger.warning("WafHTTP request error %s (attempt %d/%d): %s",
                               url, attempt, self.MAX_ATTEMPTS, exc)
                continue

            if resp.status_code == 405:
                # 阿里云 405 拦截页：会话/IP 被标记，原地重试无意义——
                # 计熔断 streak，并清 cookie 换新挑战（会话级恢复路径）。
                self.blocked_count += 1
                logger.warning("WafHTTP 405 block page on %s "
                               "(attempt %d/%d, blocked=%d)",
                               url, attempt, self.MAX_ATTEMPTS,
                               self.blocked_count)
                self.guard.register_waf_page()  # 达阈值时在此抛出
                self.session.cookies.clear()
                time.sleep(self.BLOCK_RECOVER_WAIT_SEC)
                continue

            html = resp.text
            if self._content_ok(html):
                self.guard.register_success()
                return html

            if "aliyun_waf_aa" in html or "aliyun_waf_bb" in html:
                raise WafChallengeNewVersion(url)

            arg1 = _ARG1_RE.search(html)
            if arg1:
                token = solve_acw_sc_v2(arg1.group(1))
                self.session.cookies.set("acw_sc__v2", token)
                self.solved_count += 1
                logger.info("WafHTTP solved acw_sc__v2 for '%s' "
                            "(attempt %d/%d, solved=%d)",
                            self.source_key, attempt, self.MAX_ATTEMPTS,
                            self.solved_count)
                continue  # 带 token 立即重试

            # 非 405、无 arg1、无内容 marker 的 200 页：按 WAF 页处理
            logger.warning("WafHTTP non-content 200 page (%d bytes) on %s "
                           "(attempt %d/%d)", len(html), url, attempt,
                           self.MAX_ATTEMPTS)
            self.guard.register_waf_page()  # 达阈值时在此抛出
            self.session.cookies.clear()
            time.sleep(self.BLOCK_RECOVER_WAIT_SEC)

        logger.error("WafHTTP all %d attempts exhausted for %s",
                     self.MAX_ATTEMPTS, url)
        # 连续失败熔断（与 WAF 页计数互补）：全为请求异常耗尽时在此止损。
        self.guard.register_failure()
        return None
