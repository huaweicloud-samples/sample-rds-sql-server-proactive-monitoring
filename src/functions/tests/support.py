"""单测支撑：状态存储协议假引擎、通知通道假调用、假运行上下文。

假引擎只实现本函数实际用到的接口子集（建表、描述、TTL 设置、写条目、
条件更新、按分区键查询、带过滤的扫描），并严格按协议的数据形态收发：
请求体里的属性值都是 ``{"N": "..."}`` / ``{"S": "..."}`` 这类标记形式，
返回体同样是标记形式，因此被测代码的编解码路径被完整覆盖。
"""

import json
import os
import re
import time

import net

ENV_BASE = {
    "NOTIFY_WEBHOOK_URL": "<WEBHOOK_URL>",
    "STATE_COLLECTION": "SqlServerErrorState",
    "GEMINIDB_ENDPOINT": "10.20.1.35",
    "GEMINIDB_PORT": "7001",
    "GEMINIDB_USER": "root",
    "GEMINIDB_PASSWORD": "unit-test-db-secret",
    "SMN_TOPIC_URN": "urn:smn:cn-north-4:00000000000000000000000000000000:sqlserver-error-notify",
    "REGION": "cn-north-4",
    "PROJECT_ID": "00000000000000000000000000000000",
    "NOTIFICATION_COOLDOWN_MINUTES": "15",
    "TTL_HOURS": "10",
}

CLAUSE_RE = re.compile(r"^([#\w]+)\s*(=|<>|<=|>=|<|>)\s*(:[A-Za-z0-9_]+)$")
SET_ASSIGN_RE = re.compile(r"^([#\w]+)\s*=\s*(:[A-Za-z0-9_]+)$")


def fail(status, code, message=""):
    """构造一次「协议侧异常」。"""
    body = json.dumps({"__type": "com.service.namespace#%s" % code, "message": message})
    raise net.TransportError(
        "假引擎返回错误", status=status, body=body, code=code,
        retryable=status in net.RETRYABLE_STATUS,
    )


def _values_equal(attribute, expected):
    """比较两个协议属性值是否相等（数字按数值比较）。"""
    if not isinstance(attribute, dict) or not isinstance(expected, dict):
        return False
    for key in ("S", "BOOL", "NULL"):
        if key in attribute or key in expected:
            return attribute.get(key) == expected.get(key)
    if "N" in attribute and "N" in expected:
        try:
            return float(attribute["N"]) == float(expected["N"])
        except (TypeError, ValueError):
            return False
    return False


def _resolve(name, names):
    return names.get(name, name) if name.startswith("#") else name


def match_expression(expression, names, values, item):
    """按 ``A = :v AND B = :w`` 形态过滤一条记录。"""
    if not expression:
        return True
    for clause in re.split(r"\s+AND\s+", expression.strip()):
        match = CLAUSE_RE.match(clause.strip())
        if not match:
            raise AssertionError("假引擎不支持的表达式：%r" % clause)
        attr = _resolve(match.group(1), names or {})
        operator = match.group(2)
        expected = (values or {}).get(match.group(3))
        actual = item.get(attr)
        if operator == "=":
            ok = _values_equal(actual, expected)
        elif operator == "<>":
            ok = not _values_equal(actual, expected)
        else:
            left = _number(actual)
            right = _number(expected)
            if left is None or right is None:
                ok = False
            elif operator == "<":
                ok = left < right
            elif operator == "<=":
                ok = left <= right
            elif operator == ">":
                ok = left > right
            else:
                ok = left >= right
        if not ok:
            return False
    return True


def _number(attribute):
    if isinstance(attribute, dict) and "N" in attribute:
        try:
            return float(attribute["N"])
        except (TypeError, ValueError):
            return None
    return None


