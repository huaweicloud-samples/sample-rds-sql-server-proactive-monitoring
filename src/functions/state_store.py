"""错误去重状态表：表格服务兼容协议的接口调用。

表结构（函数首启自动创建，幂等）：

* 主键：``error_number``（分区键，数字）+ ``timestamp``（排序键，数字，毫秒）
* TTL 属性：``expiry_time``（数字，Unix 秒），存活时长由运行配置 ``TTL_HOURS`` 决定
* 冷却属性：``last_notification``（数字，Unix 秒，0 表示从未告警）

去重与冷却语义：

* 同一个错误号的每次出现都会写成一行（主键含出现时间），
  告警判定按 ``error_number`` 查这批行，再按 ``level`` + ``state`` 过滤；
* 查不到历史记录 ⇒ 首次出现，放行告警；
* 查到记录 ⇒ 取最近一次告警时间，超过冷却窗口才放行；
* 状态存储读取失败时**放行告警**（宁可多报也不漏报），失败原因如实上报。

表达式里所有属性名都用 ``#`` 占位符传递，避免属性名与协议保留字冲突。
"""

import json
import time
from datetime import datetime

import net
import signing
from credentials import TempCredentials
from errors import StateStoreError

SERVICE_NAME = "dynamodb"
TABLE_WAIT_ATTEMPTS = 10
TABLE_WAIT_INTERVAL_SECONDS = 2
SCAN_PAGE_SIZE = 100

RES_NOT_FOUND = "ResourceNotFoundException"
RES_IN_USE = "ResourceInUseException"
VALIDATION_EXCEPTION = "ValidationException"
UNSUPPORTED_OPERATIONS = (
    "UnknownOperationException",
    "InvalidEndpointException",
    "SerializationException",
)


class _DbCredentials(TempCredentials):
    """状态存储鉴权用的凭据视图：账号即访问密钥、口令即签名密钥。"""


def marshal(value):
    """Python 值 -> 协议属性值。"""
    if value is None:
        return {"NULL": True}
    if isinstance(value, bool):
        return {"BOOL": value}
    if isinstance(value, (int, float)):
        return {"N": str(value)}
    if isinstance(value, str):
        return {"S": value}
    if isinstance(value, dict):
        return {"M": {key: marshal(item) for key, item in value.items()}}
    if isinstance(value, (list, tuple)):
        return {"L": [marshal(item) for item in value]}
    return {"S": str(value)}


def unmarshal(attribute):
    """协议属性值 -> Python 值。"""
    if not isinstance(attribute, dict):
        return attribute
    if "S" in attribute:
        return attribute["S"]
    if "N" in attribute:
        raw = attribute["N"]
        try:
            number = float(raw)
        except (TypeError, ValueError):
            return raw
        return int(number) if number.is_integer() else number
    if "BOOL" in attribute:
        return bool(attribute["BOOL"])
    if "NULL" in attribute:
        return None
    if "M" in attribute:
        return {key: unmarshal(item) for key, item in attribute["M"].items()}
    if "L" in attribute:
        return [unmarshal(item) for item in attribute["L"]]
    return attribute


def marshal_item(mapping):
    """记录（属性名 -> Python 值）-> 协议记录（属性名 -> 属性值）。"""
    return {key: marshal(value) for key, value in dict(mapping).items()}


def unmarshal_item(mapping):
    """协议记录 -> 记录（属性名 -> Python 值）。"""
    if not isinstance(mapping, dict):
        return {}
    return {key: unmarshal(value) for key, value in mapping.items()}


