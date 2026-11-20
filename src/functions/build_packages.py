"""函数代码包构建脚本：把 ``src/functions`` 拍平成 FunctionGraph 可用的 zip。

产物路径与 ``infra/variables.tf`` 里 ``var.notifier_package`` 的默认值
（``../src/functions/build/sqlserver-error-notifier.zip``）完全一致，
``infra/functions.tf`` 的 ``huaweicloud_obs_bucket_object.notifier_code`` 就按这个
路径取包；改一处必须同步改另一处，否则 Terraform 会在读取包文件时报路径不存在。

打包规则：

* ``src/functions`` 目录下的 ``.py`` 文件全部放在 **zip 根目录**，
  源码里一律是 ``import config``、``import state_store`` 这类平铺导入，
  包内不存在目录层级，所以本脚本只收一层文件；
* ``index.py`` 必须落在根目录，``var.fgs_handler`` 的 ``index.handler`` 契约
  与之对应；``index.py`` 里必须存在名为 ``handler`` 的顶层函数；
* ``tests/`` 与 ``build/`` 不进包：测试只在本地执行，产物目录本身是输出；
* ``build_packages.py`` 自身不进包，构建工具不属于运行时代码；
* 运行期不引入任何第三方依赖：全部模块只用标准库，签名与传输都是
  ``hashlib`` / ``hmac`` / ``urllib`` 自实现，因此函数无需 ``requirements.txt``，
  运行时的模块搜索路径也不依赖 ``requests``；
* zip 内条目的时间戳固定，同一份源码重复构建得到字节完全一致的包，
  避免无谓的代码包变更触发函数替换。

依赖自检：脚本会用 ``ast`` 解析每个模块的顶层 ``import``，凡是既不属于
标准库、又不是同目录本地模块的包名，一律判为构建失败并列出来源文件，
防止把无法在函数运行时里解析的依赖带进包里。

用法（在仓库任意位置执行）::

    python src/functions/build_packages.py            # 构建代码包
    python src/functions/build_packages.py --list     # 只列条目，不写文件

脚本只用标准库，构建完成后打印包路径、字节数、SHA-256 与条目清单，
便于确认云端拿到的就是本地刚构建的那一份。
"""

import ast
import hashlib
import os
import sys
import zipfile

FUNCTIONS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(FUNCTIONS_DIR))
BUILD_DIR = os.path.join(FUNCTIONS_DIR, "build")

#: 函数名 -> 产物 zip 文件名；与 locals.tf 的 zip key 一一对应
PACKAGES = {
    "notifier": "sqlserver-error-notifier.zip",
}

#: 包根目录必须有入口模块，handler 契约是 ``index.handler``
ENTRY_MODULE = "index.py"
ENTRY_FUNCTION = "handler"

#: 构建脚本自身与本地专用目录不进包
_EXCLUDED_FILES = {"build_packages.py"}
_EXCLUDED_DIRS = {"build", "tests", "__pycache__", ".pytest_cache", ".mypy_cache"}
_EXCLUDED_SUFFIXES = (".pyc", ".pyo", ".tmp", ".zip")

#: 固定时间戳让同一份源码产出字节一致的包
_FIXED_ZIP_DATE = (1980, 1, 1, 0, 0, 0)


def collect_source_files():
    """按文件名字典序收集要放进 zip 根目录的 ``.py`` 源码文件。"""
    names = []
    for name in sorted(os.listdir(FUNCTIONS_DIR)):
        if name in _EXCLUDED_FILES or name.startswith("."):
            continue
        if name.endswith(_EXCLUDED_SUFFIXES):
            continue
        path = os.path.join(FUNCTIONS_DIR, name)
        if os.path.isfile(path) and name.endswith(".py"):
            names.append((name, path))
    return names


def local_module_names(source_files):
    """包内模块名集合，用于把 ``import state_store`` 判定为本地依赖。"""
    return {os.path.splitext(name)[0] for name, _path in source_files}


