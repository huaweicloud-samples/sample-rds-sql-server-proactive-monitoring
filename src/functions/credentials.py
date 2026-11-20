"""运行期访问凭据读取。

函数绑定委托后，运行平台的临时凭据只来自调用上下文（一次性签发），
代码里不出现任何长期密钥，也不从文件读取凭据。

不同运行时版本暴露的取值方法名存在差异，这里按「先专后泛」的顺序逐个尝试，
全部取不到时返回 None，由调用方决定跳过还是报错。
"""

_ACCESS_KEY_GETTERS = ("getSecurityAccessKey", "getAccessKey")
_SECRET_KEY_GETTERS = ("getSecuritySecretKey", "getSecretKey")
_TOKEN_GETTERS = ("getSecurityToken", "getToken")


class TempCredentials(object):
    """临时访问凭据。

    属性只在内存里存在，日志输出必须走 :func:`redacted`。
    """

    def __init__(self, access_key=None, secret_key=None, security_token=None):
        self.access_key = access_key or None
        self.secret_key = secret_key or None
        self.security_token = security_token or None

    def available(self):
        return bool(self.access_key and self.secret_key)

    def redacted(self):
        return {
            "access_key": _mask(self.access_key),
            "secret_key": _REDADED if self.secret_key else None,
            "security_token": _REDADED if self.security_token else None,
        }

    def __repr__(self):
        return "TempCredentials(%s)" % self.redacted()


_REDADED = "<masked>"


def read_context_attribute(obj, name, default=None):
    """按方法名（或属性名）从上下文取值，取不到返回 default。"""
    if obj is None:
        return default
    getter = getattr(obj, name, None)
    if callable(getter):
        try:
            value = getter()
        except Exception:
            return default
        return default if value in (None, "") else value
    if getter not in (None, ""):
        return getter
    return default


def _first(context, names):
    for name in names:
        value = read_context_attribute(context, name)
        if value:
            return value
    return None


def from_context(context):
    """从函数上下文取临时凭据；未绑定委托或运行时不支持时返回 None。"""
    access_key = _first(context, _ACCESS_KEY_GETTERS)
    secret_key = _first(context, _SECRET_KEY_GETTERS)
    token = _first(context, _TOKEN_GETTERS)
    if not (access_key and secret_key):
        return None
    return TempCredentials(access_key, secret_key, token)


def _mask(value):
    if not value:
        return None
    text = str(value)
    if len(text) <= 4:
        return "****"
    return text[:2] + "****" + text[-2:]
