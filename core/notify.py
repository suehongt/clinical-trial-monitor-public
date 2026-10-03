"""
Notify — 告警通知模块。

每日定时 pipeline 失败时，向所有已配置的渠道扇出告警：
企业微信群机器人、钉钉群机器人、飞书群机器人、通用 webhook、SMTP 邮件。

- 全部配置通过环境变量驱动，且在调用时才读取（import 时不读），
  便于测试用 monkeypatch.setenv / delenv 隔离。
- ``send_notification`` 永不抛异常：单个渠道失败只记录日志并写入返回值。

环境变量一览（模块内常量集中定义）:

    CT_NOTIFY_WEWORK_WEBHOOK     企业微信群机器人 webhook 地址
    CT_NOTIFY_DINGTALK_WEBHOOK   钉钉群机器人 webhook 地址
    CT_NOTIFY_FEISHU_WEBHOOK     飞书群机器人 webhook 地址
    CT_NOTIFY_WEBHOOK_URL        通用 webhook 地址（POST JSON）
    CT_NOTIFY_SMTP_HOST          SMTP 服务器主机
    CT_NOTIFY_SMTP_PORT          SMTP 端口（缺省 25）
    CT_NOTIFY_SMTP_USER          SMTP 用户名（可选）
    CT_NOTIFY_SMTP_PASSWORD      SMTP 密码（可选）
    CT_NOTIFY_TO                 邮件收件人（逗号分隔）

公开 API:

    configured_channels()   -> list[str]          已配置渠道名
    send_notification(title, text, level="error", timeout=10.0) -> dict
    format_pipeline_message(results, extra="") -> tuple[str, str]
"""
from __future__ import annotations

import logging
import os
import smtplib
from email.header import Header
from email.mime.text import MIMEText
from typing import Callable, Dict, Optional

import requests

logger = logging.getLogger(__name__)

__all__ = [
    "configured_channels",
    "send_notification",
    "format_pipeline_message",
]


# ── 环境变量常量（渠道配置，调用时才读） ─────────────────────────────

ENV_WEWORK_WEBHOOK = "CT_NOTIFY_WEWORK_WEBHOOK"
ENV_DINGTALK_WEBHOOK = "CT_NOTIFY_DINGTALK_WEBHOOK"
ENV_FEISHU_WEBHOOK = "CT_NOTIFY_FEISHU_WEBHOOK"
ENV_WEBHOOK_URL = "CT_NOTIFY_WEBHOOK_URL"
ENV_SMTP_HOST = "CT_NOTIFY_SMTP_HOST"
ENV_SMTP_PORT = "CT_NOTIFY_SMTP_PORT"
ENV_SMTP_USER = "CT_NOTIFY_SMTP_USER"
ENV_SMTP_PASSWORD = "CT_NOTIFY_SMTP_PASSWORD"
ENV_SMTP_TO = "CT_NOTIFY_TO"

# 渠道名 -> 需要非空的环境变量（全部设置才算已配置）
_CHANNEL_ENV: Dict[str, tuple] = {
    "wework": (ENV_WEWORK_WEBHOOK,),
    "dingtalk": (ENV_DINGTALK_WEBHOOK,),
    "feishu": (ENV_FEISHU_WEBHOOK,),
    "webhook": (ENV_WEBHOOK_URL,),
    "email": (ENV_SMTP_HOST, ENV_SMTP_TO),
}

# 告警级别 -> 标题前缀
_LEVEL_TAGS: Dict[str, str] = {
    "error": "[ERROR]",
    "warning": "[WARNING]",
    "info": "[INFO]",
}

# HTTP 请求尝试次数：首次 + 失败重试一次
_HTTP_ATTEMPTS = 2


# ── HTTP 基础设施（模块级可注入，测试 monkeypatch _post_json） ────────