class FakeStateEngine(object):
    """状态存储假引擎。

    :param support_ttl: False 时 TTL 设置接口返回不支持，用于验证过期过滤兜底
    :param page_size: 扫描分页大小，用于验证分页拉取
    :param unavailable: True 时所有调用返回 503，用于验证状态存储不可用分支
    """

    def __init__(self, support_ttl=True, page_size=None, unavailable=False, clock=None):
        self.tables = {}
        self.ttl_settings = {}
        self.calls = []
        self.support_ttl = support_ttl
        self.page_size = page_size
        self.unavailable = unavailable
        self.clock = clock or time.time

    # ---- 传输入口 ----
    def transport(self, url=None, headers=None, body=None, timeout=None, verify=None):
        payload = json.loads(body.decode("utf-8") if isinstance(body, (bytes, bytearray)) else body)
        target = _header(headers, signing_target_header()) or ""
        operation = target.split(".")[-1]
        self.calls.append({
            "operation": operation,
            "body": payload,
            "url": url,
            "authorization": _header(headers, "Authorization"),
            "date": _header(headers, "X-Amz-Date"),
            "timeout": timeout,
            "verify": verify,
        })
        if self.unavailable:
            fail(503, "ServiceUnavailableException", "状态存储暂不可用")
        handler = getattr(self, "_op_" + operation, None)
        if handler is None:
            fail(400, "UnknownOperationException", "假引擎未实现该接口：%s" % operation)
        return handler(payload)

    # ---- 表管理 ----
    def _op_CreateTable(self, payload):
        name = payload["TableName"]
        if name in self.tables:
            fail(400, "ResourceInUseException", "表已存在")
        self.tables[name] = {}
        return {"TableDescription": {"TableName": name, "TableStatus": "ACTIVE"}}

    def _op_DescribeTable(self, payload):
        name = payload["TableName"]
        if name not in self.tables:
            fail(400, "ResourceNotFoundException", "表不存在")
        return {"Table": {"TableName": name, "TableStatus": "ACTIVE"}}

    def _op_UpdateTimeToLive(self, payload):
        if not self.support_ttl:
            fail(400, "UnknownOperationException", "对端未提供 TTL 接口")
        self.ttl_settings[payload["TableName"]] = {
            "AttributeName": payload["AttributeName"],
            "Enabled": payload["EnableTimeToLive"],
        }
        return {"TimeToLiveSpecification": {
            "AttributeName": payload["AttributeName"],
            "Enabled": payload["EnableTimeToLive"],
        }}

    # ---- 数据面 ----
    def _op_PutItem(self, payload):
        table = self._require_table(payload["TableName"])
        key = _key_of(payload["Item"])
        table[key] = dict(payload["Item"])
        return {"Attributes": table[key]}

    def _op_GetItem(self, payload):
        table = self._require_table(payload["TableName"])
        item = table.get(_key_of(payload["Key"]))
        return {"Item": item} if item else {}

    def _op_UpdateItem(self, payload):
        table = self._require_table(payload["TableName"])
        key = _key_of(payload["Key"])
        item = table.get(key)
        if item is None:
            fail(400, "ResourceNotFoundException", "记录不存在")
        assignment = re.sub(r"^SET\s+", "", payload["UpdateExpression"].strip())
        for clause in assignment.split(","):
            match = SET_ASSIGN_RE.match(clause.strip())
            if not match:
                raise AssertionError("假引擎不支持的更新表达式：%r" % clause)
            attribute = _resolve(match.group(1), payload.get("ExpressionAttributeNames"))
            item[attribute] = payload["ExpressionAttributeValues"][match.group(2)]
        table[key] = item
        if payload.get("ReturnValues") == "ALL_NEW":
            return {"Attributes": dict(item)}
        return {}

    def _op_Query(self, payload):
        table = self._require_table(payload["TableName"])
        names = payload.get("ExpressionAttributeNames") or {}
        values = payload.get("ExpressionAttributeValues") or {}
        key_attr = _resolve(
            CLAUSE_RE.match(payload["KeyConditionExpression"].strip()).group(1), names)
        key_value = values[CLAUSE_RE.match(payload["KeyConditionExpression"].strip()).group(3)]

        items = [item for item in table.values() if _values_equal(item.get(key_attr), key_value)]
        items = [item for item in items
                 if match_expression(payload.get("FilterExpression"), names, values, item)]
        reverse = payload.get("ScanIndexForward", True) is False
        items.sort(key=lambda item: _number(item.get("timestamp")) or 0, reverse=reverse)
        response = {"Count": len(items), "Items": items, "ScannedCount": len(items)}
        if payload.get("Limit") and len(items) > payload["Limit"]:
            response["Items"] = items[: payload["Limit"]]
            response["Count"] = len(response["Items"])
            response["LastEvaluatedKey"] = _plain_key(response["Items"][-1])
        return response

    def _op_Scan(self, payload):
        table = self._require_table(payload["TableName"])
        names = payload.get("ExpressionAttributeNames") or {}
        values = payload.get("ExpressionAttributeValues") or {}
        items = [item for item in sorted(table.values(),
                                        key=lambda item: _number(item.get("timestamp")) or 0)
                 if match_expression(payload.get("FilterExpression"), names, values, item)]

        start = payload.get("ExclusiveStartKey")
        offset = 0
        if start:
            keys = [_plain_key(item) for item in items]
            offset = keys.index(start) + 1 if start in keys else 0
        page_size = self.page_size or payload.get("Limit") or len(items)
        page = items[offset: offset + page_size]
        response = {"Count": len(page), "Items": page, "ScannedCount": len(items)}
        if offset + page_size < len(items):
            response["LastEvaluatedKey"] = _plain_key(page[-1])
        return response

    def _require_table(self, name):
        if name not in self.tables:
            fail(400, "ResourceNotFoundException", "表不存在")
        return self.tables[name]

    def seed(self, table_name, items):
        """直接放入若干条记录（属性值已是协议形态）。"""
        self.tables.setdefault(table_name, {})
        for item in items:
            self.tables[table_name][_key_of(item)] = dict(item)


