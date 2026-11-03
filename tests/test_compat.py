# -*- coding: utf-8 -*-
"""兼容红线：源码必须能被 Python 3.8 解析，且只依赖 pygame/pystray/Pillow 三个第三方包。"""
import sys
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

ALLOWED_THIRD_PARTY = {"pygame", "pystray", "PIL"}

# 显式列出本项目用到的标准库（Python 3.8 没有 sys.stdlib_module_names，
# 靠它判断会在 3.8 上把所有标准库都判成"第三方"）
STDLIB_ALLOWED = {
    "os", "sys", "re", "io", "json", "time", "csv", "hmac", "hashlib", "base64",
    "queue", "socket", "sqlite3", "ctypes", "threading", "datetime", "subprocess",
    "logging", "pathlib", "typing", "tempfile", "shutil", "traceback", "importlib",
    "ast", "http", "urllib", "tkinter", "winreg", "webbrowser", "string", "math",
    "collections", "functools", "itertools", "random", "glob", "contextlib",
}
FORBIDDEN_SYNTAX = ("match ", "| None", "removeprefix", "removesuffix",
                    "zoneinfo", "graphlib", "str | int")


def _py_files():
    return sorted(p for p in ROOT.rglob("*.py")
                  if ".git" not in str(p) and "__pycache__" not in str(p))


def test_every_module_parses():
    for p in _py_files():
        src = p.read_text(encoding="utf-8")
        ast.parse(src)                      # 语法层
        compile(src, str(p), "exec")


def test_no_python39_plus_syntax():
    """用 AST 判定，避免字符串误伤（检查器自身也含这些关键字）"""
    for p in _py_files():
        tree = ast.parse(p.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            # 3.10: match 语句
            assert not isinstance(node, getattr(ast, "Match", ())), "%s 用了 match 语句" % p.name
            # 3.9: str.removeprefix / removesuffix
            if isinstance(node, ast.Attribute):
                assert node.attr not in ("removeprefix", "removesuffix"), \
                    "%s 用了 3.9+ 方法 %s" % (p.name, node.attr)
            # 3.9: zoneinfo / graphlib
            if isinstance(node, ast.Import):
                for a in node.names:
                    assert a.name.split(".")[0] not in ("zoneinfo", "graphlib"), p.name
            if isinstance(node, ast.ImportFrom) and not node.level:
                assert (node.module or "").split(".")[0] not in ("zoneinfo", "graphlib"), p.name
            # 3.10: PEP 604 联合类型（X | None 出现在注解里）
            if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
                operands = (node.left, node.right)
                assert not any(isinstance(o, ast.Constant) and o.value is None
                               for o in operands), \
                    "%s 用了 3.10+ 的 X | None 注解" % p.name


def test_no_unexpected_third_party_imports():
    stdlib_ok = set(STDLIB_ALLOWED)
    if hasattr(sys, "stdlib_module_names"):
        stdlib_ok |= set(sys.stdlib_module_names)
    for p in _py_files():
        tree = ast.parse(p.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            mods = []
            if isinstance(node, ast.Import):
                mods = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:                      # 相对导入
                    continue
                if node.module:
                    mods = [node.module.split(".")[0]]
            for m in mods:
                if m in ("bellapp", "bell_system", "web_server"):
                    continue
                if stdlib_ok and m in stdlib_ok:
                    continue
                assert m in ALLOWED_THIRD_PARTY, "%s 引入了非白名单依赖: %s" % (p.name, m)