def _post_json(url: str, payload: dict, timeout: float):
    """发送一次 POST JSON 请求，返回响应对象。

    网络错误或非 2xx 状态码时抛异常（交由调用方重试/兜底）。
    """
    resp = requests.post(url, json=payload, timeout=timeout)
    resp.raise_for_status()
    return resp


def _post_json_with_retry(url: str, payload: dict, timeout: float) -> None:
    """POST JSON，失败自动重试一次；仍失败则抛出最后一次异常。"""
    last_exc: Optional[Exception] = None
    for attempt in range(_HTTP_ATTEMPTS):
        try:
            _post_json(url, payload, timeout)
            return
        except Exception as exc:  # noqa: BLE001 — 网络层异常统一兜底
            last_exc = exc
            if attempt + 1 < _HTTP_ATTEMPTS:
                logger.warning("HTTP 通知发送失败（将重试一次）: %s", exc)
    raise last_exc  # type: ignore[misc]


# ── 各渠道发送器（签名统一: title, text, level, timeout；失败抛异常） ──


def _combined_text(title: str, text: str) -> str:
    """标题与正文合并为一条文本。"""
    return f"{title}\n{text}"


def _send_wework(title: str, text: str, level: str, timeout: float) -> None:
    """企业微信群机器人: markdown 消息。"""
    url = os.environ[ENV_WEWORK_WEBHOOK]
    payload = {
        "msgtype": "markdown",
        "markdown": {"content": f"**{title}**\n{text}"},
    }
    _post_json_with_retry(url, payload, timeout)


def _send_dingtalk(title: str, text: str, level: str, timeout: float) -> None:
    """钉钉群机器人: markdown 消息（title 为会话列表预览标题）。"""
    url = os.environ[ENV_DINGTALK_WEBHOOK]
    payload = {
        "msgtype": "markdown",
        "markdown": {"title": title, "text": f"**{title}**\n{text}"},
    }
    _post_json_with_retry(url, payload, timeout)


def _send_feishu(title: str, text: str, level: str, timeout: float) -> None:
    """飞书群机器人: 纯文本消息。"""
    url = os.environ[ENV_FEISHU_WEBHOOK]
    payload = {
        "msg_type": "text",
        "content": {"text": _combined_text(title, text)},
    }
    _post_json_with_retry(url, payload, timeout)


def _send_webhook(title: str, text: str, level: str, timeout: float) -> None:
    """通用 webhook: POST JSON，level 单独作为字段携带。"""
    url = os.environ[ENV_WEBHOOK_URL]
    payload = {"title": title, "text": text, "level": level}
    _post_json_with_retry(url, payload, timeout)


def _send_email(title: str, text: str, level: str, timeout: float) -> None:
    """SMTP 邮件（STARTTLS）。收件人取 CT_NOTIFY_TO（逗号分隔）。"""
    host = os.environ[ENV_SMTP_HOST]
    port = int(os.environ.get(ENV_SMTP_PORT, "25"))
    user = os.environ.get(ENV_SMTP_USER, "")
    password = os.environ.get(ENV_SMTP_PASSWORD, "")
    recipients = [
        addr.strip()
        for addr in os.environ.get(ENV_SMTP_TO, "").split(",")
        if addr.strip()
    ]
    from_addr = user or (recipients[0] if recipients else "")

    msg = MIMEText(text, "plain", "utf-8")
    msg["Subject"] = Header(title, "utf-8")
    msg["From"] = from_addr
    msg["To"] = ", ".join(recipients)

    with smtplib.SMTP(host, port, timeout=timeout) as smtp:
        smtp.starttls()
        if user and password:
            smtp.login(user, password)
        smtp.sendmail(from_addr, recipients, msg.as_string())


# 渠道名 -> 发送器（dict 顺序即扇出顺序）
_CHANNEL_SENDERS: Dict[str, Callable[[str, str, str, float], None]] = {
    "wework": _send_wework,
    "dingtalk": _send_dingtalk,
    "feishu": _send_feishu,
    "webhook": _send_webhook,
    "email": _send_email,
}


