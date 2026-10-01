"""Java stack trace 解析器。

支持：
- 标准栈帧 "at com.example.Foo.bar(Foo.java:82)"
- JPMS 模块前缀 "at java.base/java.util.ArrayList.forEach(ArrayList.java:1511)"
- "Native Method" / "Unknown Source" 位置
- "Caused by:" 因果链
- Maven 输出中常见的 "[ERROR] " 行前缀
"""

from __future__ import annotations

import re

from devfix.models import StackFrame, StackTrace

# 栈帧行
_FRAME_RE = re.compile(
    r"^\s*at\s+"
    r"(?:(?P<module>[\w.$-]+)/)?"      # 可选 JPMS 模块前缀
    r"(?P<class>[\w.$]+)\."
    r"(?P<method>[\w$<>]+)"
    r"\((?P<loc>[^)]*)\)"
)

# 异常头："java.lang.NullPointerException: msg" / "Caused by: java.lang.IllegalStateException: msg"
_EXCEPTION_RE = re.compile(
    r"^\s*(?:Caused by:\s*)?"
    r"(?P<type>[\w.$]+(?:Exception|Error|Throwable))"
    r"(?::\s?(?P<msg>.*))?"
    r"\s*$"
)

_MORE_RE = re.compile(r"^\s*\.\.\.\s*\d+\s+more\s*$")

# Maven 按行级别输出的前缀，解析前先剥掉
_LEVEL_PREFIX_RE = re.compile(r"^\[(?:ERROR|WARNING|INFO)\]\s*")


def _strip_level_prefix(line: str) -> str:
    return _LEVEL_PREFIX_RE.sub("", line, count=1)


def _parse_location(loc: str) -> tuple[str | None, int | None]:
    """"OrderService.java:82" -> ("OrderService.java", 82)"""
    if loc in ("Native Method", "Unknown Source"):
        return None, None
    m = re.match(r"^(?P<file>.+?)(?::(?P<line>\d+))?$", loc)
    if not m or not m.group("file"):
        return None, None
    return m.group("file"), int(m.group("line")) if m.group("line") else None


def parse_stack_traces(text: str) -> list[StackTrace]:
    """从日志文本中提取全部异常栈（含 Caused by 链）。

    规则：
    - 异常头开启一条新栈；"Caused by:" 头挂到当前链尾。
    - 其后的 at 帧属于当前栈，直到遇到下一个异常头。
    """
    traces: list[StackTrace] = []
    current: StackTrace | None = None

    for raw in text.splitlines():
        line = _strip_level_prefix(raw)

        fm = _FRAME_RE.match(line)
        if fm:
            if current is None:
                continue  # 帧不属于任何已知异常，跳过
            file_name, line_no = _parse_location(fm.group("loc"))
            current.frames.append(
                StackFrame(
                    class_name=fm.group("class"),
                    method_name=fm.group("method"),
                    file_name=file_name,
                    line_number=line_no,
                    module_name=fm.group("module"),
                )
            )
            continue

        if _MORE_RE.match(line):
            continue

        em = _EXCEPTION_RE.match(line)
        if em:
            st = StackTrace(exception_type=em.group("type"), message=em.group("msg"))
            if line.lstrip().startswith("Caused by:") and current is not None:
                tail = current
                while tail.cause is not None:
                    tail = tail.cause
                tail.cause = st
            else:
                traces.append(st)
            current = st

    return traces
