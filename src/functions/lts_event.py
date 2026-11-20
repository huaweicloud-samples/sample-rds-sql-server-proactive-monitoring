"""日志投递事件解包。

事件结构以日志服务的投递事件为准（不是压缩载荷那类形态）：

.. code-block:: json

    {
      "lts": {
        "data": "<Base64 编码的 JSON 字符串>"
      }
    }

``data`` 解 Base64 后是：

.. code-block:: json

    {
      "logs": "[{...}]",            // 日志条目数组，投递侧可能直接给字符串
      "owner": "<账号标识>",
      "log_group_id": "<日志组 ID>",
      "log_topic_id": "<日志流 ID>"
    }

单条日志条目字段：

.. code-block:: json

    {
      "message": "<日志原文>",       // 也可能是 content
      "time": 1530009653059,         // 毫秒时间戳，亦兼容 btime / gtimestamp
      "host_name": "<主机名>",
      "ip": "<来源地址>",
      "path": "<日志文件路径>",
      "log_uid": "<日志唯一标识>",
      "line_no": 1
    }

本模块对以上结构做宽松解包：字段名与嵌套形态有差异时逐个尝试，只有整份事件
都不是可识别结构时才判定为事件格式错误。
"""

import base64
import json

# 单条日志原文的候选字段名（按优先级）
CONTENT_KEYS = ("content", "message", "log", "text")
# 毫秒时间戳的候选字段名
TIME_KEYS = ("time", "btime", "gtimestamp", "timestamp", "log_time")
HOST_KEYS = ("host_name", "hostname", "host")

EPOCH_SECONDS_THRESHOLD = 10 ** 11


class LogRecord(object):
    """一条待判定的日志原文。"""

    __slots__ = ("content", "timestamp_ms", "host", "ip", "path", "log_uid", "line_no", "raw")

    def __init__(self, content, timestamp_ms, host=None, ip=None, path=None,
                 log_uid=None, line_no=None, raw=None):
        self.content = content
        self.timestamp_ms = timestamp_ms
        self.host = host
        self.ip = ip
        self.path = path
        self.log_uid = log_uid
        self.line_no = line_no
        self.raw = raw

    def as_dict(self):
        return {
            "content": self.content,
            "timestamp_ms": self.timestamp_ms,
            "host": self.host,
            "ip": self.ip,
            "path": self.path,
            "log_uid": self.log_uid,
            "line_no": self.line_no,
        }

    def __repr__(self):
        return "LogRecord(timestamp_ms=%s, content=%r)" % (self.timestamp_ms, (self.content or "")[:80])


class LtsEvent(object):
    """解包后的日志投递事件。"""

    def __init__(self, log_group_id=None, log_stream_id=None, records=None,
                 owner=None, skipped=0):
        self.log_group_id = log_group_id
        self.log_stream_id = log_stream_id
        self.records = records or []
        self.owner = owner
        self.skipped = skipped

    def __repr__(self):
        return "LtsEvent(log_group_id=%s, log_stream_id=%s, records=%d, skipped=%d)" % (
            self.log_group_id, self.log_stream_id, len(self.records), self.skipped)


def _load_json(value):
    """尽力把 value 变成 dict / list；已经是对象时原样返回。"""
    if isinstance(value, (bytes, bytearray)):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError:
            return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except ValueError:
            return None
    return value


def _decode_base64(value):
    """解 Base64 事件段；解不出 JSON 时返回 None。"""
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8", "replace")
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        decoded = base64.b64decode(text, validate=False).decode("utf-8", "replace")
    except (TypeError, ValueError):
        return None
    return _load_json(decoded)


def _normalize_timestamp(entry, fallback_ms):
    for key in TIME_KEYS:
        if key not in entry:
            continue
        raw = entry.get(key)
        try:
            number = float(raw)
        except (TypeError, ValueError):
            continue
        if number <= 0:
            continue
        # 秒级时间戳补齐为毫秒，保持状态表主键口径一致
        return int(number * 1000) if number < EPOCH_SECONDS_THRESHOLD else int(number)
    return int(fallback_ms)


def _build_record(entry, fallback_ms):
    if not isinstance(entry, dict):
        return None
    content = None
    for key in CONTENT_KEYS:
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            content = value
            break
    if content is None:
        return None
    return LogRecord(
        content=content,
        timestamp_ms=_normalize_timestamp(entry, fallback_ms),
        host=_first_string(entry, HOST_KEYS),
        ip=_first_string(entry, ("ip", "ip_address")),
        path=_first_string(entry, ("path", "file_path")),
        log_uid=_first_string(entry, ("log_uid", "log_uuid", "id")),
        line_no=entry.get("line_no"),
        raw=entry,
    )


def _first_string(entry, keys):
    for key in keys:
        value = entry.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _extract_payload(event):
    """从事件里取出内层投递对象，兼容若干种外层形态。"""
    from errors import EventFormatError

    parsed = _load_json(event)
    if parsed is None:
        raise EventFormatError("事件不是合法的 JSON 结构，无法解包", event_type=type(event).__name__)
    if not isinstance(parsed, dict):
        raise EventFormatError("事件顶层不是对象结构", value=str(parsed)[:120])

    # 形态一：{"lts": {"data": "<base64>"}}
    outer = parsed.get("lts")
    if isinstance(outer, dict) and "data" in outer:
        payload = _decode_base64(outer.get("data"))
        if payload is not None:
            return payload
    # 形态二：{"data": "<base64>"}
    if "data" in parsed:
        payload = _decode_base64(parsed.get("data"))
        if payload is not None:
            return payload
    # 形态三：投递对象直接作为事件体下发
    if "logs" in parsed:
        return parsed
    raise EventFormatError(
        "事件不含日志投递段，非日志服务触发事件",
        keys=sorted(str(key) for key in parsed)[:10],
    )


def unpack(event, now_ms=None):
    """把事件解包成 :class:`LtsEvent`。

    单条日志条目结构异常时跳过并计数，不影响同批其它条目。
    """
    import time as _time

    fallback_ms = int(now_ms if now_ms is not None else _time.time() * 1000)
    payload = _extract_payload(event)
    if not isinstance(payload, dict):
        from errors import EventFormatError

        raise EventFormatError("日志投递段不是对象结构", value=str(payload)[:120])

    logs = payload.get("logs")
    if isinstance(logs, str):
        logs = _load_json(logs)
    if logs is None:
        logs = []
    if not isinstance(logs, list):
        from errors import EventFormatError

        raise EventFormatError("日志条目段不是数组", value=str(logs)[:120])

    records = []
    skipped = 0
    for entry in logs:
        if isinstance(entry, str):
            parsed_entry = _load_json(entry)
            if isinstance(parsed_entry, dict):
                # 条目本身是被字符串化的 JSON 对象，按结构化条目处理
                entry = parsed_entry
            else:
                # 投递侧只给一行原文时，按纯文本条目处理
                entry = {"content": entry, "time": fallback_ms}
        record = _build_record(entry, fallback_ms)
        if record is None:
            skipped += 1
            continue
        records.append(record)

    return LtsEvent(
        log_group_id=payload.get("log_group_id") or payload.get("logGroupId"),
        log_stream_id=payload.get("log_topic_id") or payload.get("logTopicId"),
        records=records,
        owner=payload.get("owner"),
        skipped=skipped,
    )