# ── 公开 API ─────────────────────────────────────────────────────────


def _channel_configured(channel: str) -> bool:
    """判断渠道所需的环境变量是否全部非空（调用时读取）。"""
    return all(os.environ.get(name) for name in _CHANNEL_ENV[channel])


def configured_channels() -> list[str]:
    """返回当前已配置的渠道名列表（顺序: wework, dingtalk, feishu, webhook, email）。"""
    return [name for name in _CHANNEL_SENDERS if _channel_configured(name)]


def _prefix_level(title: str, level: str) -> str:
    """把告警级别拼到标题前缀，如 ``[ERROR] Pipeline 失败``。

    未知级别透传为大写标签；空级别不加前缀。
    """
    key = (level or "").strip().lower()
    if not key:
        return title
    tag = _LEVEL_TAGS.get(key, f"[{key.upper()}]")
    return f"{tag} {title}"


def send_notification(
    title: str, text: str, level: str = "error", timeout: float = 10.0
) -> dict:
    """向所有已配置渠道扇出告警，返回每个渠道的发送结果。

    返回 dict 形如::

        {"wework": "ok", "dingtalk": "failed", "email": "skipped", ...}

    - ``ok``      发送成功
    - ``failed``  发送失败（异常已记录日志，不向上抛出）
    - ``skipped`` 渠道未配置

    本函数永不抛异常。
    """
    full_title = _prefix_level(title, level)
    results: Dict[str, str] = {}
    for name, sender in _CHANNEL_SENDERS.items():
        try:
            if not _channel_configured(name):
                results[name] = "skipped"
                continue
            sender(full_title, text, level, timeout)
            results[name] = "ok"
        except Exception as exc:  # noqa: BLE001 — 单渠道失败不影响其他渠道
            logger.warning("通知渠道 %s 发送失败: %s", name, exc)
            results[name] = "failed"
    return results


# ── pipeline 结果格式化 ──────────────────────────────────────────────


def _step_detail(step: dict) -> str:
    """提取步骤的耗时 / 失败原因等附加信息。"""
    parts: list[str] = []
    if "seconds" in step:
        try:
            parts.append(f"耗时 {float(step['seconds']):.1f}s")
        except (TypeError, ValueError):
            pass
    if step.get("error"):
        parts.append(str(step["error"]))
    return "; ".join(parts)


def format_pipeline_message(results: list[dict], extra: str = "") -> tuple[str, str]:
    """把 pipeline 各步骤结果列表格式化为 ``(title, text)``。

    ``results`` 每项形如 ``{"step": "Crawl", "ok": True, "seconds": 12.3}``，
    可选 ``"error"`` 字段携带失败原因。失败步骤以 ``[失败]`` 醒目标记，
    并在标题中汇总失败数量。
    """
    total = len(results)
    failed = [r for r in results if not r.get("ok")]
    ok_count = total - len(failed)

    if total == 0:
        title = "临床试验监测 Pipeline: 无步骤结果"
    elif failed:
        title = f"临床试验监测 Pipeline 失败 ({len(failed)}/{total} 步骤失败)"
    else:
        title = f"临床试验监测 Pipeline 成功 ({ok_count}/{total})"

    lines = [f"共 {total} 个步骤: 成功 {ok_count}, 失败 {len(failed)}"]
    if failed:
        names = ", ".join(str(r.get("step", "?")) for r in failed)
        lines.append(f"失败步骤: {names}")
    lines.append("")
    for step in results:
        name = str(step.get("step", "未命名步骤"))
        detail = _step_detail(step)
        suffix = f" ({detail})" if detail else ""
        if step.get("ok"):
            lines.append(f"[成功] {name}{suffix}")
        else:
            lines.append(f"[失败] {name}{suffix}")
    if extra:
        lines.append("")
        lines.append(extra)
    return title, "\n".join(lines)
