"""告警通知出口。

两条通道，凭据与地址都只从运行配置读取：

1. **消息通知服务主题**（主通道）：向主题发布消息，由主题订阅把消息推给各接收端。
   调用发布接口需要项目编号与运行临时凭据，任一缺失即跳过并记录原因。
2. **Webhook 通知地址**（辅助通道）：直接 POST 一段 JSON。地址为空或仍是
   ``<WEBHOOK_URL>`` 占位符时视为未配置，跳过并记录原因。

判定「至少一条通道成功」后才把告警时间写回状态表；两条都失败时不更新冷却标记，
下一条同类日志仍会重试告警，避免消息静默丢失。
"""

import json
from urllib.parse import quote

import net
import signing
from errors import NotificationError

SUBJECT_PREFIX = "SQL Server 错误告警"
MAX_SUBJECT_CHARS = 250


def build_message(error):
    """把一条错误记录渲染成人类可读的告警正文。"""
    title = "%s：%s" % (SUBJECT_PREFIX, error.get("error_number"))
    lines = [
        SUBJECT_PREFIX,
        "错误号: %s" % error.get("error_number"),
        "级别: %s" % error.get("level"),
        "状态: %s" % error.get("state"),
        "行号: %s" % error.get("line"),
        "日志时间: %s" % (error.get("log_timestamp") or "-"),
        "消息: %s" % (error.get("message") or "-"),
        "原文: %s" % (error.get("full_message") or "-"),
        "日志组: %s" % (error.get("log_group_id") or "-"),
        "日志流: %s" % (error.get("log_stream_id") or "-"),
        "来源主机: %s" % (error.get("host") or "-"),
        "解析规则: %s" % (error.get("rule") or "-"),
    ]
    return title, "\n".join(lines)


class Notifier(object):
    """通知发送器。

    :param transport: 底层 HTTP 调用，单测注入假实现
    """

    def __init__(self, cfg, logger, credentials=None, transport=None):
        self._cfg = cfg
        self._logger = logger
        self._credentials = credentials
        self._transport = transport or _default_transport

    # ------------------------------------------------------------------
    def send(self, error):
        """按主/辅两条通道发送一条告警，返回逐通道结果。"""
        title, text = build_message(error)
        result = {"title": title, "channels": {}}

        result["channels"]["smn"] = self._publish_to_topic(title, text, error)
        result["channels"]["webhook"] = self._post_webhook(title, text, error)

        delivered = any(
            str(status).startswith("sent") for status in result["channels"].values()
        )
        result["delivered"] = delivered
        if not delivered:
            self._logger.error(
                "所有通知通道均未送达",
                error_number=error.get("error_number"),
                channels=result["channels"],
            )
        return result

    # ------------------------------------------------------------------
    # 主通道：消息通知服务主题
    # ------------------------------------------------------------------
    def _publish_to_topic(self, title, text, error):
        if not self._cfg.smn_enabled():
            reason = "topic_urn 未配置" if not self._cfg.smn_topic_urn else "项目编号未配置"
            self._logger.warn("跳过消息通知主题发布", reason=reason)
            return "skipped:%s" % reason
        if self._credentials is None or not self._credentials.available():
            self._logger.warn("跳过消息通知主题发布", reason="运行凭据不可用")
            return "skipped:运行凭据不可用"

        host = self._cfg.smn_endpoint()
        path = "/v2/%s/notifications/topics/%s/publish" % (
            self._cfg.project_id,
            quote(str(self._cfg.smn_topic_urn), safe=":"),
        )
        payload = {"message": text, "subject": title[:MAX_SUBJECT_CHARS]}
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        def attempt(_attempt_no):
            headers, url = signing.sign_platform_request(
                host=host,
                path=path,
                method="POST",
                headers={"Content-Type": "application/json"},
                body=body,
                credentials=self._credentials,
            )
            try:
                self._transport(url=url, headers=headers, body=body,
                                timeout=self._cfg.http_timeout_seconds, verify=True)
            except net.TransportError as exc:
                raise NotificationError(
                    "消息通知主题发布失败",
                    code=exc.code,
                    status=exc.status,
                    retryable=exc.retryable,
                )

        try:
            net.call_with_retry(
                attempt,
                logger=self._logger,
                activity="消息通知主题发布",
                max_attempts=self._cfg.max_attempts,
                backoff_seconds=self._cfg.retry_backoff_seconds,
            )
        except NotificationError as exc:
            self._logger.error(
                "消息通知主题发布失败",
                error_number=error.get("error_number"),
                code=exc.code,
                status=exc.status,
            )
            return "failed:%s" % (exc.code or exc.status or "unknown")

        self._logger.info("已发布到消息通知主题",
                          error_number=error.get("error_number"),
                          topic_urn=self._cfg.smn_topic_urn)
        return "sent:smn"

    # ------------------------------------------------------------------
    # 辅助通道：Webhook 通知地址
    # ------------------------------------------------------------------
    def _post_webhook(self, title, text, error):
        if not self._cfg.webhook_enabled():
            self._logger.warn("跳过 Webhook 通知", reason="通知地址未配置")
            return "skipped:通知地址未配置"

        payload = {
            "text": text,
            "title": title,
            "error_number": error.get("error_number"),
            "level": error.get("level"),
            "state": error.get("state"),
            "log_timestamp": error.get("log_timestamp"),
            "message": error.get("message"),
            "log_group_id": error.get("log_group_id"),
            "log_stream_id": error.get("log_stream_id"),
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}

        def attempt(_attempt_no):
            try:
                self._transport(url=str(self._cfg.webhook_url), headers=headers, body=body,
                                timeout=self._cfg.http_timeout_seconds, verify=True)
            except net.TransportError as exc:
                raise NotificationError(
                    "Webhook 通知失败",
                    code=exc.code,
                    status=exc.status,
                    retryable=exc.retryable,
                )

        try:
            net.call_with_retry(
                attempt,
                logger=self._logger,
                activity="Webhook 通知",
                max_attempts=self._cfg.max_attempts,
                backoff_seconds=self._cfg.retry_backoff_seconds,
            )
        except NotificationError as exc:
            self._logger.error(
                "Webhook 通知失败",
                error_number=error.get("error_number"),
                code=exc.code,
                status=exc.status,
            )
            return "failed:%s" % (exc.code or exc.status or "unknown")

        self._logger.info("已发送 Webhook 通知", error_number=error.get("error_number"))
        return "sent:webhook"


def _default_transport(url, headers, body, timeout, verify):
    return net.post_json(url, body, headers=headers, timeout=timeout, verify=verify)
