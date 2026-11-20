"""最小 HTTP 客户端与重试封装。

只用标准库：请求体是 JSON，响应体按 JSON 解析，非 2xx 一律抛
:class:`TransportError` 并带上对端返回体，由上层判断是「可重试」还是「终止」。

重试上限与退避在 :func:`call_with_retry` 里统一实现，各调用点不再各写一套。
"""

import json
import socket
import ssl
import time
from urllib import error as urlerror
from urllib import request as urlrequest

from errors import MonitoringError

RETRYABLE_STATUS = (408, 429, 500, 502, 503, 504)


class TransportError(MonitoringError):
    """一次 HTTP 调用得到非 2xx 响应。"""

    def __init__(self, message, status=None, body=None, code=None, retryable=False, **details):
        super(TransportError, self).__init__(message, status=status, code=code, **details)
        self.status = status
        self.body = body
        self.code = code
        self.retryable = retryable

    def __repr__(self):
        return "TransportError(status=%s, code=%s, retryable=%s)" % (
            self.status, self.code, self.retryable)


def _ssl_context(verify):
    if verify:
        return ssl.create_default_context()
    # 状态存储走 VPC 内私有地址，证书由实例侧签发，无法用公网 CA 校验域名
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def parse_error_code(body_text):
    """从对端返回体里取异常类名。

    兼容协议的错误体把类名写成 ``<命名空间>#<类名>``，这里只取最后一个片段，
    因此不需要在代码里出现任何带命名空间的完整类名。
    """
    if not body_text:
        return None
    try:
        payload = json.loads(body_text)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    raw = payload.get("__type") or payload.get("code") or payload.get("error_code")
    if not raw:
        return None
    return str(raw).split("#")[-1].strip() or None


def post_json(url, payload, headers=None, timeout=10, verify=True):
    """POST 一段 JSON，返回解析后的响应体（解析失败时返回原始文本）。

    :param payload: 可 JSON 序列化的对象，或已经序列化好的字符串/字节串
    """
    if isinstance(payload, (bytes, bytearray)):
        body = bytes(payload)
    elif isinstance(payload, str):
        body = payload.encode("utf-8")
    else:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    request_headers = {"Content-Type": "application/json"}
    for key, value in (headers or {}).items():
        if value is not None:
            request_headers[key] = str(value)

    req = urlrequest.Request(url, data=body, headers=request_headers, method="POST")
    try:
        with urlrequest.urlopen(req, timeout=timeout, context=_ssl_context(verify)) as resp:
            text = resp.read().decode("utf-8", "replace")
            return _decode(text, resp.status)
    except urlerror.HTTPError as exc:
        text = exc.read().decode("utf-8", "replace") if exc.fp else ""
        code = parse_error_code(text)
        raise TransportError(
            "对端返回非成功状态",
            status=exc.code,
            body=text,
            code=code,
            retryable=exc.code in RETRYABLE_STATUS,
            url=url,
        )
    except (urlerror.URLError, socket.timeout, ssl.SSLError) as exc:
        # 连接层失败（超时、域名不可达、TLS 握手失败）按可重试处理
        raise TransportError(
            "网络调用失败",
            status=None,
            body=str(exc),
            code=exc.__class__.__name__,
            retryable=True,
            url=url,
        )


def _decode(text, status):
    if not text:
        return {}
    try:
        return json.loads(text)
    except ValueError:
        return {"_raw": text, "_status": status}


def call_with_retry(fn, logger=None, activity="调用", max_attempts=3, backoff_seconds=0.5):
    """执行 ``fn(attempt)``，仅对可重试失败按指数退避重试。

    达到上限后原样抛出最后一次异常——调用方必须显式处理，不吞异常。
    """
    last_exc = None
    for attempt in range(1, max(1, int(max_attempts)) + 1):
        try:
            return fn(attempt)
        except Exception as exc:  # noqa: BLE001 - 需要按 retryable 分流后再决定
            last_exc = exc
            retryable = bool(getattr(exc, "retryable", False))
            if not retryable or attempt >= max_attempts:
                raise
            wait = backoff_seconds * (2 ** (attempt - 1))
            if logger is not None:
                logger.warn(
                    "外部调用失败，准备重试",
                    activity=activity,
                    attempt=attempt,
                    max_attempts=max_attempts,
                    retry_in_seconds=round(wait, 3),
                    error=_describe(exc),
                )
            time.sleep(wait)
    raise last_exc  # 理论上不可达，保留兜底


def _describe(exc):
    if exc is None:
        return None
    detail = getattr(exc, "details", None)
    if isinstance(detail, dict):
        return "%s: %s (%s)" % (type(exc).__name__, exc, json.dumps(detail, default=str)[:200])
    return "%s: %s" % (type(exc).__name__, exc)
