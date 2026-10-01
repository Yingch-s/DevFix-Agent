"""Maven 编译错误解析（javac / maven-compiler-plugin 输出格式）。

典型输入：
    [ERROR] COMPILATION ERROR
    [ERROR] /D:/.../OrderController.java:[31,25] cannot find symbol
    [ERROR]   symbol:   method findActiveById(int)
    [ERROR]   location: variable userRepository of type com.example.UserRepository
"""

from __future__ import annotations

import re

from devfix.models import CompileError
from devfix.parsing.stack_trace import _strip_level_prefix

COMPILATION_MARKER = "COMPILATION ERROR"

_ERROR_LINE_RE = re.compile(
    r"^(?P<path>(?:[A-Za-z]:)?[/\\].+?):"  # 路径（含 Windows /D:/... 形式），到 :[行,列] 为止
    r"\[(?P<line>\d+),(?P<col>\d+)\]\s+"
    r"(?P<msg>.+)$"
)
_SYMBOL_RE = re.compile(r"^\s*symbol:\s+(?P<symbol>.+?)\s*$")
_LOCATION_RE = re.compile(r"^\s*location:\s+(?P<location>.+?)\s*$")


def _normalize_path(path: str) -> str:
    """Maven 在 Windows 上常打印 /D:/workspace/... 形式，去掉开头多余的 /。"""
    return re.sub(r"^/([A-Za-z]:/)", r"\1", path)


def parse_compile_errors(text: str) -> list[CompileError]:
    """提取全部编译错误，含 symbol/location 续行。"""
    errors: list[CompileError] = []
    current: CompileError | None = None

    for raw in text.splitlines():
        # [WARNING] 不是错误：javac 的警告（如 varargs 提示）行格式与错误行一致，
        # 若不剔除会被误判为编译错误，进而把测试失败日志错误分类为 COMPILE_ERROR
        # （真实 case 暴露：commons-lang 的构建日志里就带这类警告）
        if raw.lstrip().startswith(("[WARNING]", "[INFO]")):
            continue
        line = _strip_level_prefix(raw)

        m = _ERROR_LINE_RE.match(line)
        if m:
            current = CompileError(
                file_path=_normalize_path(m.group("path")),
                line=int(m.group("line")),
                column=int(m.group("col")),
                message=m.group("msg"),
            )
            errors.append(current)
            continue

        if current is None:
            continue

        m = _SYMBOL_RE.match(line)
        if m:
            current.symbol = m.group("symbol")
            continue

        m = _LOCATION_RE.match(line)
        if m:
            current.location = m.group("location")

    return errors