class StateStore(object):
    """状态表的读写入口。

    :param transport: 底层 HTTP 调用，单测注入假实现；签名与重试仍走真实代码路径
    """

    def __init__(self, cfg, logger, credentials=None, transport=None, clock=None):
        self._cfg = cfg
        self._logger = logger
        self._transport = transport or _default_transport
        self._clock = clock or time.time
        self.table_name = cfg.state_collection
        self._credentials = _DbCredentials(
            access_key=cfg.db_user, secret_key=cfg.db_password, security_token=None
        )
        self._base_credentials = credentials
        self.enabled = True
        self._ttl_enabled = False
        self._ready = False

    @property
    def ttl_enabled(self):
        """TTL 是否由对端托管（False 时由本模块按 expiry_time 过滤过期记录）。"""
        return self._ttl_enabled

    # ------------------------------------------------------------------
    # 协议调用
    # ------------------------------------------------------------------
    def _signing_credentials(self):
        if self._credentials.available():
            return self._credentials
        if self._base_credentials is not None and self._base_credentials.available():
            # 未配置数据库账号时退回运行凭据
            self._logger.warn("状态存储未配置账号口令，改用运行临时凭据签名")
            return self._base_credentials
        raise StateStoreError("状态存储缺少可用凭据", code="MissingCredentials")

    def _call(self, operation, body):
        """执行一次协议接口调用，返回解析后的响应体。"""
        host = _host_of(self._cfg.state_endpoint())
        path = "/"
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        last_error = [None]

        def attempt(_attempt_no):
            headers, url = signing.sign_protocol_request(
                host=host,
                path=path,
                method="POST",
                headers={
                    "Content-Type": "application/json",
                    signing.PROTOCOL_HEADER_TARGET: "%s.%s" % (signing.PROTOCOL_API_VERSION, operation),
                },
                body=payload,
                credentials=self._signing_credentials(),
                region=self._cfg.region,
                service=SERVICE_NAME,
            )
            try:
                return self._transport(
                    url=url,
                    headers=headers,
                    body=payload,
                    timeout=self._cfg.http_timeout_seconds,
                    verify=False,
                )
            except net.TransportError as exc:
                code = exc.code or net.parse_error_code(exc.body)
                raise StateStoreError(
                    "状态存储调用失败",
                    code=code,
                    retryable=exc.retryable,
                    operation=operation,
                    status=exc.status,
                )

        return net.call_with_retry(
            attempt,
            logger=self._logger,
            activity="状态存储 %s" % operation,
            max_attempts=self._cfg.max_attempts,
            backoff_seconds=self._cfg.retry_backoff_seconds,
        )

    # ------------------------------------------------------------------
    # 建表与 TTL
    # ------------------------------------------------------------------
    def ensure_ready(self):
        """确保状态表存在且 TTL 生效；幂等，失败时禁用状态能力并返回 False。"""
        if self._ready:
            return True
        try:
            try:
                self._call("DescribeTable", {"TableName": self.table_name})
            except StateStoreError as exc:
                if exc.code != RES_NOT_FOUND:
                    raise
                self._create_table()
            self._enable_ttl()
            self._ready = True
            self.enabled = True
            self._logger.info("状态表就绪", table=self.table_name, ttl_enabled=self._ttl_enabled)
            return True
        except StateStoreError as exc:
            self.enabled = False
            self._logger.error(
                "状态表不可用，本次调用按放行告警处理",
                table=self.table_name,
                code=exc.code,
                detail=exc.details.get("status"),
            )
            return False

    def _create_table(self):
        body = {
            "TableName": self.table_name,
            "AttributeDefinitions": [
                {"AttributeName": "error_number", "AttributeType": "N"},
                {"AttributeName": "timestamp", "AttributeType": "N"},
            ],
            "KeySchema": [
                {"AttributeName": "error_number", "KeyType": "HASH"},
                {"AttributeName": "timestamp", "KeyType": "RANGE"},
            ],
        }
        try:
            self._call("CreateTable", body)
        except StateStoreError as exc:
            if exc.code != RES_IN_USE:
                raise
            self._logger.info("状态表已存在，跳过创建", table=self.table_name)
            return
        self._logger.info("状态表创建请求已提交", table=self.table_name)
        self._wait_active()

    def _wait_active(self):
        for _ in range(TABLE_WAIT_ATTEMPTS):
            try:
                describe = self._call("DescribeTable", {"TableName": self.table_name})
            except StateStoreError as exc:
                if exc.code == RES_NOT_FOUND:
                    time.sleep(TABLE_WAIT_INTERVAL_SECONDS)
                    continue
                raise
            status = None
            if isinstance(describe, dict):
                status = (describe.get("Table") or {}).get("TableStatus")
            if str(status).upper() == "ACTIVE":
                return
            time.sleep(TABLE_WAIT_INTERVAL_SECONDS)
        raise StateStoreError("等待状态表就绪超时", code="TableNotReady",
                              table=self.table_name)

    def _enable_ttl(self):
        body = {
            "TableName": self.table_name,
            "AttributeName": "expiry_time",
            "EnableTimeToLive": True,
        }
        try:
            self._call("UpdateTimeToLive", body)
            self._ttl_enabled = True
        except StateStoreError as exc:
            if exc.code in UNSUPPORTED_OPERATIONS or exc.status in (400, 404):
                # 对端未提供 TTL 接口时，过期判定改由本模块按 expiry_time 过滤
                self._ttl_enabled = False
                self._logger.warn(
                    "状态存储未接受 TTL 设置，改由按 expiry_time 过滤过期记录",
                    code=exc.code,
                )
                return
            raise

    # ------------------------------------------------------------------
    # 读写
    # ------------------------------------------------------------------
    def build_item(self, parsed, record, log_group_id, log_stream_id):
        """由解析结果 + 日志条目拼出状态表记录。"""
        now_seconds = int(self._clock())
        event_seconds = int(record.timestamp_ms // 1000) if record.timestamp_ms else now_seconds
        return {
            "error_number": parsed.error_number,
            "timestamp": record.timestamp_ms,
            "level": parsed.level,
            "state": parsed.state,
            "line": parsed.line,
            "message": parsed.message,
            "full_message": parsed.full_message,
            "log_timestamp": parsed.log_timestamp,
            "is_complete": parsed.is_complete,
            "rule": parsed.rule,
            "log_group_id": log_group_id,
            "log_stream_id": log_stream_id,
            "host": record.host,
            "ip": record.ip,
            "path": record.path,
            "occurred_at": event_seconds,
            "record_created_at": now_seconds,
            "utc_time": datetime.utcnow().isoformat(),
            "expiry_time": event_seconds + self._cfg.ttl_seconds,
            "last_notification": 0,
            "notification_sent": False,
        }

    def record_occurrence(self, item):
        """写入一次错误出现记录（主键相同则覆盖，但保留已发出的告警时间）。"""
        body = {
            "TableName": self.table_name,
            "Item": marshal_item(item),
        }
        self._call("PutItem", body)
        return item

    def query_occurrences(self, error_number, level, state):
        """查同一错误号的历史记录（按出现时间倒序），并过滤掉级别/状态不符与已过期的记录。"""
        body = {
            "TableName": self.table_name,
            "KeyConditionExpression": "#en = :en",
            "FilterExpression": "#lvl = :lvl AND #st = :st",
            "ExpressionAttributeNames": {"#en": "error_number", "#lvl": "level", "#st": "state"},
            "ExpressionAttributeValues": {
                ":en": marshal(error_number),
                ":lvl": marshal(level),
                ":st": marshal(state),
            },
            "ScanIndexForward": False,
        }
        try:
            response = self._call("Query", body)
        except StateStoreError as exc:
            exc.details["stage"] = "query_occurrences"
            raise
        items = _items_of(response)
        now_seconds = int(self._clock())
        return [item for item in items if not _expired(item, now_seconds)]

    def latest_incomplete(self):
        """取最近一条「未完成」记录，用于把续行描述配对到错误头行。"""
        items = []
        start_key = None
        for _ in range(20):  # 分页上限，防止扫描不收敛
            body = {
                "TableName": self.table_name,
                "FilterExpression": "#complete = :complete",
                "ExpressionAttributeNames": {"#complete": "is_complete"},
                "ExpressionAttributeValues": {":complete": {"BOOL": False}},
                "Limit": SCAN_PAGE_SIZE,
            }
            if start_key:
                body["ExclusiveStartKey"] = start_key
            response = self._call("Scan", body)
            items.extend(_items_of(response))
            start_key = (response or {}).get("LastEvaluatedKey") if isinstance(response, dict) else None
            if not start_key:
                break
        now_seconds = int(self._clock())
        candidates = [item for item in items if not _expired(item, now_seconds)]
        if not candidates:
            return None
        candidates.sort(key=lambda item: item.get("timestamp") or 0, reverse=True)
        return candidates[0]

    def attach_description(self, item, description):
        """补齐描述并标记完成，返回更新后的记录。"""
        body = {
            "TableName": self.table_name,
            "Key": {
                "error_number": marshal(item["error_number"]),
                "timestamp": marshal(item["timestamp"]),
            },
            "UpdateExpression": "SET #msg = :msg, #complete = :complete",
            "ExpressionAttributeNames": {"#msg": "message", "#complete": "is_complete"},
            "ExpressionAttributeValues": {":msg": marshal(description), ":complete": {"BOOL": True}},
            "ReturnValues": "ALL_NEW",
        }
        response = self._call("UpdateItem", body)
        attributes = {}
        if isinstance(response, dict) and isinstance(response.get("Attributes"), dict):
            attributes = unmarshal_item(response["Attributes"])
        merged = dict(item)
        merged.update(attributes)
        merged.setdefault("message", description)
        merged["is_complete"] = True
        return merged

    def mark_notified(self, error_number, timestamp, notified_at=None):
        """记录本次告警发出时间，作为后续冷却判定依据。"""
        notified_at = int(notified_at if notified_at is not None else self._clock())
        body = {
            "TableName": self.table_name,
            "Key": {
                "error_number": marshal(error_number),
                "timestamp": marshal(timestamp),
            },
            "UpdateExpression": "SET #last = :time, #sent = :sent, #expiry = :expiry",
            "ExpressionAttributeNames": {
                "#last": "last_notification",
                "#sent": "notification_sent",
                "#expiry": "expiry_time",
            },
            "ExpressionAttributeValues": {
                ":time": marshal(notified_at),
                ":sent": {"BOOL": True},
                ":expiry": marshal(notified_at + self._cfg.ttl_seconds),
            },
        }
        self._call("UpdateItem", body)
        return notified_at

    # ------------------------------------------------------------------
    # 告警判定
    # ------------------------------------------------------------------
    def decide(self, error_number, level, state):
        """返回 ``(是否告警, 原因, 最近一次告警时间)``。

        首次出现 / 冷却窗口已过 / 状态存储不可用 => 告警；
        冷却窗口内重复 => 抑制。
        """
        if not self.enabled:
            return True, "state_store_unavailable", 0
        try:
            items = self.query_occurrences(error_number, level, state)
        except StateStoreError as exc:
            self._logger.error(
                "查询错误历史失败，按放行告警处理",
                error_number=error_number,
                code=exc.code,
                stage=(exc.details or {}).get("stage"),
            )
            return True, "state_store_query_failed", 0

        latest = 0
        for item in items:
            marker = item.get("last_notification") or 0
            try:
                marker = int(marker)
            except (TypeError, ValueError):
                marker = 0
            if marker > latest:
                latest = marker

        if not items:
            return True, "first_occurrence", latest

        now_seconds = int(self._clock())
        if now_seconds - latest > self._cfg.cooldown_seconds:
            return True, "cooldown_expired", latest
        return False, "suppressed_within_cooldown", latest


def _default_transport(url, headers, body, timeout, verify):
    return net.post_json(url, body, headers=headers, timeout=timeout, verify=verify)


def _host_of(endpoint):
    """从接入地址里取 host:port 作为签名头 host 的取值。"""
    text = str(endpoint)
    if "://" in text:
        text = text.split("://", 1)[1]
    return text.split("/", 1)[0]


def _items_of(response):
    if not isinstance(response, dict):
        return []
    raw = response.get("Items")
    if not isinstance(raw, list):
        return []
    return [unmarshal_item(item) for item in raw]


def _expired(item, now_seconds):
    """记录是否已过存活期（TTL 未生效时靠该判定兜底）。"""
    expiry = item.get("expiry_time")
    try:
        expiry = int(expiry)
    except (TypeError, ValueError):
        return False
    return expiry <= now_seconds
