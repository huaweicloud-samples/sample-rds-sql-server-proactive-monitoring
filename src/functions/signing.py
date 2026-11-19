"""请求签名。

函数只用标准库完成两类签名，不引入任何签名 SDK：

1. ``sign_protocol_request``：状态存储（表格服务兼容协议）要求的 v4 逐段派生签名。
   规范要点：
   - 算法标识与凭证作用域后缀是协议规定的固定字面量，取值不可改；
   - 派生密钥链路为「密钥前缀 + 日期 + 区域 + 服务名 + 作用域后缀」逐段 HMAC；
   - 规范 URI 用实际请求路径（本服务恒为 ``/``），末尾**不**补斜杠；
   - 参与签名的请求头转小写、按键名字典序排列、值两侧空白去除，每行以换行结束。

2. ``sign_platform_request``：平台 API 网关 v3 签名（算法名 ``SDK-HMAC-SHA256``），
   用于消息通知服务的发布接口。规范要点：
   - 签名密钥就是密钥原文直接做一次 HMAC，没有逐级派生；
   - 待签名串为「算法名 + 时间戳 + 规范请求摘要」三段；
   - **规范 URI 以 ``/`` 结尾，而实际发出的请求 URL 不带这个尾斜杠**，写错会 401；
   - 键名含下划线的请求头不参与签名。
"""

import hashlib
import hmac
from datetime import datetime
from urllib.parse import quote, unquote

# ---- 兼容协议 v4 签名的协议固定字面量 ----
PROTOCOL_ALGORITHM = "AWS4-HMAC-SHA256"
PROTOCOL_SECRET_PREFIX = "AWS4"
PROTOCOL_SCOPE_SUFFIX = "aws4_request"
PROTOCOL_DATE_FORMAT = "%Y%m%dT%H%M%SZ"
PROTOCOL_HEADER_DATE = "X-Amz-Date"
PROTOCOL_HEADER_TARGET = "X-Amz-Target"
PROTOCOL_API_VERSION = "DynamoDB_20120801"

# ---- 平台 v3 签名 ----
PLATFORM_ALGORITHM = "SDK-HMAC-SHA256"
PLATFORM_DATE_FORMAT = "%Y%m%dT%H%M%SZ"
PLATFORM_HEADER_DATE = "X-Sdk-Date"
PLATFORM_HEADER_SECURITY_TOKEN = "X-Security-Token"

EMPTY_PAYLOAD_HASH = hashlib.sha256(b"").hexdigest()


def _bytes(value):
    if value is None:
        return b""
    if isinstance(value, bytes):
        return value
    return str(value).encode("utf-8")


def _hmac(key, message):
    return hmac.new(_bytes(key), _bytes(message), hashlib.sha256).digest()


def _url_encode(value):
    return quote(str(value), safe="~")


def canonical_uri(path, trailing_slash=False):
    """规范化请求路径：整体解码后按段重新编码，按需补尾斜杠。"""
    segments = unquote(path or "/").split("/")
    encoded = "/".join(_url_encode(segment) for segment in segments)
    if not encoded.startswith("/"):
        encoded = "/" + encoded
    if trailing_slash and not encoded.endswith("/"):
        encoded += "/"
    return encoded


def canonical_query_string(query_params):
    """查询串按键名排序后逐条编码拼接。"""
    pairs = []
    for key, value in sorted(list(query_params or [])):
        if isinstance(value, (list, tuple)):
            for item in sorted(str(v) for v in value):
                pairs.append((key, item))
        else:
            pairs.append((key, value))
    return "&".join("%s=%s" % (_url_encode(k), _url_encode(v)) for k, v in pairs)


def _canonical_headers(lowered, names):
    return "".join("%s:%s\n" % (name, lowered[name]) for name in names)


def _join_url(host, path, query):
    url = "https://%s%s" % (host, path)
    if query:
        url = "%s?%s" % (url, query)
    return url


