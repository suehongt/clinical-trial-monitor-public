"""Tests for core.notify — 全部离线，HTTP 与 SMTP 均用 monkeypatch 替身，不访问网络。"""
from __future__ import annotations

import email
from email.header import Header
from email.mime.text import MIMEText

import pytest

from core import notify
from core.notify import (
    configured_channels,
    format_pipeline_message,
    send_notification,
)


# 通知模块用到的全部环境变量（测试前后保证隔离）
ALL_ENV_VARS = [
    "CT_NOTIFY_WEWORK_WEBHOOK",
    "CT_NOTIFY_DINGTALK_WEBHOOK",
    "CT_NOTIFY_FEISHU_WEBHOOK",
    "CT_NOTIFY_WEBHOOK_URL",
    "CT_NOTIFY_SMTP_HOST",
    "CT_NOTIFY_SMTP_PORT",
    "CT_NOTIFY_SMTP_USER",
    "CT_NOTIFY_SMTP_PASSWORD",
    "CT_NOTIFY_TO",
]

ENV_URLS = {
    "wework": "https://example.com/wework/hook",
    "dingtalk": "https://example.com/dingtalk/hook",
    "feishu": "https://example.com/feishu/hook",
    "webhook": "https://example.com/generic/hook",
}

ENV_KEYS = {
    "wework": "CT_NOTIFY_WEWORK_WEBHOOK",
    "dingtalk": "CT_NOTIFY_DINGTALK_WEBHOOK",
    "feishu": "CT_NOTIFY_FEISHU_WEBHOOK",
    "webhook": "CT_NOTIFY_WEBHOOK_URL",
}


@pytest.fixture
def clean_env(monkeypatch):
    """清除全部通知相关环境变量，杜绝污染真实环境 / 用例间串扰。"""
    for name in ALL_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


class FakeResponse:
    """离线替身: 仅实现 _post_json 用到的 raise_for_status。"""

    def __init__(self, status_code: int = 200):
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def install_recorder(monkeypatch, fail_urls=(), status_by_url=None):
    """把 notify._post_json 换成离线记录器，返回 calls 列表。"""
    calls = []

    def fake_post_json(url, payload, timeout):
        calls.append({"url": url, "payload": payload, "timeout": timeout})
        if url in fail_urls:
            raise RuntimeError(f"boom: {url}")
        resp = FakeResponse((status_by_url or {}).get(url, 200))
        resp.raise_for_status()
        return resp

    monkeypatch.setattr(notify, "_post_json", fake_post_json)
    return calls


def configure_http_channels(monkeypatch):
    """配置全部 4 个 HTTP 渠道（不含 email）。"""
    for channel, url in ENV_URLS.items():
        monkeypatch.setenv(ENV_KEYS[channel], url)


# ═══════════════════════════════════════════════════════════════════════
#  configured_channels
# ═══════════════════════════════════════════════════════════════════════


class TestConfiguredChannels:
    def test_none_configured(self, clean_env):
        assert configured_channels() == []

    def test_single_channel(self, clean_env):
        clean_env.setenv("CT_NOTIFY_WEWORK_WEBHOOK", ENV_URLS["wework"])
        assert configured_channels() == ["wework"]

    def test_multiple_channels_order(self, clean_env):
        configure_http_channels(clean_env)
        assert configured_channels() == ["wework", "dingtalk", "feishu", "webhook"]

    def test_empty_value_not_configured(self, clean_env):
        clean_env.setenv("CT_NOTIFY_FEISHU_WEBHOOK", "")
        assert configured_channels() == []

    def test_email_requires_host_and_to(self, clean_env):
        clean_env.setenv("CT_NOTIFY_SMTP_HOST", "smtp.example.com")
        assert configured_channels() == []
        clean_env.setenv("CT_NOTIFY_TO", "a@example.com")
        assert configured_channels() == ["email"]

    def test_email_with_port_and_credentials(self, clean_env):
        clean_env.setenv("CT_NOTIFY_SMTP_HOST", "smtp.example.com")
        clean_env.setenv("CT_NOTIFY_SMTP_PORT", "465")
        clean_env.setenv("CT_NOTIFY_SMTP_USER", "u")
        clean_env.setenv("CT_NOTIFY_SMTP_PASSWORD", "p")
        clean_env.setenv("CT_NOTIFY_TO", "a@example.com,b@example.com")
        assert configured_channels() == ["email"]


