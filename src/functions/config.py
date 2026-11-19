"""运行配置读取。

配置项与函数环境变量一一对应（部署侧由基础设施统一下发），本模块只做解析与
校验，不新增未声明的键。

约定：形如 ``<WEBHOOK_URL>`` 的尖括号值是未替换的占位符，等同于「未配置」，
对应能力安全跳过而不是启动失败。
"""

import os

from credentials import read_context_attribute

# ---- 环境变量键名，与部署侧逐字一致 ----
KEY_WEBHOOK_URL = "NOTIFY_WEBHOOK_URL"
KEY_STATE_COLLECTION = "STATE_COLLECTION"
KEY_DB_ENDPOINT = "GEMINIDB_ENDPOINT"
KEY_DB_PORT = "GEMINIDB_PORT"
KEY_DB_USER = "GEMINIDB_USER"
KEY_DB_PASSWORD = "GEMINIDB_PASSWORD"
KEY_SMN_TOPIC_URN = "SMN_TOPIC_URN"
KEY_REGION = "REGION"
KEY_COOLDOWN_MINUTES = "NOTIFICATION_COOLDOWN_MINUTES"
KEY_TTL_HOURS = "TTL_HOURS"
KEY_PROJECT_ID = "PROJECT_ID"

DEFAULT_REGION = "cn-north-4"
DEFAULT_COOLDOWN_MINUTES = 15
DEFAULT_TTL_HOURS = 10
DEFAULT_DB_PORT = "7001"

# 单次外部调用的时间与重试上限，避免把函数超时预算耗在一条链路上
HTTP_TIMEOUT_SECONDS = 10
MAX_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 0.5
# 剩余时间低于该阈值时不再开始新的日志条目处理
MIN_REMAINING_SECONDS = 5
# 一次调用最多处理的日志条数，超出部分留待下一批日志到达时处理
MAX_RECORDS_PER_INVOKE = 200


def is_placeholder(value):
    """空值或尖括号占位符都视为「未配置」。"""
    if value is None:
        return True
    text = str(value).strip()
    if not text:
        return True
    return text.startswith("<") and text.endswith(">")


def configured(value):
    return not is_placeholder(value)


def _as_int(value, fallback, logger, name):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        if logger is not None:
            logger.warn("运行配置取值非法，回退默认值", key=name, value=value, fallback=fallback)
        return fallback


class RuntimeConfig(object):
    """一次调用使用的运行配置快照。"""

    def __init__(self, environ, context=None, logger=None):
        env = dict(environ or {})
        get = lambda key, default=None: env.get(key, default)

        self.raw = {key: get(key) for key in (
            KEY_WEBHOOK_URL, KEY_STATE_COLLECTION, KEY_DB_ENDPOINT, KEY_DB_PORT,
            KEY_DB_USER, KEY_DB_PASSWORD, KEY_SMN_TOPIC_URN, KEY_REGION,
            KEY_COOLDOWN_MINUTES, KEY_TTL_HOURS, KEY_PROJECT_ID,
        )}

        self.state_collection = get(KEY_STATE_COLLECTION)
        self.db_endpoint = get(KEY_DB_ENDPOINT)
        self.db_port = get(KEY_DB_PORT) or DEFAULT_DB_PORT
        self.db_user = get(KEY_DB_USER)
        self.db_password = get(KEY_DB_PASSWORD)

        self.webhook_url = get(KEY_WEBHOOK_URL)
        self.smn_topic_urn = get(KEY_SMN_TOPIC_URN)

        self.region = get(KEY_REGION) or DEFAULT_REGION
        self.project_id = read_context_attribute(context, "getProjectId") or get(KEY_PROJECT_ID)

        self.cooldown_seconds = _as_int(
            get(KEY_COOLDOWN_MINUTES), DEFAULT_COOLDOWN_MINUTES, logger, KEY_COOLDOWN_MINUTES
        ) * 60
        self.ttl_seconds = _as_int(get(KEY_TTL_HOURS), DEFAULT_TTL_HOURS, logger, KEY_TTL_HOURS) * 3600

        self.http_timeout_seconds = HTTP_TIMEOUT_SECONDS
        self.max_attempts = MAX_ATTEMPTS
        self.retry_backoff_seconds = RETRY_BACKOFF_SECONDS
        self.min_remaining_seconds = MIN_REMAINING_SECONDS
        self.max_records_per_invoke = MAX_RECORDS_PER_INVOKE

    # ---- 必需项校验 ----
    def validate(self):
        missing = [
            name
            for name, value in (
                (KEY_STATE_COLLECTION, self.state_collection),
                (KEY_DB_ENDPOINT, self.db_endpoint),
                (KEY_DB_USER, self.db_user),
                (KEY_DB_PASSWORD, self.db_password),
            )
            if not configured(value)
        ]
        return missing

    def webhook_enabled(self):
        return configured(self.webhook_url)

    def smn_enabled(self):
        return configured(self.smn_topic_urn) and bool(self.project_id)

    def smn_endpoint(self):
        """消息通知服务的区域接入地址。"""
        return "smn.%s.myhuaweicloud.com" % self.region

    def state_endpoint(self):
        """状态存储的兼容协议接入地址（VPC 内私有地址）。"""
        host = str(self.db_endpoint).strip()
        port = str(self.db_port).strip()
        if host.startswith("http://") or host.startswith("https://"):
            return host if ":" in host.split("//", 1)[1] else "%s:%s" % (host, port)
        return "https://%s:%s" % (host, port)

    def summary(self):
        """可直接落日志的配置视图，不含任何密钥。"""
        return {
            "region": self.region,
            "project_id": self.project_id,
            "state_collection": self.state_collection,
            "state_endpoint": self.state_endpoint() if self.db_endpoint else None,
            "db_user": self.db_user,
            "db_password": self.db_password,
            "webhook_configured": self.webhook_enabled(),
            "smn_configured": self.smn_enabled(),
            "smn_topic_urn": self.smn_topic_urn,
            "cooldown_seconds": self.cooldown_seconds,
            "ttl_seconds": self.ttl_seconds,
        }

    def __repr__(self):
        return "RuntimeConfig(%s)" % self.summary()


def load(context=None, environ=None, logger=None):
    """读取并校验运行配置；必需项缺失时抛 ConfigurationError。"""
    from errors import ConfigurationError

    cfg = RuntimeConfig(os.environ if environ is None else environ, context, logger)
    missing = cfg.validate()
    if missing:
        raise ConfigurationError("运行配置缺少必需项，无法处理日志", keys=missing)
    return cfg