# ----------------------------------------------------------------------------
# 1. 状态存储：兼容协议 v4 逐段派生签名
# ----------------------------------------------------------------------------
def sign_protocol_request(host, path, method, headers, body, credentials,
                          region, service, when=None):
    """返回 ``(补全后的请求头, 请求 URL)``。

    :param credentials: 具备 ``access_key`` / ``secret_key`` / ``security_token`` 的对象
    """
    lowered = {}
    for key, value in (headers or {}).items():
        if value is None:
            continue
        lowered[key.lower()] = _bytes(value).decode("utf-8", "replace").strip()

    lowered["host"] = host
    stamp = when or datetime.utcnow()
    amz_date = stamp.strftime(PROTOCOL_DATE_FORMAT)
    lowered[PROTOCOL_HEADER_DATE.lower()] = amz_date
    if getattr(credentials, "security_token", None):
        lowered["x-amz-security-token"] = str(credentials.security_token).strip()

    signed_names = sorted(lowered)
    payload_hash = hashlib.sha256(_bytes(body)).hexdigest()
    canonical_request = "\n".join([
        method.upper(),
        canonical_uri(path),
        "",  # 本协议不使用查询串
        _canonical_headers(lowered, signed_names),
        ";".join(signed_names),
        payload_hash,
    ])

    date_stamp = stamp.strftime("%Y%m%d")
    scope = "/".join([date_stamp, region, service, PROTOCOL_SCOPE_SUFFIX])
    string_to_sign = "\n".join([
        PROTOCOL_ALGORITHM,
        amz_date,
        scope,
        hashlib.sha256(_bytes(canonical_request)).hexdigest(),
    ])

    signing_key = _hmac(PROTOCOL_SECRET_PREFIX + credentials.secret_key, date_stamp)
    signing_key = _hmac(signing_key, region)
    signing_key = _hmac(signing_key, service)
    signing_key = _hmac(signing_key, PROTOCOL_SCOPE_SUFFIX)
    signature = hmac.new(signing_key, _bytes(string_to_sign), hashlib.sha256).hexdigest()

    out_headers = {key: value for key, value in lowered.items()}
    # 头名按调用方给定的原始大小写回传，签名用小写形态，两者取值必须一致
    out_headers["X-Amz-Date"] = out_headers.pop(PROTOCOL_HEADER_DATE.lower())
    if getattr(credentials, "security_token", None):
        out_headers["X-Amz-Security-Token"] = out_headers.pop("x-amz-security-token")
    out_headers["Authorization"] = (
        "%s Credential=%s/%s, SignedHeaders=%s, Signature=%s"
        % (PROTOCOL_ALGORITHM, credentials.access_key, scope, ";".join(signed_names), signature)
    )
    return out_headers, _join_url(host, canonical_uri(path), "")


# ----------------------------------------------------------------------------
# 2. 平台 API 网关 v3 签名（消息通知服务）
# ----------------------------------------------------------------------------
def sign_platform_request(host, path, method, headers, body, credentials, query_params=None,
                          when=None):
    """返回 ``(补全后的请求头, 请求 URL)``。"""
    request_headers = {}
    for key, value in (headers or {}).items():
        if value is None:
            continue
        if isinstance(value, bytes):
            value = value.decode("utf-8", "replace")
        request_headers[key] = str(value).strip()

    request_headers["Host"] = host
    stamp = when or datetime.utcnow()
    request_headers[PLATFORM_HEADER_DATE] = stamp.strftime(PLATFORM_DATE_FORMAT)
    if getattr(credentials, "security_token", None):
        request_headers[PLATFORM_HEADER_SECURITY_TOKEN] = str(credentials.security_token).strip()

    lowered = {key.lower(): value for key, value in request_headers.items()}
    # 键名含下划线的请求头不参与签名
    signed_names = sorted(name for name in lowered if "_" not in name)
    canonical_request = "\n".join([
        method.upper(),
        canonical_uri(path, trailing_slash=True),
        canonical_query_string(query_params),
        _canonical_headers(lowered, signed_names),
        ";".join(signed_names),
        hashlib.sha256(_bytes(body)).hexdigest() if body else EMPTY_PAYLOAD_HASH,
    ])
    date_value = request_headers[PLATFORM_HEADER_DATE]
    string_to_sign = "\n".join([
        PLATFORM_ALGORITHM,
        date_value,
        hashlib.sha256(_bytes(canonical_request)).hexdigest(),
    ])
    signature = hmac.new(_bytes(credentials.secret_key), _bytes(string_to_sign),
                         hashlib.sha256).hexdigest()

    request_headers["Authorization"] = "%s Access=%s, SignedHeaders=%s, Signature=%s" % (
        PLATFORM_ALGORITHM,
        credentials.access_key,
        ";".join(signed_names),
        signature,
    )
    return request_headers, _join_url(host, path, canonical_query_string(query_params))