# ═══════════════════════════════════════════════════════════════════════
#  send_notification — payload 格式 / 扇出聚合
# ═══════════════════════════════════════════════════════════════════════


class TestSendNotificationPayloads:
    def test_fanout_all_http_channels(self, clean_env, monkeypatch):
        configure_http_channels(clean_env)
        calls = install_recorder(monkeypatch)
        results = send_notification("Pipeline 失败", "Crawl 步骤出错")

        assert results == {
            "wework": "ok",
            "dingtalk": "ok",
            "feishu": "ok",
            "webhook": "ok",
            "email": "skipped",  # 未配置 → skipped
        }
        assert len(calls) == 4
        by_url = {c["url"]: c for c in calls}

        # 企业微信: markdown content 合并标题与正文
        wework = by_url[ENV_URLS["wework"]]["payload"]
        assert wework["msgtype"] == "markdown"
        assert "[ERROR] Pipeline 失败" in wework["markdown"]["content"]
        assert "Crawl 步骤出错" in wework["markdown"]["content"]

        # 钉钉: markdown title + text
        dingtalk = by_url[ENV_URLS["dingtalk"]]["payload"]
        assert dingtalk["msgtype"] == "markdown"
        assert dingtalk["markdown"]["title"] == "[ERROR] Pipeline 失败"
        assert "Crawl 步骤出错" in dingtalk["markdown"]["text"]

        # 飞书: 纯文本
        feishu = by_url[ENV_URLS["feishu"]]["payload"]
        assert feishu["msg_type"] == "text"
        assert "[ERROR] Pipeline 失败" in feishu["content"]["text"]
        assert "Crawl 步骤出错" in feishu["content"]["text"]

        # 通用 webhook: title / text / level 三字段
        generic = by_url[ENV_URLS["webhook"]]["payload"]
        assert generic == {
            "title": "[ERROR] Pipeline 失败",
            "text": "Crawl 步骤出错",
            "level": "error",
        }

    def test_timeout_passed_through(self, clean_env, monkeypatch):
        clean_env.setenv("CT_NOTIFY_WEBHOOK_URL", ENV_URLS["webhook"])
        calls = install_recorder(monkeypatch)
        send_notification("t", "b", timeout=3.5)
        assert calls[0]["timeout"] == 3.5

    def test_level_prefixes(self, clean_env, monkeypatch):
        clean_env.setenv("CT_NOTIFY_WEBHOOK_URL", ENV_URLS["webhook"])
        calls = install_recorder(monkeypatch)

        send_notification("t", "b", level="warning")
        assert calls[-1]["payload"]["title"].startswith("[WARNING] ")
        send_notification("t", "b", level="info")
        assert calls[-1]["payload"]["title"].startswith("[INFO] ")
        send_notification("t", "b")
        assert calls[-1]["payload"]["title"].startswith("[ERROR] ")
        # 未知级别透传为大写标签
        send_notification("t", "b", level="critical")
        assert calls[-1]["payload"]["title"].startswith("[CRITICAL] ")
        assert calls[-1]["payload"]["level"] == "critical"

    def test_unconfigured_channels_skipped(self, clean_env, monkeypatch):
        clean_env.setenv("CT_NOTIFY_WEWORK_WEBHOOK", ENV_URLS["wework"])
        calls = install_recorder(monkeypatch)
        results = send_notification("t", "b")
        assert results == {
            "wework": "ok",
            "dingtalk": "skipped",
            "feishu": "skipped",
            "webhook": "skipped",
            "email": "skipped",
        }
        assert len(calls) == 1


# ═══════════════════════════════════════════════════════════════════════
#  send_notification — 失败隔离与重试
# ═══════════════════════════════════════════════════════════════════════