def signing_target_header():
    import signing

    return signing.PROTOCOL_HEADER_TARGET


def _header(headers, name):
    for key, value in (headers or {}).items():
        if str(key).lower() == name.lower():
            return value
    return None


def _key_of(item):
    return (json.dumps(item.get("error_number"), sort_keys=True),
            json.dumps(item.get("timestamp"), sort_keys=True))


def _plain_key(item):
    return {"error_number": item.get("error_number"), "timestamp": item.get("timestamp")}


class RecordingTransport(object):
    """通知通道假调用：记录每次 POST 的地址、头与请求体。"""

    def __init__(self, status=200, fail_with=None, fail_times=0):
        self.requests = []
        self.status = status
        self.fail_with = fail_with
        self.fail_times = fail_times

    def transport(self, url=None, headers=None, body=None, timeout=None, verify=None):
        record = {
            "url": url,
            "headers": dict(headers or {}),
            "body": json.loads((body.decode("utf-8") if isinstance(body, (bytes, bytearray))
                                else body) or "{}"),
            "authorization": _header(headers, "Authorization"),
            "timeout": timeout,
            "verify": verify,
        }
        self.requests.append(record)
        if self.fail_with is not None and len(self.requests) <= self.fail_times:
            fail(self.status, self.fail_with, "假通道返回失败")
        return {"MessageId": "00000000-0000-0000-0000-000000000001"}


class FakeContext(object):
    """函数运行上下文假实现。"""

    def __init__(self, remaining_millis=60000, credentials=True):
        self._remaining = remaining_millis
        self._credentials = credentials

    def getRequestID(self):
        return "req-fake-0001"

    def getFunctionName(self):
        return "rds-sqlserver-monitor-error-notifier"

    def getRemainingTimeInMilliSeconds(self):
        return self._remaining

    def getSecurityAccessKey(self):
        return "FAKEAK" if self._credentials else None

    def getSecuritySecretKey(self):
        return "FAKESK" if self._credentials else None

    def getSecurityToken(self):
        return "FAKETOKEN" if self._credentials else None

    def getProjectId(self):
        return None


def build_event(records, log_group_id="lg-fake", log_stream_id="ls-fake", now_ms=None):
    """按日志投递事件的真实结构（外层 Base64 + 内层 logs 字符串）构造事件。"""
    import base64

    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    logs = []
    for record in records:
        entry = dict(record)
        entry.setdefault("time", now_ms)
        entry.setdefault("host_name", "sqlserver-0")
        entry.setdefault("log_uid", "uid-%d" % len(logs))
        logs.append(entry)
    inner = json.dumps({
        "logs": json.dumps(logs, ensure_ascii=False),
        "owner": "owner-fake",
        "log_group_id": log_group_id,
        "log_topic_id": log_stream_id,
    }, ensure_ascii=False)
    return {"lts": {"data": base64.b64encode(inner.encode("utf-8")).decode("ascii")}}


def lts_env(**overrides):
    env = dict(ENV_BASE)
    env.update(overrides)
    return env


def use_placeholder_webhook(env):
    env["NOTIFY_WEBHOOK_URL"] = "<WEBHOOK_URL>"
    return env
