"""错误处理函数使用到的异常类型。

异常分层的目的：调用方（函数入口）能区分「事件结构不可用」与「单次外部调用失败」，
前者必须让整次调用失败（平台会记录失败并可按投递目标告警），
后者按重试与容错策略处理并在返回结果里如实上报。
"""


class MonitoringError(Exception):
    """本函数的所有自定义异常的基类。"""

    def __init__(self, message, **details):
        super(MonitoringError, self).__init__(message)
        self.message = message
        self.details = details

    def as_dict(self):
        payload = {"error": type(self).__name__, "message": self.message}
        payload.update(self.details)
        return payload


class ConfigurationError(MonitoringError):
    """运行配置缺失或取值非法，整次调用无法开始。"""


class EventFormatError(MonitoringError):
    """事件不是可识别的日志投递结构，无法解包。"""


class StateStoreError(MonitoringError):
    """状态存储调用失败。

    code 为对端返回的异常类名（如 ResourceNotFoundException），用于判定
    「表不存在」这类可恢复分支；status 为对端 HTTP 状态码；
    retryable 决定重试循环是否再次尝试。
    """

    def __init__(self, message, code=None, status=None, retryable=False, **details):
        super(StateStoreError, self).__init__(
            message, code=code, status=status, retryable=retryable, **details
        )
        self.code = code
        self.status = status
        self.retryable = retryable


class NotificationError(MonitoringError):
    """通知通道调用失败。

    retryable 由抛出方按对端状态码判定，重试循环只重试可恢复失败。
    """

    def __init__(self, message, code=None, status=None, retryable=False, **details):
        super(NotificationError, self).__init__(
            message, code=code, status=status, retryable=retryable, **details
        )
        self.code = code
        self.status = status
        self.retryable = retryable


class DeadlineExceeded(MonitoringError):
    """函数剩余执行时间不足，提前结束剩余处理。"""