class TestFailureIsolation:
    def test_single_channel_failure_does_not_raise(self, clean_env, monkeypatch):
        configure_http_channels(clean_env)
        calls = install_recorder(monkeypatch, fail_urls={ENV_URLS["dingtalk"]})
        # 不抛异常即为通过
        results = send_notification("t", "b")
        assert results["dingtalk"] == "failed"
        assert results["wework"] == "ok"
        assert results["feishu"] == "ok"
        assert results["webhook"] == "ok"
        # 失败渠道重试一次: 首次 + 重试 = 2 次调用
        assert len([c for c in calls if c["url"] == ENV_URLS["dingtalk"]]) == 2

    def test_http_500_is_failure_with_retry(self, clean_env, monkeypatch):
        clean_env.setenv("CT_NOTIFY_WEBHOOK_URL", ENV_URLS["webhook"])
        calls = install_recorder(
            monkeypatch, status_by_url={ENV_URLS["webhook"]: 500}
        )
        results = send_notification("t", "b")
        assert results["webhook"] == "failed"
        assert len(calls) == 2  # 非 2xx 同样重试一次

    def test_retry_then_success(self, clean_env, monkeypatch):
        clean_env.setenv("CT_NOTIFY_DINGTALK_WEBHOOK", ENV_URLS["dingtalk"])
        attempts = {"n": 0}

        def flaky_post_json(url, payload, timeout):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise RuntimeError("transient network error")
            return FakeResponse()

        monkeypatch.setattr(notify, "_post_json", flaky_post_json)
        results = send_notification("t", "b")
        assert results["dingtalk"] == "ok"
        assert attempts["n"] == 2

    def test_all_channels_fail_still_returns_dict(self, clean_env, monkeypatch):
        configure_http_channels(clean_env)
        install_recorder(
            monkeypatch,
            fail_urls=set(ENV_URLS.values()),
        )
        results = send_notification("t", "b")
        assert results == {
            "wework": "failed",
            "dingtalk": "failed",
            "feishu": "failed",
            "webhook": "failed",
            "email": "skipped",
        }


# ═══════════════════════════════════════════════════════════════════════
#  send_notification — SMTP 渠道
# ═══════════════════════════════════════════════════════════════════════


def make_fake_smtp(monkeypatch, starttls_error=None):
    """替换 smtplib.SMTP 为离线替身类，返回实例收集列表。"""
    instances = []

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            self.host = host
            self.port = port
            self.timeout = timeout
            self.starttls_called = False
            self.login_args = None
            self.mail_args = None
            instances.append(self)

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def starttls(self):
            if starttls_error is not None:
                raise starttls_error
            self.starttls_called = True

        def login(self, user, password):
            self.login_args = (user, password)

        def sendmail(self, from_addr, to_addrs, body):
            self.mail_args = (from_addr, to_addrs, body)

    monkeypatch.setattr(notify.smtplib, "SMTP", FakeSMTP)
    return instances


