"""SQL Server 错误日志的解析与噪音过滤规则。

规则口径（与错误日志实际行格式一一对应，改动任何一条都会改变告警范围）：

* 需告警的错误行有两种书写形态：

  - 形态 A（完整行，同行带错误描述）：
    ``Msg <错误号>, Level <级别>, State <状态>, Server <实例>, Line <行号> <描述>``
  - 形态 B（头行 + 续行）：
    ``Error: <错误号>, Severity: <级别>, State: <状态>``
    描述在下一条日志行里，因此头行先落状态表并标记为未完成，等续行补齐后
    再一起判定是否告警。

* 可忽略的噪音：日志滚动、DBCC 常规输出、纯提示类信息等固定话术，
  命中即整条跳过，不写状态表也不告警。

* 判定只看「能否解析出错误号 + 级别 + 状态」，不看级别阈值：
  凡能解析且不属于噪音的错误都进入告警判定，重复抑制由状态表的冷却窗口负责。
"""

import re

# 日志行前缀里的时间戳，形如 2026-10-15 10:00:00.123
TIMESTAMP_PATTERN = r"(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\.\d+)"

# 形态 A：Msg / Level / State / Server / Line 同行给出描述
PATTERN_MSG_LINE = (
    r"Msg\s+(\d+),\s+Level\s+(\d+),\s+State\s+(\d+),\s+Server\s+([^,]+),\s+Line\s+(\d+)\s+(.+)"
)
# 形态 B：Error / Severity / State 头行，描述在续行
PATTERN_ERROR_HEADER = r"Error:\s+(\d+),\s+Severity:\s+(\d+),\s+State:\s+(\d+)"

# 续行开头的时间戳与 spid 前缀，补齐描述时去掉
DESCRIPTION_PREFIX_PATTERN = r"^\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\.\d+\s+spid\d+\s+"

# 可忽略的噪音话术（整行匹配，忽略大小写）
NOISE_PATTERNS = (
    r"Attempting to cycle error log",
    r"Logging SQL Server messages in file",
    r"DBCC CHECKDB .* found 0 errors and repaired 0 errors",
    r"This is an informational message only",
    r"The error log has been reinitialized",
    r"The last error 0 was within the time threshold for the duplicate count",
    r"DBCC execution completed\. If DBCC printed error messages, contact your system administrator\.",
    r"DBCC STDOUT:",
    r"DBCC STDERR:",
    r"DBCC has extra input",
    r"DBCC is not currently processing any command",
)

_COMPILED_NOISE = tuple(re.compile(pattern, re.IGNORECASE) for pattern in NOISE_PATTERNS)
_COMPILED_MSG_LINE = re.compile(PATTERN_MSG_LINE)
_COMPILED_ERROR_HEADER = re.compile(PATTERN_ERROR_HEADER)
_COMPILED_TIMESTAMP = re.compile(TIMESTAMP_PATTERN)
_COMPILED_DESCRIPTION_PREFIX = re.compile(DESCRIPTION_PREFIX_PATTERN)

# 解析规则名，用于日志与返回结果里说明命中了哪条规则
RULE_MSG_LINE = "msg_level_state_server_line"
RULE_ERROR_HEADER = "error_severity_state"


class ParsedError(object):
    """一条日志行的解析结果。"""

    __slots__ = ("error_number", "level", "state", "line", "message",
                 "full_message", "log_timestamp", "is_complete", "rule")

    def __init__(self, error_number, level, state, line, message, full_message,
                 log_timestamp, is_complete, rule):
        self.error_number = error_number
        self.level = level
        self.state = state
        self.line = line
        self.message = message
        self.full_message = full_message
        self.log_timestamp = log_timestamp
        self.is_complete = is_complete
        self.rule = rule

    def as_dict(self):
        return {
            "error_number": self.error_number,
            "level": self.level,
            "state": self.state,
            "line": self.line,
            "message": self.message,
            "full_message": self.full_message,
            "log_timestamp": self.log_timestamp,
            "is_complete": self.is_complete,
            "rule": self.rule,
        }

    def __repr__(self):
        return "ParsedError(%s)" % self.as_dict()


def extract_timestamp(text):
    """取出日志行内的时间戳原文，取不到返回空串。"""
    match = _COMPILED_TIMESTAMP.search(text or "")
    return match.group(1) if match else ""


def is_noise(text):
    """命中噪音话术返回 True（整条跳过）。"""
    if not text:
        return False
    for pattern in _COMPILED_NOISE:
        if pattern.search(text):
            return True
    return False


def noise_rule(text):
    """返回命中的噪音话术原文，未命中返回 None（便于日志说明跳过原因）。"""
    if not text:
        return None
    for raw_pattern, pattern in zip(NOISE_PATTERNS, _COMPILED_NOISE):
        if pattern.search(text):
            return raw_pattern
    return None


def parse_error(text):
    """按规则解析日志行，解析不出错误结构返回 None。"""
    message_text = (text or "").strip()
    if not message_text:
        return None

    log_timestamp = extract_timestamp(message_text)

    match = _COMPILED_MSG_LINE.search(message_text)
    if match:
        return ParsedError(
            error_number=int(match.group(1)),
            level=int(match.group(2)),
            state=int(match.group(3)),
            line=int(match.group(5)),
            message=match.group(6).strip(),
            full_message=text,
            log_timestamp=log_timestamp,
            is_complete=True,  # 形态 A 同行带描述，无需续行配对
            rule=RULE_MSG_LINE,
        )

    match = _COMPILED_ERROR_HEADER.search(message_text)
    if match:
        return ParsedError(
            error_number=int(match.group(1)),
            level=int(match.group(2)),  # 该形态里 Severity 即级别
            state=int(match.group(3)),
            line=0,                # 该形态不含行号
            message="",            # 描述由后续日志行补齐
            full_message=text,
            log_timestamp=log_timestamp,
            is_complete=False,     # 形态 B 永远先标为未完成
            rule=RULE_ERROR_HEADER,
        )

    return None


def clean_description(text):
    """补齐描述时去掉行首的时间戳与 spid 前缀。"""
    return _COMPILED_DESCRIPTION_PREFIX.sub("", text or "").strip()
