"""结构化日志。

函数运行日志由平台采集到日志流，因此统一输出「一行一条 JSON」，字段可直接被
检索与统计；同时保证任何凭据、通知地址的查询参数都不落日志。
"""

import json
import sys
import time

_REDADED = "<masked>"

# 字段名命中这些片段即视为敏感信息，落日志前脱敏
_SENSITIVE_HINTS = (
    "password",
    "secret",
    "access_key",
    "accesskey",
    "security_token",
    "token",
    "webhook",
    "credential",
    "authorization",
)

MAX_TEXT_CHARS = 800


def truncate(text, limit=MAX_TEXT_CHARS):
    """日志里的原文字段统一截断，避免超长错误堆栈把日志打满。"""
    if text is None:
        return None
    text = text if isinstance(text, str) else str(text)
    if len(text) <= limit:
        return text
    return text[:limit] + "...<truncated>"


def sanitize(value, _key=None):
    """递归脱敏：命中敏感字段名的值一律替换为占位符。"""
    if _key and any(hint in _key.lower() for hint in _SENSITIVE_HINTS):
        return _REDADED if value not in (None, "") else value
    if isinstance(value, dict):
        return {str(k): sanitize(v, k) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize(item) for item in value]
    if isinstance(value, str):
        return truncate(value)
    return value


class StructuredLogger(object):
    """一次函数调用一个实例，公共字段（请求编号、函数名）随每条日志输出。"""

    def __init__(self, request_id=None, function_name=None, stream=None):
        self.request_id = request_id
        self.function_name = function_name
        self._stream = stream or sys.stdout
        self.records = []  # 单测与返回结果用，保留本次调用产生的日志条目

    def _emit(self, level, message, fields):
        entry = {
            "level": level,
            "time": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z",
            "request_id": self.request_id,
            "function_name": self.function_name,
            "message": message,
        }
        entry.update(sanitize(fields) or {})
        self.records.append(entry)
        try:
            self._stream.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        except Exception:  # 日志输出本身不允许影响业务
            pass
        return entry

    def debug(self, message, **fields):
        return self._emit("DEBUG", message, fields)

    def info(self, message, **fields):
        return self._emit("INFO", message, fields)

    def warn(self, message, **fields):
        return self._emit("WARN", message, fields)

    def error(self, message, **fields):
        return self._emit("ERROR", message, fields)


def build(context, stream=None):
    """优先用函数上下文里的请求编号与函数名，取不到时留空。"""
    from credentials import read_context_attribute

    return StructuredLogger(
        request_id=read_context_attribute(context, "getRequestID"),
        function_name=read_context_attribute(context, "getFunctionName"),
        stream=stream,
    )