class TestEmailChannel:
    def configure(self, monkeypatch):
        monkeypatch.setenv("CT_NOTIFY_SMTP_HOST", "smtp.example.com")
        monkeypatch.setenv("CT_NOTIFY_SMTP_PORT", "587")
        monkeypatch.setenv("CT_NOTIFY_SMTP_USER", "alerter@example.com")
        monkeypatch.setenv("CT_NOTIFY_SMTP_PASSWORD", "secret")
        monkeypatch.setenv("CT_NOTIFY_TO", "a@example.com, b@example.com")

    def test_email_sent_with_starttls_and_login(self, clean_env, monkeypatch):
        self.configure(monkeypatch)
        instances = make_fake_smtp(monkeypatch)
        results = send_notification("Pipeline 失败", "正文内容", timeout=7.5)

        assert results["email"] == "ok"
        assert len(instances) == 1
        smtp = instances[0]
        assert smtp.host == "smtp.example.com"
        assert smtp.port == 587
        assert smtp.timeout == 7.5
        assert smtp.starttls_called is True
        assert smtp.login_args == ("alerter@example.com", "secret")
        from_addr, to_addrs, body = smtp.mail_args
        assert from_addr == "alerter@example.com"
        assert to_addrs == ["a@example.com", "b@example.com"]  # 逗号分隔并去空白
        # 邮件结构与手工构造的 MIME 消息完全一致（标题带级别前缀进 Subject）
        expected = MIMEText("正文内容", "plain", "utf-8")
        expected["Subject"] = Header("[ERROR] Pipeline 失败", "utf-8")
        expected["From"] = "alerter@example.com"
        expected["To"] = "a@example.com, b@example.com"
        assert body == expected.as_string()
        # 正文解码后即告警文本
        decoded = email.message_from_string(body).get_payload(decode=True)
        assert decoded.decode("utf-8") == "正文内容"

    def test_default_port_25(self, clean_env, monkeypatch):
        monkeypatch.setenv("CT_NOTIFY_SMTP_HOST", "smtp.example.com")
        monkeypatch.setenv("CT_NOTIFY_TO", "a@example.com")
        instances = make_fake_smtp(monkeypatch)
        results = send_notification("t", "b")
        assert results["email"] == "ok"
        assert instances[0].port == 25

    def test_no_login_without_credentials(self, clean_env, monkeypatch):
        monkeypatch.setenv("CT_NOTIFY_SMTP_HOST", "smtp.example.com")
        monkeypatch.setenv("CT_NOTIFY_TO", "a@example.com")
        instances = make_fake_smtp(monkeypatch)
        send_notification("t", "b")
        assert instances[0].login_args is None

    def test_email_failure_isolated(self, clean_env, monkeypatch):
        self.configure(monkeypatch)
        make_fake_smtp(monkeypatch, starttls_error=RuntimeError("TLS refused"))
        results = send_notification("t", "b")
        assert results["email"] == "failed"  # 不抛异常
        assert results["wework"] == "skipped"


# ═══════════════════════════════════════════════════════════════════════
#  format_pipeline_message
# ═══════════════════════════════════════════════════════════════════════


class TestFormatPipelineMessage:
    def test_all_ok(self):
        title, text = format_pipeline_message(
            [
                {"step": "Crawl", "ok": True, "seconds": 12.3},
                {"step": "Translate", "ok": True, "seconds": 3.2},
            ]
        )
        assert "成功" in title
        assert "失败" not in title
        assert "[成功] Crawl" in text
        assert "12.3s" in text
        assert "[失败]" not in text

    def test_failed_steps_marked(self):
        results = [
            {"step": "Crawl", "ok": True, "seconds": 12.3},
            {"step": "Translate", "ok": False, "seconds": 1.0, "error": "API 超限"},
            {"step": "Report", "ok": False, "error": "写库失败"},
        ]
        title, text = format_pipeline_message(results)
        assert "Pipeline 失败" in title
        assert "2/3" in title
        # 失败步骤醒目: 汇总行 + 逐条标记
        assert "失败步骤: Translate, Report" in text
        assert "[失败] Translate" in text
        assert "[失败] Report" in text
        assert "API 超限" in text
        assert "写库失败" in text
        # 成功步骤不受影响
        assert "[成功] Crawl" in text

    def test_extra_appended(self):
        title, text = format_pipeline_message(
            [{"step": "Crawl", "ok": True}], extra="详情见日志 /var/log/monitor.log"
        )
        assert "详情见日志 /var/log/monitor.log" in text.split("\n")[-1]

    def test_empty_results(self):
        title, text = format_pipeline_message([])
        assert "无步骤结果" in title

    def test_invalid_seconds_ignored(self):
        title, text = format_pipeline_message(
            [{"step": "Crawl", "ok": True, "seconds": "not-a-number"}]
        )
        assert "[成功] Crawl" in text  # 不抛异常，跳过耗时展示

    def test_roundtrip_with_send_notification(self, clean_env, monkeypatch):
        """format_pipeline_message 的输出可直接作为 send_notification 入参。"""
        clean_env.setenv("CT_NOTIFY_WEBHOOK_URL", ENV_URLS["webhook"])
        calls = install_recorder(monkeypatch)
        results = [
            {"step": "Crawl", "ok": False, "error": "connection refused"},
            {"step": "Report", "ok": True, "seconds": 2.0},
        ]
        title, text = format_pipeline_message(results)
        status = send_notification(title, text, level="error")
        assert status["webhook"] == "ok"
        assert "[ERROR]" in calls[-1]["payload"]["title"]
        assert "Pipeline 失败" in calls[-1]["payload"]["title"]