def external_imports(path, local_names):
    """解析单个模块的顶层导入，返回非标准库且非本地模块的包名。"""
    with open(path, "r", encoding="utf-8") as handle:
        tree = ast.parse(handle.read(), filename=path)

    stdlib = getattr(sys, "stdlib_module_names", None)
    found = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            roots = [alias.name.split(".")[0] for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            # 相对导入与 ``from . import x`` 都在包内解析，不参与判定
            if node.level or node.module is None:
                continue
            roots = [node.module.split(".")[0]]
        else:
            continue
        for root in roots:
            if root in local_names:
                continue
            if stdlib is not None and root in stdlib:
                continue
            found.append(root)
    return sorted(set(found))


def check_dependencies(source_files):
    """确认包内所有模块只依赖标准库与本地模块，返回问题清单。"""
    if getattr(sys, "stdlib_module_names", None) is None:
        # Python 3.9 及以下没有标准库模块名清单，跳过判定并说明原因
        return ["跳过依赖自检：当前解释器未提供 sys.stdlib_module_names（需 Python 3.10+）"]

    local_names = local_module_names(source_files)
    problems = []
    for name, path in source_files:
        for root in external_imports(path, local_names):
            problems.append("%s 依赖非标准库模块 %s，函数运行时无需安装的第三方包"
                            % (name, root))
    return problems


def check_entry(source_files):
    """校验入口模块存在且暴露 handler 函数。"""
    names = [name for name, _path in source_files]
    if ENTRY_MODULE not in names:
        return ["缺少入口模块 %s，handler 契约 index.%s 无法成立"
                % (ENTRY_MODULE, ENTRY_FUNCTION)]

    path = dict(source_files)[ENTRY_MODULE]
    with open(path, "r", encoding="utf-8") as handle:
        tree = ast.parse(handle.read(), filename=path)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name == ENTRY_FUNCTION:
            return []
    return ["%s 未定义顶层函数 %s()" % (ENTRY_MODULE, ENTRY_FUNCTION)]


def build(zip_name, source_files):
    """写出代码包，返回 zip 路径。"""
    if not os.path.isdir(BUILD_DIR):
        os.makedirs(BUILD_DIR)
    zip_path = os.path.join(BUILD_DIR, zip_name)

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for arcname, path in source_files:
            info = zipfile.ZipInfo(arcname, date_time=_FIXED_ZIP_DATE)
            info.compress_type = zipfile.ZIP_DEFLATED
            # 固定外部属性，避免不同平台的文件权限位进入包体
            info.external_attr = 0o644 << 16
            with open(path, "rb") as handle:
                archive.writestr(info, handle.read())

    return zip_path


def describe(zip_path):
    """返回包体字节数与 SHA-256，用于确认上传的就是刚构建的这一份。"""
    size = os.path.getsize(zip_path)
    digest = hashlib.sha256()
    with open(zip_path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return size, digest.hexdigest()


def main(argv):
    list_only = "--list" in argv[1:]
    targets = [item for item in argv[1:] if not item.startswith("-")]
    targets = targets or sorted(PACKAGES)

    for unknown in [item for item in targets if item not in PACKAGES]:
        print("未知函数名：%s（可选 %s）"
              % (unknown, "、".join(sorted(PACKAGES))), file=sys.stderr)
        return 2

    source_files = collect_source_files()
    if not source_files:
        print("%s 下没有找到任何 .py 源码" % FUNCTIONS_DIR, file=sys.stderr)
        return 1

    problems = check_entry(source_files) + check_dependencies(source_files)
    blocking = [item for item in problems if not item.startswith("跳过")]
    for item in problems:
        print("  ! %s" % item, file=sys.stderr)
    if blocking:
        print("代码包自检未通过，已停止构建", file=sys.stderr)
        return 1

    for function_name in targets:
        zip_name = PACKAGES[function_name]
        if list_only:
            zip_path = os.path.join(BUILD_DIR, zip_name)
            print("%s（不写文件）" % os.path.relpath(zip_path, REPO_ROOT).replace("\\", "/"))
        else:
            zip_path = build(zip_name, source_files)
            size, checksum = describe(zip_path)
            print("%s -> %s (%d bytes)"
                  % (function_name,
                     os.path.relpath(zip_path, REPO_ROOT).replace("\\", "/"), size))
            print("  sha256: %s" % checksum)
        for arcname, _path in source_files:
            print("  - %s" % arcname)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
